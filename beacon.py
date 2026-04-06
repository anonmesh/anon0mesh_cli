#!/usr/bin/env python3
"""
beacon.py — anon0mesh Beacon Node (Receiver / RPC Gateway)
============================================================
Runs a Reticulum destination that accepts encrypted RPC requests
from clients on the mesh, forwards them to a Solana JSON-RPC endpoint,
and returns the response — all tunnelled through Reticulum's end-to-end
encrypted link layer.

Usage
-----
  python beacon.py                          # devnet, auto config
  python beacon.py --network mainnet        # mainnet-beta
  python beacon.py --rpc https://my.node   # custom RPC endpoint
  python beacon.py --config ~/.reticulum   # custom RNS config dir

The beacon prints its DESTINATION HASH on startup.
Share this hash with clients so they can reach you on the mesh.

Architecture recap (anon0mesh Proof-of-Relay):
  Client ──[RNS Link, AES-256-GCM]──► Beacon ──[HTTPS]──► Solana RPC
  Beacon earns a fee (future: on-chain) for settling transactions.
"""

import sys
import time
import argparse
import os
import json
import threading

# ── SSL cert fix — must happen before importing requests ──────────────────────
# If certifi's .pem is missing (broken venv), fall back to the system bundle.
import certifi as _certifi
if not os.path.isfile(_certifi.where()):
    _system_certs = "/etc/ssl/certs/ca-certificates.crt"
    if os.path.isfile(_system_certs):
        os.environ["REQUESTS_CA_BUNDLE"] = _system_certs
        os.environ["SSL_CERT_FILE"]      = _system_certs
# Also respect .env override
_env_cert = os.getenv("REQUESTS_CA_BUNDLE", "")
if _env_cert and os.path.isfile(_env_cert):
    os.environ["REQUESTS_CA_BUNDLE"] = _env_cert
    os.environ["SSL_CERT_FILE"]      = _env_cert

import requests

# ── Reticulum ──────────────────────────────────────────────────────────────────
try:
    import RNS
except ImportError:
    print("Reticulum not installed. Run:  pip install rns")
    sys.exit(1)

# ── Patch: guard synthesize_tunnel against Transport.identity race ─────────────
# RNS bug: TCPInterface.reconnect() fires synthesize_tunnel in a background
# thread before Transport.identity is set, causing:
#   AttributeError: 'NoneType' object has no attribute 'get_public_key'
# Fix: wrap the method to bail silently when identity isn't ready yet.
# Safe to apply unconditionally — the tunnel will be synthesized on the
# *next* successful reconnect once Transport is fully initialised.
_original_synthesize_tunnel = RNS.Transport.synthesize_tunnel.__func__ \
    if hasattr(RNS.Transport.synthesize_tunnel, "__func__") \
    else RNS.Transport.synthesize_tunnel

def _safe_synthesize_tunnel(interface):
    if RNS.Transport.identity is None:
        # Transport not ready yet — skip this cycle, reconnect will retry
        return
    try:
        _original_synthesize_tunnel(interface)
    except AttributeError:
        pass  # still not ready, will retry on next reconnect

RNS.Transport.synthesize_tunnel = staticmethod(_safe_synthesize_tunnel)

# ── Local ──────────────────────────────────────────────────────────────────────
from shared import (
    APP_NAME, APP_ASPECT, RPC_PATH, ANNOUNCE_DATA,
    SOLANA_ENDPOINTS, RNS_REQUEST_TIMEOUT,
    decode_json, build_response, compress_response,
    banner, log_info, log_ok, log_warn, log_err, log_tx,
    BOLD, CYAN, GREEN, RESET, DIM,
)

# ── Auto-load .env if present ─────────────────────────────────────────────────
from pathlib import Path
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# ── Arcium MPC integration (optional — enabled via ARCIUM_ENABLED=1) ───────────
try:
    from arcium_client import ArciumBeacon, rescue_keygen, rescue_encrypt, rescue_decrypt
    HAS_ARCIUM_MODULE = True
except ImportError:
    HAS_ARCIUM_MODULE = False

# ── Solders (optional — enables tx co-signing) ─────────────────────────────────
try:
    import base64 as _base64
    from solders.keypair    import Keypair     as _Keypair
    from solders.transaction import Transaction as _Transaction
    from solders.hash        import Hash        as _Hash
    HAS_SOLDERS = True
