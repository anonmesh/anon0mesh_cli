#!/usr/bin/env python3
"""
test_tcp_bridge.py — End-to-end integration test for the TCP localhost bridge.

Validates the full relay pipeline:
  test client → relay Reticulum (TCP:4243) → exit Reticulum → exit_node.py → Solana devnet

Uses Flasher's dual-instance configs (relay on 37430, exit on 37432).
No hardware required — TCP localhost simulates the LoRa link.

Usage:
  python scripts/test_tcp_bridge.py
"""
from __future__ import annotations

import os
import sys
import time
import json
import signal
import subprocess

RELAY_CONFIG = os.path.join(os.path.dirname(__file__), "..", "..", "config", "relay")
EXIT_CONFIG = os.path.join(os.path.dirname(__file__), "..", "..", "config", "exit")
EXIT_NODE_SCRIPT = os.path.join(os.path.dirname(__file__), "exit_node.py")
PROJECT_ROOT = os.path.join(os.path.dirname(__file__), "..")

# Validate paths
for path, label in [(RELAY_CONFIG, "relay config"), (EXIT_CONFIG, "exit config"),
                     (EXIT_NODE_SCRIPT, "exit_node.py")]:
    resolved = os.path.realpath(path)
    if not os.path.exists(resolved):
        print(f"FATAL: {label} not found at {resolved}")
        sys.exit(1)

RELAY_CONFIG = os.path.realpath(RELAY_CONFIG)
EXIT_CONFIG = os.path.realpath(EXIT_CONFIG)

# ── Colors ────────────────────────────────────────────────────────────────────
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
DIM = "\033[2m"
RESET = "\033[0m"

def log(prefix, color, msg):
    ts = time.strftime("%H:%M:%S")
    print(f"{DIM}{ts}{RESET} {color}{BOLD}{prefix}{RESET} {msg}")

def log_ok(msg):   log("✓", GREEN, msg)
def log_err(msg):  log("✗", RED, msg)
def log_info(msg): log("·", CYAN, msg)
def log_warn(msg): log("!", YELLOW, msg)


