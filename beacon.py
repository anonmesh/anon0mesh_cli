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
    decode_json, build_response,
    banner, log_info, log_ok, log_warn, log_err, log_tx,
    BOLD, CYAN, GREEN, RESET, DIM,
)

# ── State ──────────────────────────────────────────────────────────────────────
beacon_identity    = None
beacon_destination = None
rpc_endpoint       = None
request_count      = 0
request_lock       = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# RPC Forwarding
# ═══════════════════════════════════════════════════════════════════════════════

def forward_to_solana(raw_request: bytes) -> bytes:
    """
    Parse an incoming JSON-RPC request from the mesh, forward it to the
    Solana RPC endpoint, and return the raw JSON response bytes.
    Supports any Solana JSON-RPC method (getBalance, sendTransaction, …).
    """
    global request_count

    try:
        req = decode_json(raw_request)
    except Exception as exc:
        log_err(f"Failed to parse RPC payload: {exc}")
        return build_response(error=f"Invalid JSON payload: {exc}")

    method  = req.get("method", "?")
    req_id  = req.get("id", 1)
    params  = req.get("params", [])

    with request_lock:
        request_count += 1
        count = request_count

    log_tx(f"[#{count}] Mesh→Beacon  method={method}  params_len={len(json.dumps(params))}")

    try:
        http_resp = requests.post(
            rpc_endpoint,
            json=req,          # re-transmit the full JSON-RPC object as-is
            timeout=20,
            headers={"Content-Type": "application/json"},
        )
        http_resp.raise_for_status()
        result_bytes = http_resp.content          # raw bytes of the JSON response

        # Log result summary without leaking full balance data to terminal
        try:
            parsed = http_resp.json()
            if "result" in parsed:
                log_ok(f"[#{count}] Beacon→Solana ✔  method={method}  result_type={type(parsed['result']).__name__}")
            elif "error" in parsed:
                log_warn(f"[#{count}] Solana error: {parsed['error'].get('message', '?')}")
        except Exception:
            pass

        return result_bytes

    except requests.exceptions.Timeout:
        log_err(f"[#{count}] Solana RPC timeout for {method}")
        return build_response(error="Solana RPC timeout", req_id=req_id)
    except requests.exceptions.ConnectionError as exc:
        log_err(f"[#{count}] Solana connection error: {exc}")
        return build_response(error=f"Solana connection error: {exc}", req_id=req_id)
    except Exception as exc:
        log_err(f"[#{count}] Unexpected error: {exc}")
        return build_response(error=str(exc), req_id=req_id)


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
    return forward_to_solana(bytes(data))


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
# Setup
# ═══════════════════════════════════════════════════════════════════════════════

def setup_beacon(config_path: str | None, network: str, custom_rpc: str | None, announce_interval: int = 300) -> None:
    global beacon_identity, beacon_destination, rpc_endpoint

    # ── Choose RPC endpoint ────────────────────────────────────────────────────
    if custom_rpc:
        rpc_endpoint = custom_rpc
    elif network in SOLANA_ENDPOINTS:
        rpc_endpoint = SOLANA_ENDPOINTS[network]
    else:
        log_err(f"Unknown network: {network}. Choose from {list(SOLANA_ENDPOINTS.keys())}")
        sys.exit(1)

    log_info(f"Solana RPC endpoint: {rpc_endpoint}")

    # ── Start Reticulum ────────────────────────────────────────────────────────
    RNS.Reticulum(config_path)

    # Give Transport time to finish setting its identity before any TCPInterface
    # reconnect threads fire synthesize_tunnel. Without this, the first
    # reconnect after startup hits the Transport.identity = None race.
    # 1.5 s is enough on most hardware; raise to 3 s on a Raspberry Pi Zero.
    log_info("Waiting for Transport identity to settle...")
    deadline = time.time() + 5.0
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    if RNS.Transport.identity is None:
        log_warn("Transport identity still None after 5 s — proceeding (patch will guard)")
    else:
        log_ok("Reticulum started  (Transport identity ready)")

    # ── Load or create beacon identity ────────────────────────────────────────
    # Persist identity so the beacon hash stays stable across restarts.
    identity_path = os.path.join(
        config_path or os.path.expanduser("~/.reticulum"),
        "anonmesh_beacon_identity"
    )
    if os.path.isfile(identity_path):
        beacon_identity = RNS.Identity.from_file(identity_path)
        log_info("Loaded persisted beacon identity")
    else:
        beacon_identity = RNS.Identity()
        beacon_identity.to_file(identity_path)
        log_ok("Generated new beacon identity (saved)")

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