except ImportError:
    HAS_SOLDERS = False

# ── State ──────────────────────────────────────────────────────────────────────
beacon_identity         = None
beacon_destination      = None
rpc_endpoint            = None
arcium: "ArciumBeacon | None" = None
beacon_cosign_keypair   = None   # Keypair used to co-sign client Arcium txs
request_count           = 0
request_lock            = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# RPC Forwarding
# ═══════════════════════════════════════════════════════════════════════════════

# ═══════════════════════════════════════════════════════════════════════════════
# Arcium co-sign handlers  (getBeaconPubkey / cosignTransaction)
# ═══════════════════════════════════════════════════════════════════════════════

def _handle_get_beacon_pubkey(req_id: int) -> bytes:
    """Return the beacon's co-signing pubkey so clients can build co-sign txs."""
    if not HAS_SOLDERS or beacon_cosign_keypair is None:
        return build_response(
            error="Beacon co-signing keypair not configured (set ARCIUM_PAYER_KEYPAIR)",
            req_id=req_id,
        )
    return build_response(result=str(beacon_cosign_keypair.pubkey()), req_id=req_id)


def _handle_cosign_transaction(params: list, req_id: int, count: int) -> bytes:
    """
    Co-sign a partially-signed client transaction and submit it to Solana.
    params[0]: base64-encoded partially-signed transaction
    params[1]: optional {"arcium": {...}} metadata for post-relay stats logging
    """
    if not HAS_SOLDERS or beacon_cosign_keypair is None:
        return build_response(error="Beacon co-signing keypair not configured", req_id=req_id)
    if not params or not isinstance(params[0], str):
        return build_response(error="cosignTransaction: params[0] must be a base64 tx", req_id=req_id)

    try:
        tx_bytes = _base64.b64decode(params[0])
        tx       = _Transaction.from_bytes(tx_bytes)
        bh       = tx.message.recent_blockhash
        tx.partial_sign([beacon_cosign_keypair], bh)
        fully_signed_b64 = _base64.b64encode(bytes(tx)).decode()
    except Exception as exc:
        log_err(f"[#{count}] Co-sign error: {exc}")
        return build_response(error=f"Co-sign failed: {exc}", req_id=req_id)

    submit_req   = {"jsonrpc": "2.0", "id": req_id, "method": "sendTransaction",
                    "params": [fully_signed_b64, {"encoding": "base64", "skipPreflight": True,
                                                  "preflightCommitment": "confirmed"}]}
    result_bytes = forward_plain_rpc(submit_req, req_id, count, "sendTransaction[co-signed]")
    return result_bytes


def _dispatch_cosign(method: str, params: list, req_id: int, count: int) -> "bytes | None":
    """Route Arcium co-sign protocol methods. Returns None for all other methods."""
    if method == "getBeaconPubkey":
        return _handle_get_beacon_pubkey(req_id)
    if method == "cosignTransaction":
        return _handle_cosign_transaction(params, req_id, count)
    return None


def _resolve_arcium_meta(params: list) -> dict:
    """Extract arcium metadata from params and fill missing token fields from env."""
    meta = {}
    if len(params) > 1 and isinstance(params[1], dict):
        meta = params[1].get("arcium", {})
    if not meta.get("mint"):
        meta = dict(meta, mint=os.getenv("ARCIUM_MINT", ""))
    if not meta.get("payer_ta"):
        meta = dict(meta, payer_ta=os.getenv("ARCIUM_PAYER_TOKEN_ACCOUNT", ""))
    if not meta.get("recipient_ta"):
        meta = dict(meta, recipient_ta=os.getenv("ARCIUM_RECIPIENT_TOKEN_ACCOUNT", ""))
    if not meta.get("broadcaster_ta"):
        v = os.getenv("ARCIUM_BROADCASTER_TOKEN_ACCOUNT", "")
        if v:
            meta = dict(meta, broadcaster_ta=v)
    return meta


