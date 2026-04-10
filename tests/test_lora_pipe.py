#!/usr/bin/env python3
"""
test_lora_pipe.py — LoRa transport smoke test for anon0mesh
=============================================================
Verifies the Reticulum → RNode → LoRa pipeline works end-to-end.

Modes:
  --loopback   (default)  Single-process self-test over local transport.
               Proves Reticulum stack + request/response + compression.
               Also verifies RNode interface initializes (serial link up).

  --beacon     Start a test beacon that echoes payloads back.
               Run on device A, then run --client on device B.

  --client HASH  Connect to a remote test beacon and send test payloads.
               Use the destination hash printed by --beacon.

Usage:
  python tests/test_lora_pipe.py                         # loopback
  python tests/test_lora_pipe.py --config path/to/conf   # with RNode config
  python tests/test_lora_pipe.py --beacon                # two-radio: beacon
  python tests/test_lora_pipe.py --client <hash>         # two-radio: client
"""

import sys
import os
import time
import json
import argparse
import threading

# Add project root to path so we can import shared.py
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import RNS
except ImportError:
    print("Reticulum not installed. Run:  pip install rns")
    sys.exit(1)

from shared import (
    build_rpc, build_response, decode_json,
    compress_response, decompress_response,
    BOLD, CYAN, GREEN, YELLOW, RED, DIM, RESET,
)

# ── Test constants ────────────────────────────────────────────────────────────
TEST_APP_NAME  = "anonmesh"
TEST_ASPECT    = "lora_test"
TEST_PATH      = "/ping"
LINK_TIMEOUT   = 30  # seconds to wait for link establishment
REQUEST_TIMEOUT = 15  # seconds to wait for request response


# ── Logging ───────────────────────────────────────────────────────────────────

def ts():
    return f"{DIM}[{time.strftime('%H:%M:%S')}]{RESET}"

def log(color, icon, msg):
    print(f"{ts()} {color}{icon} {msg}{RESET}")

def info(msg):  log(CYAN, "ℹ", msg)
def ok(msg):    log(GREEN, "✔", msg)
def warn(msg):  log(YELLOW, "⚠", msg)
def err(msg):   log(RED, "✘", msg)


# ── Interface diagnostics ────────────────────────────────────────────────────

def report_interfaces(reticulum_instance):
    """Print status of all active Reticulum interfaces."""
    print()
    info("─── Interface Report ───────────────────────────────────────────")
    rnode_found = False
    for iface in RNS.Transport.interfaces:
        name = iface.name if hasattr(iface, "name") else type(iface).__name__
        itype = type(iface).__name__
        status = "UP" if getattr(iface, "online", True) else "DOWN"
        color = GREEN if status == "UP" else RED
        print(f"  {color}{status:4s}{RESET}  {BOLD}{name}{RESET}  ({itype})")

        # RNode-specific details
        if "RNode" in itype or "RNode" in name:
            rnode_found = True
            for attr in ("frequency", "bandwidth", "sf", "cr", "txpower",
                         "r_frequency", "r_bandwidth", "r_sf", "r_cr", "r_txpower"):
                val = getattr(iface, attr, None)
                if val is not None:
                    label = attr.replace("r_", "radio ") if attr.startswith("r_") else attr
                    if "frequency" in attr.lower() and isinstance(val, (int, float)):
                        print(f"         {label}: {val/1e6:.3f} MHz")
                    else:
                        print(f"         {label}: {val}")

            # RSSI / SNR from last packet (if available)
            rssi = getattr(iface, "r_rssi", None) or getattr(iface, "rssi", None)
            snr = getattr(iface, "r_snr", None) or getattr(iface, "snr", None)
            if rssi is not None:
                print(f"         RSSI: {rssi} dBm")
            if snr is not None:
                print(f"         SNR:  {snr} dB")

    if not rnode_found:
        warn("No RNode interface detected — LoRa radio not connected")
        info("To add LoRa: copy config/reticulum_rnode.conf to ~/.reticulum/config")
    print()


