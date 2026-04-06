"""
mesh.py — Reticulum network layer
===================================
BeaconLink, BeaconPool, BeaconAnnounceHandler, and startup helpers.
"""

import time
import threading
from typing import Optional

try:
    import RNS
except ImportError:
    import sys
    print("Reticulum not installed. Run:  pip install rns")
    sys.exit(1)

# Patch a race condition in RNS.Transport.synthesize_tunnel that fires when
# the transport identity is not yet set at startup.
_orig_st = RNS.Transport.synthesize_tunnel.__func__ \
    if hasattr(RNS.Transport.synthesize_tunnel, "__func__") \
    else RNS.Transport.synthesize_tunnel

def _safe_st(interface):
    if RNS.Transport.identity is None:
        return
    try:
        _orig_st(interface)
    except AttributeError:
        pass

RNS.Transport.synthesize_tunnel = staticmethod(_safe_st)

import state
from shared import (
    APP_NAME, APP_ASPECT, RPC_PATH, ANNOUNCE_DATA,
    RNS_REQUEST_TIMEOUT, build_rpc, decode_json, decompress_response,
    log_info, log_ok, log_warn, log_err,
    BOLD, GREEN, RED, RESET, DIM,
)


# ═══════════════════════════════════════════════════════════════════════════════
# BeaconLink — one encrypted RNS link to one beacon
# ═══════════════════════════════════════════════════════════════════════════════

# Reconnection backoff schedule (seconds between retries)
_BACKOFF = [5, 10, 20, 40, 60, 120, 300]


class BeaconLink:
    """
    Manages one encrypted RNS link to one beacon.

    Reconnection state machine
    ──────────────────────────
    ACTIVE  ──(link closed)──►  RECONNECTING  ──(success)──►  ACTIVE
                                      │
                                  (removed)
                                      │
                                      ▼
                                   DEAD (no retry)

    When a link drops (hop-out), _on_closed fires → _schedule_reconnect()
    starts a background thread that retries with exponential backoff using
    _BACKOFF = [5, 10, 20, 40, 60, 120, 300] seconds between attempts.

    When the beacon re-announces (hop-back-in), add_from_announce on the pool
    calls refresh_identity() which resets the backoff and reconnects immediately
    using the fresh identity from the announce — no waiting for the next retry.
    """

    def __init__(self, dest_hash_hex: str, label: str = ""):
        self.dest_hash_hex  = dest_hash_hex.lower().strip()
        self.dest_hash      = bytes.fromhex(self.dest_hash_hex)
        self.label          = label or self.dest_hash_hex[:12] + "..."
        self.link: Optional[RNS.Link] = None
        self.ready          = threading.Event()
        self.active         = False
        self._lock          = threading.Lock()
        self._removed       = False
        self._reconnecting  = False
        self._retry_count   = 0
        self._last_identity: Optional["RNS.Identity"] = None

    def _on_established(self, link):
        with self._lock:
            self.link          = link
            self.active        = True
            self._reconnecting = False
            self._retry_count  = 0
        log_ok(f"[{self.label}] Link established")
        if state.identify_self and state.client_identity:
            link.identify(state.client_identity)
        link.set_link_closed_callback(self._on_closed)
        self.ready.set()

    def _on_closed(self, link):
        reason = link.teardown_reason
        with self._lock:
            self.active = False
            removed     = self._removed
        self.ready.clear()

        if removed:
            return

        log_warn(f"[{self.label}] Link closed  reason={reason}  — will reconnect")
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        with self._lock:
            if self._reconnecting or self._removed:
                return
            self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self) -> None:
        while True:
            with self._lock:
                if self._removed:
                    return
                delay             = _BACKOFF[min(self._retry_count, len(_BACKOFF) - 1)]
                self._retry_count += 1

            log_info(f"[{self.label}] Reconnecting in {delay}s (attempt {self._retry_count})...")
            time.sleep(delay)

            with self._lock:
                if self._removed:
                    return
                identity = self._last_identity

            self.ready.clear()

            ok = self.connect_with_identity(identity, timeout=20.0) \
                 if identity is not None else self.connect(timeout=20.0)

            if ok:
                log_ok(f"[{self.label}] Reconnected successfully")
                with self._lock:
                    self._reconnecting = False
                return

            log_warn(f"[{self.label}] Reconnect attempt {self._retry_count} failed")

    def connect(self, timeout: float = 20.0) -> bool:
        if not RNS.Transport.has_path(self.dest_hash):
            log_info(f"[{self.label}] Requesting path from mesh...")
            RNS.Transport.request_path(self.dest_hash)
            deadline = time.time() + timeout
            while not RNS.Transport.has_path(self.dest_hash):
                if time.time() > deadline:
                    log_err(f"[{self.label}] Path resolution timed out")
                    return False
                time.sleep(0.2)
        else:
            log_info(f"[{self.label}] Path already cached")

        beacon_id = RNS.Identity.recall(self.dest_hash)
        if beacon_id is None:
            log_err(f"[{self.label}] Could not recall identity")
            return False

        with self._lock:
            self._last_identity = beacon_id

        dest = RNS.Destination(
            beacon_id,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT,
        )
        link = RNS.Link(dest)
        link.set_link_established_callback(self._on_established)

        if not self.ready.wait(timeout=timeout):
            log_err(f"[{self.label}] Link establishment timed out")
            return False
        return True

    def connect_with_identity(self, identity: "RNS.Identity", timeout: float = 20.0) -> bool:
        with self._lock:
            self._last_identity = identity

        dest = RNS.Destination(
            identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            APP_ASPECT,
        )
        link = RNS.Link(dest)
        link.set_link_established_callback(self._on_established)
        if not self.ready.wait(timeout=timeout):
            log_err(f"[{self.label}] Link establishment timed out (identity fast-path)")
            return False
        return True

    def refresh_identity(self, identity: "RNS.Identity") -> None:
        with self._lock:
            self._last_identity      = identity
            already_active           = self.active
            already_reconnecting     = self._reconnecting

        if already_active:
            return

        if not already_reconnecting:
            log_info(f"[{self.label}] Re-announce received while down — reconnecting now")
            self._schedule_reconnect()
        else:
            log_info(f"[{self.label}] Re-announce received — identity refreshed for next retry")

    def request(self, payload: bytes, result_event: threading.Event,
                result_holder: list, timeout: float) -> None:
        if not self.active or self.link is None:
            return

        def on_response(receipt):
            if receipt.response is not None and result_holder[0] is None:
                try:
                    raw = decompress_response(bytes(receipt.response))
                    parsed = decode_json(raw)
                    if "result" in parsed or "error" in parsed:
                        result_holder[0] = parsed
                        result_holder[1] = self.label
                        result_event.set()
                except Exception:
                    pass

        def on_failed(receipt):
            log_warn(f"[{self.label}] Request failed / timed out")

        try:
            self.link.request(
                RPC_PATH,
                data=payload,
                response_callback=on_response,
                failed_callback=on_failed,
                timeout=timeout,
            )
        except Exception as exc:
            log_warn(f"[{self.label}] Send error: {exc}")

    def teardown(self):
        with self._lock:
            self._removed = True
            self.active   = False
            link          = self.link
        if link:
            try:
                link.teardown()
            except Exception:
                pass

    def __repr__(self):
        return f"BeaconLink({self.label} {'ACTIVE' if self.active else 'DOWN'})"