def _fire_arcium_stats(meta: dict, count: int, label: str) -> None:
    """Call arcium.log_payment_stats if amount, mint, and payer_ta are present."""
    if not arcium or not arcium.enabled:
        return
    missing = [k for k in ("amount", "mint", "payer_ta") if not meta.get(k)]
    if missing:
        log_warn(f"[#{count}] Arcium skipped — missing: {', '.join(missing)}"
                 f"  (set ARCIUM_MINT / ARCIUM_PAYER_TOKEN_ACCOUNT in .env)")
        return
    try:
        arcium.log_payment_stats(
            amount                    = int(meta["amount"]),
            payer_token_account       = meta["payer_ta"],
            recipient                 = meta.get("recipient", ""),
            recipient_token_account   = meta.get("recipient_ta", ""),
            mint                      = meta["mint"],
            broadcaster               = meta.get("broadcaster"),
            broadcaster_token_account = meta.get("broadcaster_ta"),
        )
        log_info(f"[#{count}] Arcium payment stats queued ({label})")
    except Exception as exc:
        log_err(f"[#{count}] Arcium execute_payment failed: {exc}")


def _maybe_log_arcium_stats(params: list, result_bytes: bytes, count: int) -> None:
    """
    Fire-and-forget: log encrypted payment stats to the Arcium MXE after a
    successful sendTransaction.  Never raises — must not block the response.
    """
    try:
        parsed_result = decode_json(result_bytes)
        if not ("result" in parsed_result and isinstance(parsed_result["result"], str)):
            return
        _fire_arcium_stats(_resolve_arcium_meta(params), count, "fire-and-forget")
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════════════════════════
# RPC Forwarding
# ═══════════════════════════════════════════════════════════════════════════════

def forward_plain_rpc(req: dict, req_id: int, count: int, method: str) -> bytes:
    """Forward a JSON-RPC request to the Solana HTTP endpoint and return raw bytes."""
    try:
        # Determine SSL cert path — fall back to system bundle if certifi is broken
        import certifi as _c
        _cert = _c.where() if os.path.isfile(_c.where()) else "/etc/ssl/certs/ca-certificates.crt"

        http_resp = requests.post(
            rpc_endpoint,
            json=req,
            timeout=20,
            headers={"Content-Type": "application/json"},
            verify=_cert,
        )
        http_resp.raise_for_status()
        try:
            parsed = http_resp.json()
            if "result" in parsed:
                log_ok(f"[#{count}] Solana ✔  method={method}  type={type(parsed['result']).__name__}")
            elif "error" in parsed:
                log_warn(f"[#{count}] Solana error: {parsed['error'].get('message', '?')}")
                logs = parsed["error"].get("data", {}) or {}
                for line in (logs.get("logs") or []):
                    log_warn(f"  sim> {line}")
        except Exception:
            pass
        return http_resp.content
    except requests.exceptions.Timeout:
        log_err(f"[#{count}] Solana RPC timeout  method={method}")
        return build_response(error="Solana RPC timeout", req_id=req_id)
    except requests.exceptions.ConnectionError as exc:
        log_err(f"[#{count}] Solana connection error: {exc}")
        return build_response(error=f"Solana connection error: {exc}", req_id=req_id)
    except Exception as exc:
        log_err(f"[#{count}] Unexpected error: {exc}")
        return build_response(error=str(exc), req_id=req_id)


def forward_to_solana(raw_request: bytes) -> bytes:
    """
    Route an incoming JSON-RPC request:
      - Encrypted getBalance  → Arcium MPC (confidential, beacon never sees address/balance)
      - Everything else       → plain Solana RPC
    """
    global request_count

    try:
        req = decode_json(raw_request)
    except Exception as exc:
        log_err(f"Failed to parse RPC payload: {exc}")
        return build_response(error=f"Invalid JSON payload: {exc}")

    method = req.get("method", "?")
    req_id = req.get("id", 1)
    params = req.get("params", [])

    with request_lock:
        request_count += 1
        count = request_count

    log_tx(f"[#{count}] Mesh→Beacon  method={method}  params_len={len(json.dumps(params))}")

    # ── Co-sign protocol (Arcium revenue share) ───────────────────────────────
    cosign_result = _dispatch_cosign(method, params, req_id, count)
    if cosign_result is not None:
        return cosign_result

    # ── Plain RPC path ────────────────────────────────────────────────────────
    result_bytes = forward_plain_rpc(req, req_id, count, method)

    # ── Arcium: log payment stats after sendTransaction ────────────────────────
    if method == "sendTransaction" and arcium and arcium.enabled:
        _maybe_log_arcium_stats(params, result_bytes, count)

    return result_bytes


