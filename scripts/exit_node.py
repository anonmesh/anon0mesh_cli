#!/usr/bin/env python3
"""
exit_node.py — Minimal Solana RPC exit gateway for anon0mesh
=============================================================
Accepts mesh links from relay nodes, forwards Solana JSON-RPC requests
to a real RPC endpoint, returns responses over the mesh. Logs every
relayed request with timestamp, method, response size, and RTT.

This is beacon.py stripped to the relay function — no wallet, no Arcium,
no REPL. Designed for the single-radio demo path (TCP localhost bridge)
and as the internet-facing half of a two-node LoRa deployment.

Usage:
  python scripts/exit_node.py                        # devnet, default config
  python scripts/exit_node.py --network mainnet      # mainnet-beta
  python scripts/exit_node.py --rpc https://my.node  # custom RPC
  python scripts/exit_node.py --config /path/to/conf # custom Reticulum config dir
  python scripts/exit_node.py --port 4343            # TCP listener for relay nodes
"""
from __future__ import annotations

import sys
import os
import time
import json
import argparse
import threading

# ── Add project root to path so shared.py is importable ──────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import requests

try:
    import RNS
except ImportError:
    print("Reticulum not installed. Run:  pip install rns")
    sys.exit(1)

from shared import (
    APP_NAME, APP_ASPECT, RPC_PATH, ANNOUNCE_DATA,
    SOLANA_ENDPOINTS, RNS_REQUEST_TIMEOUT,
    decode_json, build_response, compress_response,
    banner, log_info, log_ok, log_warn, log_err, log_tx,
    BOLD, CYAN, GREEN, RESET, DIM,
)

# ── Patch: guard synthesize_tunnel against Transport.identity race ───────────
_original_synthesize_tunnel = RNS.Transport.synthesize_tunnel.__func__ \
    if hasattr(RNS.Transport.synthesize_tunnel, "__func__") \
    else RNS.Transport.synthesize_tunnel

def _safe_synthesize_tunnel(interface):
    if RNS.Transport.identity is None:
        return
    try:
        _original_synthesize_tunnel(interface)
    except AttributeError:
        pass

RNS.Transport.synthesize_tunnel = staticmethod(_safe_synthesize_tunnel)

# ── State ────────────────────────────────────────────────────────────────────
exit_identity    = None
exit_destination = None
rpc_endpoint     = None
request_count    = 0
request_lock     = threading.Lock()

# ── Stats ────────────────────────────────────────────────────────────────────
stats_lock       = threading.Lock()
total_relayed    = 0
total_bytes_in   = 0
total_bytes_out  = 0
total_rtt_ms     = 0.0
start_time       = None


# ═════════════════════════════════════════════════════════════════════════════
# RPC Forwarding
# ═════════════════════════════════════════════════════════════════════════════

def forward_rpc(raw_request: bytes) -> tuple[bytes, str, float]:
    """
    Forward a JSON-RPC request to Solana and return (response_bytes, method, rtt_ms).
    """
    global request_count

    try:
        req = decode_json(raw_request)
    except Exception as exc:
        return build_response(error=f"Invalid JSON: {exc}"), "?", 0.0

    method = req.get("method", "?")
    req_id = req.get("id", 1)

    with request_lock:
        request_count += 1
        count = request_count

    log_tx(f"[#{count}] → {method}  ({len(raw_request)}B)")

    t0 = time.monotonic()
    try:
        http_resp = requests.post(
            rpc_endpoint,
            json=req,
            timeout=20,
            headers={"Content-Type": "application/json"},
        )
        http_resp.raise_for_status()
        result_bytes = http_resp.content
        rtt_ms = (time.monotonic() - t0) * 1000

        # Log result summary
        try:
            parsed = http_resp.json()
            if "result" in parsed:
                log_ok(f"[#{count}] ← {method}  {len(result_bytes)}B  {rtt_ms:.0f}ms")
            elif "error" in parsed:
                err_msg = parsed["error"].get("message", "?")
                log_warn(f"[#{count}] ← {method}  error: {err_msg}  {rtt_ms:.0f}ms")
        except Exception:
            log_ok(f"[#{count}] ← {method}  {len(result_bytes)}B  {rtt_ms:.0f}ms")

        return result_bytes, method, rtt_ms

    except requests.exceptions.Timeout:
        rtt_ms = (time.monotonic() - t0) * 1000
        log_err(f"[#{count}] ← {method}  TIMEOUT  {rtt_ms:.0f}ms")
        return build_response(error="Solana RPC timeout", req_id=req_id), method, rtt_ms
    except requests.exceptions.ConnectionError as exc:
        rtt_ms = (time.monotonic() - t0) * 1000
        log_err(f"[#{count}] ← {method}  connection error: {exc}")
        return build_response(error=f"Connection error: {exc}", req_id=req_id), method, rtt_ms
    except Exception as exc:
        rtt_ms = (time.monotonic() - t0) * 1000
        log_err(f"[#{count}] ← {method}  error: {exc}")
        return build_response(error=str(exc), req_id=req_id), method, rtt_ms