# ═══════════════════════════════════════════════════════════════════════════════
# BeaconPool — N links, race / fallback dispatch
# ═══════════════════════════════════════════════════════════════════════════════

class BeaconPool:

    def __init__(self, strategy: str = "race", request_timeout: float = 30.0):
        self.strategy        = strategy
        self.request_timeout = request_timeout
        self._links: dict[str, BeaconLink] = {}
        self._lock           = threading.Lock()
        self._pending_count  = 0

    def add_background(self, dest_hash_hex: str, label: str = "") -> None:
        """Fire-and-forget connect. Returns immediately; link becomes active in the background."""
        dest_hash_hex = dest_hash_hex.lower().strip()
        expected = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
        if len(dest_hash_hex) != expected:
            log_err(f"Invalid hash length ({len(dest_hash_hex)} chars, need {expected})")
            return
        with self._lock:
            if dest_hash_hex in self._links:
                return
            self._pending_count += 1

        bl = BeaconLink(dest_hash_hex, label or dest_hash_hex[:12] + "...")
        with self._lock:
            self._links[dest_hash_hex] = bl

        def _connect():
            ok = bl.connect(timeout=25.0)
            with self._lock:
                self._pending_count = max(0, self._pending_count - 1)
            if not ok:
                with self._lock:
                    self._links.pop(dest_hash_hex, None)

        threading.Thread(target=_connect, daemon=True).start()

    def pending_count(self) -> int:
        with self._lock:
            return self._pending_count

    def add(self, dest_hash_hex: str, label: str = "", connect: bool = True) -> bool:
        dest_hash_hex = dest_hash_hex.lower().strip()
        expected = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
        if len(dest_hash_hex) != expected:
            log_err(f"Invalid hash length ({len(dest_hash_hex)} chars, need {expected})")
            return False
        with self._lock:
            if dest_hash_hex in self._links:
                return True
        bl = BeaconLink(dest_hash_hex, label)
        with self._lock:
            self._links[dest_hash_hex] = bl
        if connect:
            ok = bl.connect(timeout=20.0)
            if not ok:
                with self._lock:
                    del self._links[dest_hash_hex]
            return ok
        return True

    def add_from_announce(self, dest_hash: bytes, identity: "RNS.Identity", label: str = "") -> None:
        dest_hash_hex = dest_hash.hex()

        with self._lock:
            existing = self._links.get(dest_hash_hex)

        if existing is not None:
            existing.refresh_identity(identity)
            return

        bl = BeaconLink(dest_hash_hex, label or dest_hash_hex[:12] + "...")
        with self._lock:
            self._links[dest_hash_hex] = bl

        def _open():
            ok = bl.connect_with_identity(identity, timeout=20.0)
            if ok:
                log_ok(f"[{bl.label}] Auto-connected via announce ({self.size()} in pool)")
            else:
                with self._lock:
                    self._links.pop(dest_hash_hex, None)

        threading.Thread(target=_open, daemon=True).start()

    def remove(self, dest_hash_hex: str) -> None:
        dest_hash_hex = dest_hash_hex.lower().strip()
        with self._lock:
            bl = self._links.pop(dest_hash_hex, None)
        if bl:
            bl.teardown()
            log_info(f"Removed beacon {bl.label}")

    def active_links(self) -> list:
        with self._lock:
            return [bl for bl in self._links.values() if bl.active]

    def all_links(self) -> list:
        with self._lock:
            return list(self._links.values())

    def size(self) -> int:
        return len(self._links)

    def teardown_all(self) -> None:
        with self._lock:
            links = list(self._links.values())
            self._links.clear()
        for bl in links:
            bl.teardown()

    def call(self, method: str, params=None) -> dict | None:
        active = self.active_links()
        if not active:
            log_err("No active beacons in pool")
            return None
        payload = build_rpc(method, params or [])
        from shared import log_tx
        log_tx(f"method={method}  beacons={len(active)}  strategy={self.strategy}  ({len(payload)}B)")
        if self.strategy == "race":
            return self._race(active, payload, method)
        return self._fallback(active, payload, method)

    def _race(self, links, payload, method):
        result_holder = [None, None]
        result_event  = threading.Event()
        for bl in links:
            bl.request(payload, result_event, result_holder, self.request_timeout)
        fired = result_event.wait(timeout=self.request_timeout + 5)
        if fired and result_holder[0] is not None:
            log_ok(f"Response from [{result_holder[1]}]  method={method}")
            return result_holder[0]
        log_err(f"All {len(links)} beacons failed/timed out for {method}")
        return None

    def _fallback(self, links, payload, method):
        for bl in links:
            if not bl.active:
                continue
            result_holder = [None, None]
            result_event  = threading.Event()
            bl.request(payload, result_event, result_holder, self.request_timeout)
            if result_event.wait(timeout=self.request_timeout + 5) and result_holder[0] is not None:
                log_ok(f"Response from [{bl.label}]  method={method}")
                return result_holder[0]
            log_warn(f"[{bl.label}] no response, trying next...")
        log_err(f"All beacons exhausted for {method}")
        return None

    def status_table(self) -> str:
        lines = [f"\n  {BOLD}Beacon Pool  ({self.strategy} strategy){RESET}"]
        with self._lock:
            items = list(self._links.values())
        if not items:
            lines.append("  (empty)")
        for bl in items:
            dot = f"{GREEN}●{RESET}" if bl.active else f"{RED}○{RESET}"
            lines.append(f"  {dot}  {BOLD}{bl.label}{RESET}  {DIM}{bl.dest_hash_hex}{RESET}")
        lines.append("")
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-discovery via RNS announce handler
# ═══════════════════════════════════════════════════════════════════════════════