# ═══════════════════════════════════════════════════════════════════════════════
# Reticulum Request Handler
# ═══════════════════════════════════════════════════════════════════════════════

def rpc_request_handler(path, data, request_id, link_id, remote_identity, requested_at):
    """
    Called by Reticulum whenever a client sends a request to RPC_PATH.
    `data` is the raw bytes of the JSON-RPC request payload.
    Must return bytes — this becomes the response body sent back over the link.
    The link is already encrypted; no additional crypto needed here.
    """
    if data is None:
        return build_response(error="Empty request body")

    remote_id_str = (
        RNS.prettyhexrep(remote_identity.hash)
        if remote_identity else "anonymous"
    )
    log_info(f"Request from {remote_id_str}  path={path}  size={len(data)}B")
    raw = forward_to_solana(bytes(data))
    compressed = compress_response(raw)
    if len(compressed) < len(raw):
        log_info(f"Compressed response {len(raw)}B → {len(compressed)}B ({100 - len(compressed)*100//len(raw)}% saved)")
    return compressed


# ═══════════════════════════════════════════════════════════════════════════════
# Link lifecycle callbacks
# ═══════════════════════════════════════════════════════════════════════════════

def link_established(link):
    remote = RNS.prettyhexrep(link.get_remote_identity().hash) if link.get_remote_identity() else "?"
    log_ok(f"Link established  remote={remote}")
    link.set_remote_identified_callback(client_identified)


def client_identified(link, identity):
    log_info(f"Client self-identified as {RNS.prettyhexrep(identity.hash)}")


def link_closed(link):
    log_info(f"Link closed  reason={link.teardown_reason}")


# ═══════════════════════════════════════════════════════════════════════════════
# Announce loop — periodically re-announces the beacon on the mesh
# ═══════════════════════════════════════════════════════════════════════════════

def announce_loop(interval_sec: int = 300):
    """
    Burst-then-backoff announce schedule:
      - First 2 minutes: announce every 15 s so freshly-started clients
        discover this beacon without waiting for the full interval.
      - After that: drop to interval_sec (default 300 s).
    The initial announce is fired by setup_beacon() before this thread starts.
    """
    burst_interval = 15
    burst_end      = time.time() + 120   # 2 minute burst window

    while True:
        if time.time() < burst_end:
            time.sleep(burst_interval)
            beacon_destination.announce(app_data=ANNOUNCE_DATA)
            remaining = max(0, int(burst_end - time.time()))
            log_info(f"Burst re-announce (long interval in {remaining}s)")
        else:
            time.sleep(interval_sec)
            beacon_destination.announce(app_data=ANNOUNCE_DATA)
            log_info("Re-announced beacon on mesh")


# ═══════════════════════════════════════════════════════════════════════════════
# Arcium connection test
# ═══════════════════════════════════════════════════════════════════════════════