# ═════════════════════════════════════════════════════════════════════════════
# Reticulum Request Handler
# ═════════════════════════════════════════════════════════════════════════════

def rpc_request_handler(path, data, request_id, link_id, remote_identity, requested_at):
    """Handle incoming mesh RPC requests — forward to Solana, return compressed response."""
    global total_relayed, total_bytes_in, total_bytes_out, total_rtt_ms

    if data is None:
        return build_response(error="Empty request body")

    remote = (
        RNS.prettyhexrep(remote_identity.hash)
        if remote_identity else "anonymous"
    )
    data_bytes = bytes(data)
    log_info(f"Request from {remote}  size={len(data_bytes)}B")

    raw_response, method, rtt_ms = forward_rpc(data_bytes)
    compressed = compress_response(raw_response)

    saved = len(raw_response) - len(compressed)
    if saved > 0:
        pct = 100 - len(compressed) * 100 // len(raw_response)
        log_info(f"Compressed {len(raw_response)}B → {len(compressed)}B ({pct}% saved)")

    # Update stats
    with stats_lock:
        total_relayed  += 1
        total_bytes_in += len(data_bytes)
        total_bytes_out += len(compressed)
        total_rtt_ms   += rtt_ms

    return compressed


# ═════════════════════════════════════════════════════════════════════════════
# Link callbacks
# ═════════════════════════════════════════════════════════════════════════════

def link_established(link):
    remote = RNS.prettyhexrep(link.get_remote_identity().hash) if link.get_remote_identity() else "?"
    log_ok(f"Link established  remote={remote}")

def link_closed(link):
    log_info(f"Link closed  reason={link.teardown_reason}")


# ═════════════════════════════════════════════════════════════════════════════
# Announce loop
# ═════════════════════════════════════════════════════════════════════════════

def announce_loop(interval_sec: int = 300):
    """Burst-then-backoff: 15s for first 2 min, then interval_sec."""
    burst_end = time.time() + 120
    while True:
        if time.time() < burst_end:
            time.sleep(15)
        else:
            time.sleep(interval_sec)
        exit_destination.announce(app_data=ANNOUNCE_DATA)


# ═════════════════════════════════════════════════════════════════════════════
# Stats printer
# ═════════════════════════════════════════════════════════════════════════════

