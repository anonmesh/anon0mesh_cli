#!/usr/bin/env python3
"""
client.py — anon0mesh Client Node  (Multi-Beacon Edition)
==========================================================
Connects to multiple Beacon nodes simultaneously over the Reticulum mesh
and races RPC calls across all active links — the first valid response wins.

Beacon sources
--------------
  1. --beacon HASH [HASH ...]   explicit hashes (one or many)
  2. --discover                 passive: listens for beacon announcements
                                on the mesh and auto-joins them
  3. Both combined              start with known beacons, absorb new ones
                                as they announce themselves

Strategies
----------
  --strategy race      (default) broadcast to all links, return first response
  --strategy fallback  try beacons in order; advance on failure/timeout

Usage examples
--------------
  python client.py --beacon HASH1 HASH2
  python client.py --discover
  python client.py --beacon HASH1 --discover
  python client.py --beacon HASH1 HASH2 --balance <ADDRESS>
  python client.py --beacon HASH1 HASH2 HASH3 --strategy fallback
"""

import sys
import time
import json
import argparse
import threading
import base64
import os
from typing import Optional

try:
    import RNS
except ImportError:
    print("Reticulum not installed. Run:  pip install rns")
    sys.exit(1)

# patch synthesize_tunnel race
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

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    from solders.system_program import transfer, TransferParams
    from solders.message import Message
    from solders.hash import Hash
    HAS_SOLDERS = True
except ImportError:
    HAS_SOLDERS = False