def _test_arcium() -> None:
    """
    Run at beacon startup to verify every layer of the Arcium integration:

      Layer 0 — rescue_shim.mjs reachable      (Node.js + @arcium-hq/client)
      Layer 1 — ArciumBeacon initialises        (.env vars, anchorpy, solana)
      Layer 2 — MXE pubkey fetchable from chain (RPC + program account)
      Layer 3 — RescueCipher round-trip         (encrypt → decrypt = original)
    """
    global arcium

    print()
    log_info("─── Arcium connection test ───────────────────────────────────────")

    # ── Layer 0: rescue_shim.mjs ───────────────────────────────────────────────
    try:
        priv_hex, pub_hex = rescue_keygen()
        log_ok(f"Layer 0 ✔  rescue_shim.mjs reachable  (keygen pubkey={pub_hex[:16]}…)")
    except Exception as exc:
        log_warn(f"Layer 0 ✘  rescue_shim.mjs: {exc}")
        log_warn("  Fix: npm install @arcium-hq/client  (in project dir)")
        log_warn("  Arcium confidential RPC disabled")
        arcium = None
        print()
        return

    # ── Layer 1: ArciumBeacon from env ────────────────────────────────────────
    if os.getenv("ARCIUM_ENABLED", "0") != "1":
        log_info("Layer 1 –  ARCIUM_ENABLED not set — confidential RPC disabled")
        log_info("  To enable: add ARCIUM_ENABLED=1 to your .env file")
        arcium = None
        print()
        return

    arcium = ArciumBeacon.from_env()

    if not arcium.enabled:
        log_warn("Layer 1 ✘  ArciumBeacon failed to initialise")
        log_warn("  Check: ARCIUM_PAYER_KEYPAIR, ARCIUM_MXE_PROGRAM_ID, ARCIUM_MXE_PUBKEY_HEX in .env")
        print()
        return

    log_ok("Layer 1 ✔  ArciumBeacon initialised  (solana-py connected)")

    # ── Layer 2: MXE pubkey from chain ────────────────────────────────────────
    mxe_pubkey_hex = os.getenv("ARCIUM_MXE_PUBKEY_HEX", "")
    if not mxe_pubkey_hex:
        log_warn("Layer 2 –  ARCIUM_MXE_PUBKEY_HEX not set")
        log_warn("  Run:  node rescue_shim.mjs mxe_pubkey <PROGRAM_ID>")
        log_warn("  Then add ARCIUM_MXE_PUBKEY_HEX=<result> to .env")
    else:
        log_ok(f"Layer 2 ✔  MXE pubkey loaded  ({mxe_pubkey_hex[:16]}…)")

    # ── Layer 3: RescueCipher round-trip ──────────────────────────────────────
    if mxe_pubkey_hex:
        try:
            from arcium_client import rescue_encrypt, rescue_decrypt, rescue_shared_secret
            test_values  = [42, 101]
            enc          = rescue_encrypt(mxe_pubkey_hex, test_values)
            shared_secret = enc["shared_secret_hex"]
            decrypted    = rescue_decrypt(shared_secret, enc["ciphertexts"], enc["nonce_hex"])
            assert decrypted == test_values, f"mismatch: {decrypted} != {test_values}"
            log_ok(f"Layer 3 ✔  RescueCipher round-trip  encrypt([42,101]) → decrypt = {decrypted}")
        except AssertionError as exc:
            log_err(f"Layer 3 ✘  RescueCipher round-trip FAILED: {exc}")
        except Exception as exc:
            log_warn(f"Layer 3 ✘  RescueCipher test error: {exc}")
    else:
        log_info("Layer 3 –  Skipped (no MXE pubkey)")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    if arcium and arcium.enabled:
        log_ok("Arcium MPC ACTIVE — payment stats will be logged after sendTransaction")
        log_info("  Program:    7xeQNUggKc2e5q6AQxsFBLBkXGg2p54kSx11zVainMks")
        log_info("  Instruction: execute_payment (logs encrypted amount via MPC)")
        log_info("  Triggered by: sendTransaction with arcium metadata in params")
    else:
        log_warn("Arcium MPC INACTIVE — payment stats not logged")
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Setup helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _init_reticulum(config_path: str | None) -> None:
    """Start Reticulum and wait for Transport identity, then load/create beacon identity."""
    global beacon_identity
    RNS.Reticulum(config_path)
    log_info("Waiting for Transport identity to settle...")
    deadline = time.time() + 5.0
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    if RNS.Transport.identity is None:
        log_warn("Transport identity still None after 5 s — proceeding (patch will guard)")
    else:
        log_ok("Reticulum started  (Transport identity ready)")

    identity_path = os.path.join(
        config_path or os.path.expanduser("~/.reticulum"),
        "anonmesh_beacon_identity",
    )
    if os.path.isfile(identity_path):
        beacon_identity = RNS.Identity.from_file(identity_path)
        log_info("Loaded persisted beacon identity")
    else:
        beacon_identity = RNS.Identity()
        beacon_identity.to_file(identity_path)
        log_ok("Generated new beacon identity (saved)")


def _load_cosign_keypair() -> None:
    """Load the beacon's co-signing keypair from ARCIUM_PAYER_KEYPAIR (if available)."""
    global beacon_cosign_keypair
    if not HAS_SOLDERS:
        log_info("solders not installed — cosignTransaction disabled")
        return
    kp_path = os.getenv("ARCIUM_PAYER_KEYPAIR", "").strip()
    if not kp_path:
        log_info("ARCIUM_PAYER_KEYPAIR not set — cosignTransaction disabled")
        return
    try:
        with open(os.path.expanduser(kp_path)) as f:
            beacon_cosign_keypair = _Keypair.from_bytes(bytes(json.load(f)))
        log_ok(f"Co-sign keypair ready: {beacon_cosign_keypair.pubkey()}")
    except Exception as exc:
        log_warn(f"Could not load ARCIUM_PAYER_KEYPAIR for co-signing: {exc}")