def stats_loop(interval_sec: int = 60):
    """Print relay stats every interval_sec."""
    while True:
        time.sleep(interval_sec)
        with stats_lock:
            if total_relayed == 0:
                continue
            avg_rtt = total_rtt_ms / total_relayed
            uptime = int(time.time() - start_time)
            h, m = divmod(uptime // 60, 60)
            log_info(
                f"Stats: {total_relayed} relayed | "
                f"{total_bytes_in}B in / {total_bytes_out}B out | "
                f"avg RTT {avg_rtt:.0f}ms | "
                f"uptime {h}h{m:02d}m"
            )


# ═════════════════════════════════════════════════════════════════════════════
# Setup
# ═════════════════════════════════════════════════════════════════════════════

def setup_exit_node(config_path: str | None, network: str, custom_rpc: str | None,
                    announce_interval: int = 300) -> None:
    global exit_identity, exit_destination, rpc_endpoint, start_time

    # ── RPC endpoint ──────────────────────────────────────────────────────────
    if custom_rpc:
        rpc_endpoint = custom_rpc
    elif network in SOLANA_ENDPOINTS:
        rpc_endpoint = SOLANA_ENDPOINTS[network]
    else:
        log_err(f"Unknown network: {network}")
        sys.exit(1)

    log_info(f"Solana RPC: {rpc_endpoint}")

    # ── Reticulum ─────────────────────────────────────────────────────────────
    RNS.Reticulum(config_path)
    deadline = time.time() + 5.0
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    if RNS.Transport.identity is None:
        log_warn("Transport identity not ready after 5s — proceeding")
    else:
        log_ok("Reticulum started")

    # ── Identity (persisted) ──────────────────────────────────────────────────
    identity_dir = config_path or os.path.expanduser("~/.reticulum")
    identity_path = os.path.join(identity_dir, "anonmesh_exit_identity")
    if os.path.isfile(identity_path):
        exit_identity = RNS.Identity.from_file(identity_path)
        log_info("Loaded persisted exit node identity")
    else:
        exit_identity = RNS.Identity()
        exit_identity.to_file(identity_path)
        log_ok("Generated new exit node identity (saved)")

    # ── Destination ───────────────────────────────────────────────────────────
    exit_destination = RNS.Destination(
        exit_identity,
        RNS.Destination.IN,
        RNS.Destination.SINGLE,
        APP_NAME,
        APP_ASPECT,
    )

    exit_destination.register_request_handler(
        RPC_PATH,
        response_generator=rpc_request_handler,
        allow=RNS.Destination.ALLOW_ALL,
    )
    exit_destination.set_link_established_callback(link_established)
    exit_destination.announce(app_data=ANNOUNCE_DATA)

    dest_hash = RNS.prettyhexrep(exit_destination.hash)
    start_time = time.time()

    print()
    print(f"{BOLD}{CYAN}┌─ EXIT NODE READY ──────────────────────────────────────────┐{RESET}")
    print(f"{BOLD}{CYAN}│{RESET}  Destination hash:  {GREEN}{BOLD}{dest_hash}{RESET}")
    print(f"{BOLD}{CYAN}│{RESET}  Network:           {network}")
    print(f"{BOLD}{CYAN}│{RESET}  RPC endpoint:      {rpc_endpoint}")
    print(f"{BOLD}{CYAN}│{RESET}  Config:            {config_path or '~/.reticulum (default)'}")
    print(f"{BOLD}{CYAN}└────────────────────────────────────────────────────────────┘{RESET}")
    print()
    print(f"  Share this hash with relay nodes:  {BOLD}{dest_hash}{RESET}")
    print()

    # ── Background threads ────────────────────────────────────────────────────
    threading.Thread(target=announce_loop, args=(announce_interval,), daemon=True).start()
    threading.Thread(target=stats_loop, args=(60,), daemon=True).start()


# ═════════════════════════════════════════════════════════════════════════════
# Entry point
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="anon0mesh Exit Node — minimal Solana RPC relay over Reticulum mesh"
    )
    parser.add_argument("--config", "-c", default=None,
                        help="Reticulum config dir (default: ~/.reticulum)")
    parser.add_argument("--network", "-n", default="devnet",
                        choices=list(SOLANA_ENDPOINTS.keys()),
                        help="Solana network (default: devnet)")
    parser.add_argument("--rpc", default=None,
                        help="Custom Solana RPC URL (overrides --network)")
    parser.add_argument("--announce-interval", "-a", default=300, type=int,
                        metavar="SECONDS", help="Re-announce interval (default: 300s)")
    args = parser.parse_args()

    banner("EXIT NODE  ·  RPC Relay")
    setup_exit_node(args.config, args.network, args.rpc, args.announce_interval)

    log_info("Waiting for relay connections… (Ctrl+C to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        with stats_lock:
            log_info(f"Shutting down. Relayed {total_relayed} requests.")


if __name__ == "__main__":
    main()