# ── Test payloads ─────────────────────────────────────────────────────────────

PAYLOADS = [
    ("getBalance",      build_rpc("getBalance", ["11111111111111111111111111111111"])),
    ("getSlot",         build_rpc("getSlot")),
    ("getTransaction",  build_rpc("getTransaction", [
        "5VERv8NMhHnLfMn5JX3tPunBMbpMYb9a7v5UaqGzMHTBJeFgSGJbhJMPcfEePp2PFBx3mYBm3UKEFhp7sa8v1gmk",
        {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0},
    ])),
]


# ── Echo handler (used by both loopback and --beacon) ─────────────────────────

def echo_handler(path, data, request_id, link_id, remote_identity, requested_at):
    """Echo the request back as a JSON-RPC response, with compression."""
    try:
        req = decode_json(bytes(data))
        method = req.get("method", "?")
        req_id = req.get("id", 1)
    except Exception:
        method = "?"
        req_id = 1

    # Build a mock response that's realistic in size
    if method == "getBalance":
        result = {"context": {"apiVersion": "2.0.15", "slot": 296000000}, "value": 1000000000}
    elif method == "getSlot":
        result = 296000000
    elif method == "getTransaction":
        # Simulate a medium-size transaction response (~900 bytes)
        result = {
            "slot": 296000000,
            "transaction": {
                "message": {
                    "accountKeys": [
                        "11111111111111111111111111111111",
                        "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                        "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL",
                    ],
                    "instructions": [{"programIdIndex": 2, "accounts": [0, 1], "data": "3Bxs4h24hBtQy9rw"}],
                    "recentBlockhash": "GHtXQBtXnBhkpBUmJGLEfgXdGHkpBUmJGLEfgXdGHm7K",
                },
                "signatures": [
                    "5VERv8NMhHnLfMn5JX3tPunBMbpMYb9a7v5UaqGzMHTBJeFgSGJbhJMPcfEePp2PFBx3mYBm3UKEFhp7sa8v1gmk"
                ],
            },
            "meta": {
                "err": None, "fee": 5000, "preBalances": [999995000, 0],
                "postBalances": [999990000, 5000],
                "logMessages": [
                    "Program 11111111111111111111111111111111 invoke [1]",
                    "Program 11111111111111111111111111111111 success",
                ],
            },
            "blockTime": 1712000000,
        }
    else:
        result = {"echo": True, "method": method}

    raw = build_response(result=result, req_id=req_id)
    compressed = compress_response(raw)
    ok(f"Echo: {method}  raw={len(raw)}B  compressed={len(compressed)}B"
       f"  saved={100 - len(compressed)*100//len(raw)}%")
    return compressed


# ── Send test payloads over a link ────────────────────────────────────────────

def run_tests(link):
    """Send each test payload and measure RTT."""
    print()
    info("─── Sending test payloads ──────────────────────────────────────")
    results = []

    for name, payload in PAYLOADS:
        info(f"Sending {name} ({len(payload)} bytes)...")
        t0 = time.time()
        receipt = link.request(TEST_PATH, data=payload, timeout=REQUEST_TIMEOUT)

        if receipt is False:
            err(f"{name}: link.request() returned False (couldn't send)")
            results.append((name, False, 0, 0, 0, 0))
            continue

        # Wait for response using correct RNS API
        while not receipt.concluded():
            time.sleep(0.05)

        rtt = (time.time() - t0) * 1000  # ms

        if receipt.get_status() != RNS.RequestReceipt.READY:
            err(f"{name}: TIMEOUT/FAILED after {rtt:.0f}ms (status={receipt.get_status()})")
            results.append((name, False, rtt, 0, 0, 0))
            continue

        raw_resp = bytes(receipt.get_response())
        decompressed = decompress_response(raw_resp)
        was_compressed = len(raw_resp) != len(decompressed)

        try:
            resp = json.loads(decompressed)
            has_result = "result" in resp
        except Exception:
            has_result = False

        ok(f"{name}: {rtt:.0f}ms  "
           f"sent={len(payload)}B  recv={len(raw_resp)}B"
           f"{'→' + str(len(decompressed)) + 'B' if was_compressed else ''}"
           f"  {'✔ valid' if has_result else '✘ invalid'}")
        results.append((name, has_result, rtt, len(payload), len(raw_resp), len(decompressed)))

    # Summary
    print()
    info("─── Summary ────────────────────────────────────────────────────")
    passed = sum(1 for r in results if r[1])
    total = len(results)
    color = GREEN if passed == total else (YELLOW if passed > 0 else RED)
    print(f"  {color}{BOLD}{passed}/{total} passed{RESET}")
    for name, success, rtt, sent, recv, decomp in results:
        icon = f"{GREEN}✔{RESET}" if success else f"{RED}✘{RESET}"
        comp_info = f"  (compressed {decomp}→{recv}B)" if recv != decomp else ""
        print(f"  {icon} {name:20s}  {rtt:6.0f}ms  sent={sent}B  recv={recv}B{comp_info}")
    print()

    return passed == total