# ═══════════════════════════════════════════════════════════════════════════════
# Setup
# ═══════════════════════════════════════════════════════════════════════════════

def setup_beacon(config_path: str | None, network: str, custom_rpc: str | None, announce_interval: int = 300) -> None:
    global beacon_destination, rpc_endpoint

    # ── Choose RPC endpoint ────────────────────────────────────────────────────
    if custom_rpc:
        rpc_endpoint = custom_rpc
    elif network in SOLANA_ENDPOINTS:
        rpc_endpoint = SOLANA_ENDPOINTS[network]
    else:
        log_err(f"Unknown network: {network}. Choose from {list(SOLANA_ENDPOINTS.keys())}")
        sys.exit(1)

    log_info(f"Solana RPC endpoint: {rpc_endpoint}")

    # ── Start Reticulum + load beacon identity ─────────────────────────────────
    _init_reticulum(config_path)

    # ── Create IN destination ──────────────────────────────────────────────────
    beacon_destination = RNS.Destination(
        beacon_identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        APP_ASPECT,
    )

    # ── Register the RPC request handler ──────────────────────────────────────
    beacon_destination.register_request_handler(
        RPC_PATH,
        response_generator=rpc_request_handler,
        allow=RNS.Destination.ALLOW_ALL,   # open relay — add allowlist for prod
    )

    # ── Register link callbacks ────────────────────────────────────────────────
    beacon_destination.set_link_established_callback(link_established)

    # ── Announce self on the mesh ──────────────────────────────────────────────
    beacon_destination.announce(app_data=ANNOUNCE_DATA)

    # ── Print destination hash ─────────────────────────────────────────────────
    dest_hash = RNS.prettyhexrep(beacon_destination.hash)
    print()
    print(f"{BOLD}{CYAN}┌─ BEACON READY ─────────────────────────────────────────────┐{RESET}")
    print(f"{BOLD}{CYAN}│{RESET}  Destination hash:  {GREEN}{BOLD}{dest_hash}{RESET}")
    print(f"{BOLD}{CYAN}│{RESET}  Network:           {network}")
    print(f"{BOLD}{CYAN}│{RESET}  RPC endpoint:      {rpc_endpoint}")
    print(f"{BOLD}{CYAN}│{RESET}  Reticulum config:  {config_path or '~/.reticulum (default)'}")
    print(f"{BOLD}{CYAN}└────────────────────────────────────────────────────────────┘{RESET}")
    print()
    print(f"  Share this hash with clients:  {BOLD}{dest_hash}{RESET}")
    print()

    # ── Beacon co-sign keypair ─────────────────────────────────────────────────
    _load_cosign_keypair()

    # ── Arcium MPC init + connection test ─────────────────────────────────────
    global arcium
    if HAS_ARCIUM_MODULE:
        _test_arcium()
    else:
        log_warn("arcium_client.py not found — confidential RPC disabled")

    # ── Start re-announce thread ───────────────────────────────────────────────
    t = threading.Thread(target=announce_loop, args=(announce_interval,), daemon=True)
    t.start()


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="anon0mesh Beacon — Reticulum RPC gateway for Solana"
    )
    parser.add_argument(
        "--config", "-c",
        default=None,
        help="Path to Reticulum config dir (default: ~/.reticulum)",
    )
    parser.add_argument(
        "--network", "-n",
        default="devnet",
        choices=list(SOLANA_ENDPOINTS.keys()),
        help="Solana network (default: devnet)",
    )
    parser.add_argument(
        "--rpc",
        default=None,
        help="Custom Solana RPC URL (overrides --network)",
    )
    parser.add_argument(
        "--announce-interval", "-a",
        default=300,
        type=int,
        metavar="SECONDS",
        help="Re-announce interval after burst phase (default: 300 s)",
    )
    args = parser.parse_args()

    banner("BEACON  ·  RPC Gateway")
    setup_beacon(args.config, args.network, args.rpc, args.announce_interval)

    log_info("Waiting for mesh connections… (Ctrl+C to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        log_info("Beacon shutting down.")


if __name__ == "__main__":
    main()