class BeaconAnnounceHandler:
    aspect_filter = None

    def __init__(self, p: BeaconPool):
        self.pool = p

    def received_announce(self, destination_hash, announced_identity, app_data):
        hash_hex = destination_hash.hex()
        short    = hash_hex[:12] + "..."

        if app_data is None:
            return
        if app_data != ANNOUNCE_DATA:
            try:
                if app_data.decode("utf-8", errors="replace") != ANNOUNCE_DATA.decode("utf-8"):
                    return
            except Exception:
                return

        if announced_identity is None:
            log_warn(f"Announce from {short} has no identity — skipping")
            return

        with self.pool._lock:
            already = hash_hex in self.pool._links
        if already:
            log_info(f"Beacon {short} already in pool — skipping")
            return

        log_ok(f"Discovered beacon via announce: {short}")
        self.pool.add_from_announce(destination_hash, announced_identity, label=f"disc:{short}")


# ═══════════════════════════════════════════════════════════════════════════════
# Startup helpers
# ═══════════════════════════════════════════════════════════════════════════════

def start_reticulum(config_path) -> None:
    RNS.Reticulum(config_path)
    deadline = time.time() + 5.0
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    log_ok("Reticulum started")
    state.client_identity = RNS.Identity()
    log_info(f"Client identity: {RNS.prettyhexrep(state.client_identity.hash)}")


def connect_all_parallel(hashes: list[str], label_prefix: str = "") -> int:
    """Blocking parallel connect. Returns the number of successful connections."""
    results = {}
    threads = []

    def _connect(h):
        label      = f"{label_prefix}{h[:12]}..." if label_prefix else h[:12] + "..."
        results[h] = state.pool.add(h, label=label, connect=True)

    for h in hashes:
        t = threading.Thread(target=_connect, args=(h,), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()
    return sum(1 for v in results.values() if v)