# ── Loopback mode ─────────────────────────────────────────────────────────────

def run_loopback(config_path):
    """Single-process self-test: beacon + client in one process."""
    info("Mode: LOOPBACK (single-device self-test)")
    info(f"Config: {config_path or '~/.reticulum (default)'}")
    print()

    # Start Reticulum
    info("Starting Reticulum...")
    reticulum = RNS.Reticulum(config_path)

    # Wait for transport
    deadline = time.time() + 5
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    if RNS.Transport.identity:
        ok("Reticulum started (Transport identity ready)")
    else:
        warn("Transport identity not ready after 5s — continuing")

    report_interfaces(reticulum)

    # Create test beacon destination
    identity = RNS.Identity()
    destination = RNS.Destination(
        identity, RNS.Destination.IN, RNS.Destination.SINGLE,
        TEST_APP_NAME, TEST_ASPECT,
    )
    destination.register_request_handler(
        TEST_PATH, response_generator=echo_handler,
        allow=RNS.Destination.ALLOW_ALL,
    )
    dest_hash = RNS.prettyhexrep(destination.hash)
    ok(f"Test destination: {dest_hash}")

    # Announce and wait for local propagation
    destination.announce()
    info("Announced — waiting for local propagation...")
    time.sleep(1)

    # Open link to self
    info("Opening link to self...")
    link = RNS.Link(destination)

    # Wait for link establishment
    t0 = time.time()
    deadline = time.time() + LINK_TIMEOUT
    while link.status != RNS.Link.ACTIVE and time.time() < deadline:
        time.sleep(0.1)

    if link.status == RNS.Link.ACTIVE:
        ok(f"Link established in {(time.time()-t0)*1000:.0f}ms")
    else:
        err(f"Link failed — status: {link.status}")
        return False

    # Run tests
    success = run_tests(link)

    # Teardown
    link.teardown()
    return success


# ── Beacon mode (two-radio test) ──────────────────────────────────────────────

def run_beacon(config_path):
    """Start a test beacon that echoes payloads. Run --client on the other device."""
    info("Mode: BEACON (waiting for remote client)")
    info(f"Config: {config_path or '~/.reticulum (default)'}")
    print()

    reticulum = RNS.Reticulum(config_path)

    deadline = time.time() + 5
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    ok("Reticulum started")

    report_interfaces(reticulum)

    identity = RNS.Identity()
    destination = RNS.Destination(
        identity, RNS.Destination.IN, RNS.Destination.SINGLE,
        TEST_APP_NAME, TEST_ASPECT,
    )
    destination.register_request_handler(
        TEST_PATH, response_generator=echo_handler,
        allow=RNS.Destination.ALLOW_ALL,
    )

    dest_hash = RNS.prettyhexrep(destination.hash)
    destination.announce()

    print()
    print(f"  {BOLD}{CYAN}┌─ TEST BEACON READY ──────────────────────────────────┐{RESET}")
    print(f"  {BOLD}{CYAN}│{RESET}  Hash: {GREEN}{BOLD}{dest_hash}{RESET}")
    print(f"  {BOLD}{CYAN}│{RESET}  Run on other device:")
    print(f"  {BOLD}{CYAN}│{RESET}    python tests/test_lora_pipe.py --client {destination.hexhash}")
    print(f"  {BOLD}{CYAN}└──────────────────────────────────────────────────────┘{RESET}")
    print()

    info("Waiting for connections... (Ctrl+C to quit)")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print()
        info("Beacon stopped.")