from shared import (
    APP_NAME, APP_ASPECT, RPC_PATH, ANNOUNCE_DATA,
    RNS_REQUEST_TIMEOUT, build_rpc, decode_json,
    banner, log_info, log_ok, log_warn, log_err, log_tx,
    BOLD, CYAN, GREEN, YELLOW, RED, RESET, DIM,
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
        self._removed       = False        # set by pool.remove() to stop retries
        self._reconnecting  = False        # True while a retry thread is running
        self._retry_count   = 0
        self._last_identity: Optional["RNS.Identity"] = None  # most recent known identity

    def _on_established(self, link):
        with self._lock:
            self.link         = link
            self.active       = True
            self._reconnecting = False
            self._retry_count  = 0
        log_ok(f"[{self.label}] Link established")
        if identify_self and client_identity:
            link.identify(client_identity)
        link.set_link_closed_callback(self._on_closed)
        self.ready.set()

    def _on_closed(self, link):
        reason = link.teardown_reason
        with self._lock:
            self.active = False
            removed     = self._removed
        self.ready.clear()

        if removed:
            return  # manually removed — do not retry

        log_warn(f"[{self.label}] Link closed  reason={reason}  — will reconnect")
        self._schedule_reconnect()

    def _schedule_reconnect(self) -> None:
        """Spawn a reconnect thread if one isn't already running."""
        with self._lock:
            if self._reconnecting or self._removed:
                return
            self._reconnecting = True
        threading.Thread(target=self._reconnect_loop, daemon=True).start()

    def _reconnect_loop(self) -> None:
        """
        Retry loop with exponential backoff.
        Exits when:  connected  |  manually removed  |  no identity to use
        """
        while True:
            with self._lock:
                if self._removed:
                    return
                delay       = _BACKOFF[min(self._retry_count, len(_BACKOFF) - 1)]
                self._retry_count += 1

            log_info(f"[{self.label}] Reconnecting in {delay}s "
                     f"(attempt {self._retry_count})...")
            time.sleep(delay)

            with self._lock:
                if self._removed:
                    return
                identity = self._last_identity

            # Reset ready event for the new attempt
            self.ready.clear()

            ok = False
            if identity is not None:
                ok = self.connect_with_identity(identity, timeout=20.0)
            else:
                ok = self.connect(timeout=20.0)

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
            self._last_identity = beacon_id  # cache for reconnect loop

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
        """
        Fast-path connect used when the identity is already known (e.g. from
        an announce packet). Skips path resolution and Identity.recall() —
        both of which can fail in the window right after an announce arrives.
        Also used by the reconnect loop.
        """
        with self._lock:
            self._last_identity = identity  # always keep the freshest identity

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
        """
        Called when this beacon re-announces after a hop-out.
        Updates the cached identity and kicks the reconnect loop immediately
        instead of waiting for the next scheduled retry.
        """
        with self._lock:
            self._last_identity = identity
            already_active      = self.active
            already_reconnecting = self._reconnecting

        if already_active:
            return  # still connected, nothing to do

        if not already_reconnecting:
            # Link is dead and no retry thread running — start one immediately
            log_info(f"[{self.label}] Re-announce received while down — reconnecting now")
            self._schedule_reconnect()
        else:
            # Retry thread is sleeping — it will use the updated identity on
            # its next wake, no need to interrupt the sleep
            log_info(f"[{self.label}] Re-announce received — identity refreshed for next retry")

    def request(self, payload: bytes, result_event: threading.Event,
                result_holder: list, timeout: float) -> None:
        if not self.active or self.link is None:
            return

        def on_response(receipt):
            if receipt.response is not None and result_holder[0] is None:
                try:
                    parsed = decode_json(bytes(receipt.response))
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
            self._removed = True   # stop any running reconnect loop
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
        """
        Called directly from the announce handler with the identity in hand.
        Two cases:
          - New beacon:  create BeaconLink, connect immediately (no recall needed)
          - Known beacon that re-announced after hop-out: refresh its identity
            and kick the reconnect loop so it reconnects immediately instead of
            waiting for its next scheduled backoff retry
        """
        dest_hash_hex = dest_hash.hex()

        with self._lock:
            existing = self._links.get(dest_hash_hex)

        if existing is not None:
            # Beacon already known — it hopped back in
            existing.refresh_identity(identity)
            return

        # Brand-new beacon
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
    # None = receive ALL announces, then filter by app_data ourselves.
    # Using APP_ASPECT here is unreliable because RNS matches it against the
    # full destination name ("anonmesh.rpc_beacon") which may or may not
    # do a suffix match depending on the RNS version.
    aspect_filter = None

    def __init__(self, p: BeaconPool):
        self.pool = p

    def received_announce(self, destination_hash, announced_identity, app_data):
        # Only handle our own beacon announces, ignore everything else on the mesh
        if app_data != ANNOUNCE_DATA:
            return
        # announced_identity is passed directly — no recall needed
        if announced_identity is None:
            return
        hash_hex = destination_hash.hex()
        short    = hash_hex[:12] + "..."
        with self.pool._lock:
            already = hash_hex in self.pool._links
        if already:
            return
        log_info(f"Discovered beacon via announce: {short}")
        # Pass identity directly — skips path resolution and recall race
        self.pool.add_from_announce(destination_hash, announced_identity, label=f"disc:{short}")


# ═══════════════════════════════════════════════════════════════════════════════
# Global state
# ═══════════════════════════════════════════════════════════════════════════════

pool:            BeaconPool | None = None
client_identity: RNS.Identity | None = None
identify_self:   bool = True
request_timeout: float = RNS_REQUEST_TIMEOUT


# ═══════════════════════════════════════════════════════════════════════════════
# Solana helpers
# ═══════════════════════════════════════════════════════════════════════════════

def rpc_call(method, params=None):
    return pool.call(method, params)

def get_balance(address):
    resp = rpc_call("getBalance", [address])
    if resp is None: return
    if "error" in resp:
        log_err(f"RPC error: {resp['error'].get('message', resp['error'])}"); return
    lamports = resp.get("result", {})
    if isinstance(lamports, dict):
        lamports = lamports.get("value", 0)
    sol = lamports / 1_000_000_000
    print(f"\n  {GREEN}{BOLD}{address}{RESET}")
    print(f"  Balance: {BOLD}{sol:.9f} SOL{RESET}  ({lamports:,} lamports)\n")

def get_slot():
    resp = rpc_call("getSlot")
    if resp and "result" in resp:
        print(f"\n  Current slot: {BOLD}{resp['result']:,}{RESET}\n")

def get_block_height():
    resp = rpc_call("getBlockHeight")
    if resp and "result" in resp:
        print(f"\n  Block height: {BOLD}{resp['result']:,}{RESET}\n")

def get_transaction_count():
    resp = rpc_call("getTransactionCount")
    if resp and "result" in resp:
        print(f"\n  Transaction count: {BOLD}{resp['result']:,}{RESET}\n")

def get_recent_blockhash():
    resp = rpc_call("getLatestBlockhash")
    if resp and "result" in resp:
        bh = resp["result"]["value"]["blockhash"]
        print(f"\n  Latest blockhash: {BOLD}{bh}{RESET}\n")
        return bh
    return None

def get_token_accounts(owner):
    # Fire both requests in parallel through the beacon pool
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_sol    = ex.submit(rpc_call, "getBalance", [owner])
        fut_tokens = ex.submit(rpc_call, "getTokenAccountsByOwner", [
            owner,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ])
        sol_resp   = fut_sol.result()
        token_resp = fut_tokens.result()

    # ── Header ────────────────────────────────────────────────────────────────
    print(f"\n  {GREEN}{BOLD}{owner}{RESET}")

    # ── SOL balance ───────────────────────────────────────────────────────────
    if sol_resp and "result" in sol_resp:
        lamports = sol_resp["result"]
        if isinstance(lamports, dict):
            lamports = lamports.get("value", 0)
        sol = lamports / 1_000_000_000
        print(f"  {BOLD}SOL Balance:{RESET}  {BOLD}{sol:.9f} SOL{RESET}  {DIM}({lamports:,} lamports){RESET}")
    else:
        log_warn("Could not fetch SOL balance")

    # ── SPL tokens ────────────────────────────────────────────────────────────
    if token_resp and "result" in token_resp:
        accounts = token_resp["result"]["value"]
        if not accounts:
            print(f"  {DIM}No SPL token accounts{RESET}")
        else:
            print(f"  {BOLD}SPL Tokens ({len(accounts)}){RESET}")
            for acc in accounts:
                try:
                    info     = acc["account"]["data"]["parsed"]["info"]
                    mint     = info.get("mint", "?")
                    decimals = info.get("tokenAmount", {}).get("decimals", 0)
                    amount   = info.get("tokenAmount", {}).get("uiAmountString", "?")
                    symbol   = f"  {DIM}({decimals} decimals){RESET}" if decimals else ""
                    print(f"  {DIM}·{RESET} {mint}  {BOLD}{amount}{RESET}{symbol}")
                except (KeyError, TypeError):
                    print(f"  {DIM}· (could not parse account){RESET}")
    else:
        log_warn("Could not fetch SPL token accounts")

    print()

def send_transaction(signed_tx_b64):
    resp = rpc_call("sendTransaction", [signed_tx_b64, {"encoding": "base64"}])
    if resp is None: return
    if "error" in resp:
        log_err(f"Transaction rejected: {resp['error'].get('message', resp['error'])}"); return
    log_ok("Transaction relayed via mesh!")
    print(f"\n  Signature: {BOLD}{GREEN}{resp.get('result','?')}{RESET}\n")

def simulate_transaction(signed_tx_b64):
    resp = rpc_call("simulateTransaction", [signed_tx_b64, {"encoding": "base64"}])
    if resp and "result" in resp:
        sim = resp["result"]["value"]
        if sim.get("err"):
            log_warn(f"Simulation error: {sim['err']}")
        else:
            log_ok("Simulation successful")
        for line in sim.get("logs", []):
            print(f"  {DIM}{line}{RESET}")
        print()

def offline_sign_transfer(keypair_json_path, to_address, lamports, blockhash=None):
    if not HAS_SOLDERS:
        log_err("Offline signing requires: pip install solders"); return None
    try:
        with open(keypair_json_path) as f:
            keypair = Keypair.from_bytes(bytes(json.load(f)))
    except Exception as exc:
        log_err(f"Failed to load keypair: {exc}"); return None

    from_pubkey = keypair.pubkey()
    to_pubkey   = Pubkey.from_string(to_address)
    log_info(f"Signing  from={from_pubkey}  to={to_pubkey}  lamports={lamports}")

    if blockhash is None:
        blockhash = get_recent_blockhash()
        if blockhash is None:
            log_err("Could not obtain blockhash"); return None

    ix  = transfer(TransferParams(from_pubkey=from_pubkey, to_pubkey=to_pubkey, lamports=lamports))
    msg = Message.new_with_blockhash([ix], from_pubkey, Hash.from_string(blockhash))
    tx  = Transaction.new_unsigned(msg)
    tx.sign([keypair], Hash.from_string(blockhash))
    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    log_ok(f"Transaction signed offline ({len(tx_b64)} chars)")
    print(f"\n  Signed TX: {BOLD}{tx_b64[:72]}...{RESET}\n")
    return tx_b64


# ═══════════════════════════════════════════════════════════════════════════════
# REPL
# ═══════════════════════════════════════════════════════════════════════════════

HELP_TEXT = f"""
{BOLD}RPC commands:{RESET}
  balance  <ADDRESS>                Fetch SOL balance
  slot                              Current slot
  height                            Block height
  txcount                           Transaction count
  blockhash                         Latest blockhash
  tokens   <OWNER_ADDRESS>          SPL token accounts
  send     <BASE64_SIGNED_TX>       Relay signed transaction
  simulate <BASE64_SIGNED_TX>       Simulate (dry-run)
  raw      <METHOD> [PARAMS_JSON]   Raw JSON-RPC call

{BOLD}Pool management:{RESET}
  beacons                           List all beacons + status
  add      <HASH>                   Add + connect a beacon
  remove   <HASH>                   Remove a beacon
  strategy <race|fallback>          Switch dispatch strategy

{BOLD}Other:{RESET}
  help  /  quit
"""

def repl():
    print(HELP_TEXT)
    while True:
        n = len(pool.active_links())
        col = GREEN if n > 0 else RED
        try:
            line = input(f"{DIM}[{n} beacon{'s' if n != 1 else ''}]{RESET} {col}mesh>{RESET} ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); break
        if not line: continue
        parts = line.split(None, 2)
        cmd   = parts[0].lower()

        if cmd in ("quit", "exit", "q"):
            break
        elif cmd == "help":
            print(HELP_TEXT)
        elif cmd == "beacons":
            print(pool.status_table())
        elif cmd == "add" and len(parts) >= 2:
            h = parts[1]
            threading.Thread(
                target=pool.add,
                args=(h,),
                kwargs={"label": f"manual:{h[:12]}...", "connect": True},
                daemon=True,
            ).start()
        elif cmd == "remove" and len(parts) >= 2:
            pool.remove(parts[1])
        elif cmd == "strategy" and len(parts) >= 2:
            s = parts[1].lower()
            if s in ("race", "fallback"):
                pool.strategy = s; log_ok(f"Strategy: {s}")
            else:
                log_warn("Use 'race' or 'fallback'")
        elif cmd == "balance" and len(parts) >= 2:
            get_balance(parts[1])
        elif cmd == "slot":
            get_slot()
        elif cmd == "height":
            get_block_height()
        elif cmd == "txcount":
            get_transaction_count()
        elif cmd == "blockhash":
            get_recent_blockhash()
        elif cmd == "tokens" and len(parts) >= 2:
            get_token_accounts(parts[1])
        elif cmd == "send" and len(parts) >= 2:
            send_transaction(parts[1])
        elif cmd == "simulate" and len(parts) >= 2:
            simulate_transaction(parts[1])
        elif cmd == "raw" and len(parts) >= 2:
            method = parts[1]
            params = json.loads(parts[2]) if len(parts) >= 3 else []
            resp   = rpc_call(method, params)
            if resp: print(json.dumps(resp, indent=2))
        else:
            log_warn(f"Unknown command: {cmd}  (type 'help')")


# ═══════════════════════════════════════════════════════════════════════════════
# Startup
# ═══════════════════════════════════════════════════════════════════════════════

def start_reticulum(config_path):
    global client_identity
    RNS.Reticulum(config_path)
    deadline = time.time() + 5.0
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    log_ok("Reticulum started")
    client_identity = RNS.Identity()
    log_info(f"Client identity: {RNS.prettyhexrep(client_identity.hash)}")


def connect_all_parallel(hashes, label_prefix=""):
    results = {}
    threads = []
    def _connect(h):
        label      = f"{label_prefix}{h[:12]}..." if label_prefix else h[:12] + "..."
        results[h] = pool.add(h, label=label, connect=True)
    for h in hashes:
        t = threading.Thread(target=_connect, args=(h,), daemon=True)
        threads.append(t); t.start()
    for t in threads:
        t.join()
    return sum(1 for v in results.values() if v)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    global pool, identify_self, request_timeout

    parser = argparse.ArgumentParser(
        description="anon0mesh Client — multi-beacon Solana RPC over Reticulum mesh"
    )
    parser.add_argument("--beacon", "-b", nargs="+", metavar="HASH", default=[],
                        help="One or more beacon destination hashes")
    parser.add_argument("--discover", "-d", action="store_true",
                        help="Auto-discover beacons via RNS announces")
    parser.add_argument("--strategy", default="race", choices=["race", "fallback"])
    parser.add_argument("--config",   "-c", default=None)
    parser.add_argument("--timeout",  "-t", default=30, type=int)
    parser.add_argument("--no-identify", action="store_true")
    parser.add_argument("--balance",     metavar="ADDRESS")
    parser.add_argument("--slot",        action="store_true")
    parser.add_argument("--blockhash",   action="store_true")
    parser.add_argument("--send-tx",     metavar="BASE64_TX")
    parser.add_argument("--simulate-tx", metavar="BASE64_TX")
    parser.add_argument("--sign-offline", action="store_true")
    parser.add_argument("--from",        dest="from_keypair", metavar="KEYPAIR_JSON")
    parser.add_argument("--to",          metavar="ADDRESS")
    parser.add_argument("--lamports",    type=int, default=0)
    parser.add_argument("--blockhash-value", metavar="HASH")
    args = parser.parse_args()

    if not args.beacon and not args.discover:
        parser.error("Provide --beacon HASH [HASH ...] and/or --discover")

    identify_self   = not args.no_identify
    request_timeout = float(args.timeout)

    banner("CLIENT  ·  Multi-Beacon Mesh RPC")
    start_reticulum(args.config)

    pool = BeaconPool(strategy=args.strategy, request_timeout=request_timeout)

    if args.discover:
        RNS.Transport.register_announce_handler(BeaconAnnounceHandler(pool))
        log_info("Discovery active — listening for beacon announces...")

    if args.beacon:
        log_info(f"Connecting to {len(args.beacon)} beacon(s) in parallel...")
        connected = connect_all_parallel(args.beacon)
        log_ok(f"{connected}/{len(args.beacon)} beacon(s) connected")
        if connected == 0 and not args.discover:
            log_err("No beacons connected and discovery is off")
            sys.exit(1)

    if args.discover and not args.beacon:
        log_info("Waiting for beacon announces (30 s, beacons burst-announce every 15 s)...")
        deadline = time.time() + 30
        while not pool.active_links() and time.time() < deadline:
            time.sleep(0.5)
        if not pool.active_links():
            log_warn("No beacons discovered yet — REPL is open")

    print(pool.status_table())

    one_shot = any([args.balance, args.slot, args.blockhash,
                    args.send_tx, args.simulate_tx, args.sign_offline])

    if args.balance:      get_balance(args.balance)
    if args.slot:         get_slot()
    if args.blockhash:    get_recent_blockhash()
    if args.send_tx:      send_transaction(args.send_tx)
    if args.simulate_tx:  simulate_transaction(args.simulate_tx)
    if args.sign_offline:
        if not (args.from_keypair and args.to and args.lamports):
            log_err("--sign-offline requires --from, --to, --lamports")
        else:
            tx_b64 = offline_sign_transfer(
                args.from_keypair, args.to, args.lamports, args.blockhash_value)
            if tx_b64:
                if input("  Relay now? [y/N]: ").strip().lower() == "y":
                    send_transaction(tx_b64)

    if not one_shot:
        repl()

    pool.teardown_all()
    log_info("Client disconnected.")


if __name__ == "__main__":
    main()