def main():
    procs = []

    def cleanup():
        for p in procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass

    def sighandler(sig, frame):
        log_warn("Interrupted — cleaning up")
        cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    print()
    print(f"{BOLD}{CYAN}═══ anon0mesh TCP Bridge Integration Test ═══{RESET}")
    print()

    # ── Step 1: Start relay rnsd ──────────────────────────────────────────────
    log_info(f"Starting relay rnsd (config: {RELAY_CONFIG})")
    relay_proc = subprocess.Popen(
        ["rnsd", "--config", RELAY_CONFIG],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    procs.append(relay_proc)
    time.sleep(3)

    if relay_proc.poll() is not None:
        out = relay_proc.stdout.read().decode() if relay_proc.stdout else ""
        log_err(f"Relay rnsd exited with code {relay_proc.returncode}")
        if out:
            print(out)
        cleanup()
        sys.exit(1)
    log_ok("Relay rnsd running (port 37430, TCP server 4243)")

    # ── Step 2: Start exit rnsd ───────────────────────────────────────────────
    log_info(f"Starting exit rnsd (config: {EXIT_CONFIG})")
    exit_rnsd = subprocess.Popen(
        ["rnsd", "--config", EXIT_CONFIG],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    procs.append(exit_rnsd)
    time.sleep(3)

    if exit_rnsd.poll() is not None:
        out = exit_rnsd.stdout.read().decode() if exit_rnsd.stdout else ""
        log_err(f"Exit rnsd exited with code {exit_rnsd.returncode}")
        if out:
            print(out)
        cleanup()
        sys.exit(1)
    log_ok("Exit rnsd running (port 37432, TCP client → 4243)")

    # ── Step 3: Start exit_node.py ────────────────────────────────────────────
    log_info("Starting exit_node.py on exit instance")
    exit_node = subprocess.Popen(
        [sys.executable, EXIT_NODE_SCRIPT, "--config", EXIT_CONFIG],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        cwd=PROJECT_ROOT,
    )
    procs.append(exit_node)

    # Wait for exit node to announce
    log_info("Waiting for exit node to announce (15s)...")
    time.sleep(15)

    if exit_node.poll() is not None:
        out = exit_node.stdout.read().decode() if exit_node.stdout else ""
        log_err(f"exit_node.py exited with code {exit_node.returncode}")
        if out:
            print(out[-2000:])
        cleanup()
        sys.exit(1)
    log_ok("exit_node.py running")

    # ── Step 4: Run test client ───────────────────────────────────────────────
    log_info("Starting test client on relay instance...")

    # The client connects to the relay Reticulum, discovers the exit node's
    # announce, establishes a link through the TCP bridge, and sends an RPC request.
    client_code = f'''
import sys, os, time, json
sys.path.insert(0, "{os.path.realpath(PROJECT_ROOT)}")
import RNS
from shared import APP_NAME, APP_ASPECT, RPC_PATH, ANNOUNCE_DATA, build_rpc, decompress_response

try:
    # Connect to relay instance
    r = RNS.Reticulum("{RELAY_CONFIG}")
    time.sleep(2)

    print("CLIENT: Reticulum connected to relay instance")

    # Create client identity
    client_id = RNS.Identity()

    # Look for exit node announces using handler object (RNS API requires this)
    dest_hash = None

    class AnnounceHandler:
        aspect_filter = None  # accept all aspects
        def received_announce(self, destination_hash, announced_identity, app_data):
            global dest_hash
            if app_data == ANNOUNCE_DATA:
                dest_hash = destination_hash
                print(f"CLIENT: Found exit node: {{RNS.prettyhexrep(destination_hash)}}")

    RNS.Transport.register_announce_handler(AnnounceHandler())

    # Wait for announce
    print("CLIENT: Waiting for exit node announce...")
    deadline = time.time() + 45
    while dest_hash is None and time.time() < deadline:
        time.sleep(0.5)

    if dest_hash is None:
        print("CLIENT: FAIL — no exit node announce received within 45s")
        sys.exit(1)

    # Establish link
    server_id = RNS.Identity.recall(dest_hash)
    if server_id is None:
        print("CLIENT: FAIL — cannot recall identity for exit node")
        sys.exit(1)

    dest = RNS.Destination(server_id, RNS.Destination.OUT, RNS.Destination.SINGLE, APP_NAME, APP_ASPECT)
    link = RNS.Link(dest)

    print("CLIENT: Establishing link...")
    deadline = time.time() + 15
    while link.status != RNS.Link.ACTIVE and time.time() < deadline:
        time.sleep(0.1)

    if link.status != RNS.Link.ACTIVE:
        print(f"CLIENT: FAIL — link not active, status={{link.status}}")
        sys.exit(1)

    print("CLIENT: Link active!")

    # Send getBalance request
    payload = build_rpc("getBalance", ["11111111111111111111111111111111"])
    print(f"CLIENT: Sending getBalance ({{len(payload)}}B)...")

    t0 = time.monotonic()
    receipt = link.request(RPC_PATH, data=payload, timeout=30)

    if receipt is False:
        print("CLIENT: FAIL — link.request() returned False (couldn't send)")
        sys.exit(1)

    print(f"CLIENT: Request sent, waiting for response (status={{receipt.get_status()}})...")

    # Poll using the correct RNS API
    while not receipt.concluded():
        time.sleep(0.1)

    rtt = (time.monotonic() - t0) * 1000
    status = receipt.get_status()
    print(f"CLIENT: Request concluded, status={{status}} (READY={{RNS.RequestReceipt.READY}}, FAILED={{RNS.RequestReceipt.FAILED}})")

    if status == RNS.RequestReceipt.READY:
        raw = receipt.get_response()
        if raw is None:
            print("CLIENT: FAIL — status READY but get_response() returned None")
            sys.exit(1)
        raw = bytes(raw)
        decompressed = decompress_response(raw)
        try:
            parsed = json.loads(decompressed)
            balance = parsed.get("result", {{}}).get("value", "?")
            print(f"CLIENT: SUCCESS — getBalance = {{balance}} lamports")
            print(f"CLIENT: Response: {{len(raw)}}B wire / {{len(decompressed)}}B decompressed, RTT {{rtt:.0f}}ms")
            sys.exit(0)
        except json.JSONDecodeError as e:
            print(f"CLIENT: FAIL — invalid JSON response: {{e}}")
            print(f"CLIENT: Raw (first 200): {{decompressed[:200]}}")
            sys.exit(1)
    else:
        print(f"CLIENT: FAIL — request failed with status={{status}}, RTT={{rtt:.0f}}ms")
        sys.exit(1)

except Exception as e:
    import traceback
    print(f"CLIENT: FAIL — unhandled exception: {{e}}")
    traceback.print_exc()
    sys.exit(1)
'''

    client_proc = subprocess.Popen(
        [sys.executable, "-u", "-c", client_code],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    procs.append(client_proc)

    # Stream client output
    for line in iter(client_proc.stdout.readline, b''):
        text = line.decode().rstrip()
        if text:
            prefix = "CLIENT" if text.startswith("CLIENT:") else "      "
            if "SUCCESS" in text:
                log_ok(text.replace("CLIENT: ", ""))
            elif "FAIL" in text:
                log_err(text.replace("CLIENT: ", ""))
            else:
                log_info(text.replace("CLIENT: ", ""))

    client_proc.wait(timeout=90)
    log_info(f"Client exited with returncode={client_proc.returncode}")

    # ── Results ───────────────────────────────────────────────────────────────
    print()
    if client_proc.returncode == 0:
        print(f"{BOLD}{GREEN}═══ TCP BRIDGE TEST PASSED ═══{RESET}")
        print()
        print("The full relay pipeline works:")
        print("  client → relay Reticulum → TCP:4243 → exit Reticulum → exit_node.py → Solana devnet")
        print()
        print("Next: plug in Heltec V3, swap TCP bridge for RNode LoRa interface.")
    else:
        print(f"{BOLD}{RED}═══ TCP BRIDGE TEST FAILED ═══{RESET}")
        # Dump exit_node.py output for debugging
        try:
            exit_node.terminate()
            out, _ = exit_node.communicate(timeout=3)
            if out:
                print()
                print("exit_node.py output (last 2000 chars):")
                print(out.decode()[-2000:])
        except Exception:
            pass

    cleanup()
    sys.exit(client_proc.returncode)


if __name__ == "__main__":
    main()