# ── Client mode (two-radio test) ─────────────────────────────────────────────

def run_client(config_path, dest_hash_hex):
    """Connect to a remote test beacon and run test payloads."""
    info("Mode: CLIENT (connecting to remote beacon)")
    info(f"Config: {config_path or '~/.reticulum (default)'}")
    info(f"Target: {dest_hash_hex}")
    print()

    reticulum = RNS.Reticulum(config_path)

    deadline = time.time() + 5
    while RNS.Transport.identity is None and time.time() < deadline:
        time.sleep(0.1)
    ok("Reticulum started")

    report_interfaces(reticulum)

    # Resolve destination
    dest_hash = bytes.fromhex(dest_hash_hex)

    if not RNS.Transport.has_path(dest_hash):
        info("No path to destination — requesting...")
        RNS.Transport.request_path(dest_hash)

        path_deadline = time.time() + 30
        while not RNS.Transport.has_path(dest_hash) and time.time() < path_deadline:
            time.sleep(0.25)

        if not RNS.Transport.has_path(dest_hash):
            err("Could not find path to beacon. Is it running? Is LoRa configured?")
            return False

    ok("Path resolved")

    # Get identity from path
    remote_identity = RNS.Identity.recall(dest_hash)
    if remote_identity is None:
        err("Could not recall identity for destination")
        return False

    destination = RNS.Destination(
        remote_identity, RNS.Destination.OUT, RNS.Destination.SINGLE,
        TEST_APP_NAME, TEST_ASPECT,
    )

    # Open link
    info("Opening link...")
    link = RNS.Link(destination)

    t0 = time.time()
    deadline = time.time() + LINK_TIMEOUT
    while link.status != RNS.Link.ACTIVE and time.time() < deadline:
        time.sleep(0.1)

    if link.status == RNS.Link.ACTIVE:
        ok(f"Link established in {(time.time()-t0)*1000:.0f}ms")
    else:
        err(f"Link failed — status: {link.status}")
        return False

    # Check RSSI after link is up (from the interface that carried it)
    for iface in RNS.Transport.interfaces:
        rssi = getattr(iface, "r_rssi", None) or getattr(iface, "rssi", None)
        snr = getattr(iface, "r_snr", None) or getattr(iface, "snr", None)
        if rssi is not None:
            info(f"Link signal: RSSI={rssi} dBm" + (f"  SNR={snr} dB" if snr else ""))

    success = run_tests(link)
    link.teardown()
    return success


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="anon0mesh LoRa transport smoke test",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s                              # loopback self-test
  %(prog)s --config config/reticulum_rnode.conf  # test with RNode config
  %(prog)s --beacon                     # two-radio: start beacon
  %(prog)s --client <hash>              # two-radio: connect to beacon
""",
    )
    parser.add_argument("--config", "-c", default=None,
                        help="Reticulum config dir or file")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--loopback", action="store_true", default=True,
                      help="Single-device self-test (default)")
    mode.add_argument("--beacon", action="store_true",
                      help="Start test beacon for two-radio test")
    mode.add_argument("--client", metavar="HASH",
                      help="Connect to remote test beacon (hex hash)")
    args = parser.parse_args()

    print()
    print(f"  {BOLD}{CYAN}anon0mesh LoRa pipe test{RESET}")
    print()

    if args.client:
        success = run_client(args.config, args.client.replace(":", "").strip())
    elif args.beacon:
        run_beacon(args.config)
        return
    else:
        success = run_loopback(args.config)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
