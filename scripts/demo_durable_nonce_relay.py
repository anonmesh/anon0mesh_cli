#!/usr/bin/env python3
"""
demo_durable_nonce_relay.py — Hackathon demo: offline Solana tx over mesh
=========================================================================
Runs the exact stage-demo flow end-to-end:

  1. Generate ephemeral keypair
  2. Airdrop SOL on devnet
  3. Create a durable nonce account (tx never expires)
  4. Build an offline SOL transfer using the nonce
  5. Relay the signed tx through the mesh bridge
  6. Confirm the tx landed on-chain

Works against the TCP bridge today — swap --config for LoRa and it's the
same demo over radio.

Usage:
  # Start exit_node.py first, then:
  python scripts/demo_durable_nonce_relay.py --discover
  python scripts/demo_durable_nonce_relay.py --beacon <HASH>
  python scripts/demo_durable_nonce_relay.py --beacon <HASH> --recipient <ADDR>
  python scripts/demo_durable_nonce_relay.py --beacon <HASH> --config /path/to/conf
  python scripts/demo_durable_nonce_relay.py --beacon <HASH> --lamports 500000
"""
from __future__ import annotations

import sys
import os
import time
import json
import base64
import argparse
import tempfile
import threading

# ── Add project root to path ─────────────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    import RNS
except ImportError:
    print("Reticulum not installed. Run:  pip install rns")
    sys.exit(1)

try:
    from solders.keypair import Keypair
    from solders.pubkey import Pubkey
    from solders.transaction import Transaction
    from solders.system_program import (
        transfer, TransferParams,
        create_account, CreateAccountParams,
        initialize_nonce_account, InitializeNonceAccountParams,
        advance_nonce_account, AdvanceNonceAccountParams,
    )
    from solders.message import Message
    from solders.hash import Hash
except ImportError:
    print("solders not installed. Run:  pip install solders")
    sys.exit(1)

import state
from shared import (
    APP_NAME, APP_ASPECT, RPC_PATH, ANNOUNCE_DATA,
    build_rpc, decode_json, decompress_response, compress_response,
    banner, log_info, log_ok, log_warn, log_err, log_tx,
    BOLD, CYAN, GREEN, YELLOW, RED, DIM, RESET,
)
from mesh import BeaconPool, BeaconAnnounceHandler, start_reticulum

# ── Constants ─────────────────────────────────────────────────────────────────
NONCE_ACCOUNT_LENGTH = 80
AIRDROP_LAMPORTS     = 2_000_000_000   # 2 SOL
TRANSFER_LAMPORTS    = 100_000         # 0.0001 SOL default
CONFIRM_TIMEOUT      = 60             # seconds to wait for tx confirmation
CONFIRM_POLL         = 2              # seconds between confirmation polls


# ═════════════════════════════════════════════════════════════════════════════
# Timing helpers
# ═════════════════════════════════════════════════════════════════════════════

class Timer:
    """Accumulates named timing spans for the final report."""

    def __init__(self):
        self.steps: list[tuple[str, float]] = []
        self._start = time.monotonic()

    def mark(self, label: str, t0: float):
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.steps.append((label, elapsed_ms))
        return elapsed_ms

    def total_ms(self) -> float:
        return (time.monotonic() - self._start) * 1000

    def report(self) -> str:
        lines = [f"\n  {BOLD}{CYAN}── Demo Timing ──────────────────────────────────────{RESET}"]
        for label, ms in self.steps:
            if ms >= 1000:
                lines.append(f"  {label:<40s}  {BOLD}{ms/1000:.1f}s{RESET}")
            else:
                lines.append(f"  {label:<40s}  {BOLD}{ms:.0f}ms{RESET}")
        total = self.total_ms()
        lines.append(f"  {'─' * 52}")
        lines.append(f"  {'TOTAL':<40s}  {BOLD}{GREEN}{total/1000:.1f}s{RESET}")
        lines.append("")
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════════════
# RPC call via mesh pool
# ═════════════════════════════════════════════════════════════════════════════

def mesh_rpc(method: str, params=None) -> dict | None:
    """Send a JSON-RPC call through the mesh pool. Returns parsed response or None."""
    resp = state.pool.call(method, params)
    if resp is None:
        log_err(f"No response from beacon for {method}")
    return resp


def extract_result(resp: dict):
    """Unwrap Solana's {context, value} wrapper if present."""
    if resp is None:
        return None
    r = resp.get("result")
    if isinstance(r, dict) and "value" in r:
        return r["value"]
    return r


# ═════════════════════════════════════════════════════════════════════════════
# Demo steps
# ═════════════════════════════════════════════════════════════════════════════

def step_generate_keypair(tmpdir: str) -> tuple[Keypair, str]:
    """Generate an ephemeral keypair for the demo."""
    kp = Keypair()
    path = os.path.join(tmpdir, "demo_wallet.json")
    with open(path, "w") as f:
        json.dump(list(bytes(kp)), f)
    log_ok(f"Ephemeral keypair: {kp.pubkey()}")
    return kp, path


def step_airdrop(address: str, lamports: int, timer: Timer) -> str | None:
    """Request devnet airdrop and wait for confirmation."""
    log_info(f"Requesting airdrop of {lamports / 1e9:.1f} SOL to {address}...")
    t0 = time.monotonic()

    resp = mesh_rpc("requestAirdrop", [address, lamports])
    if resp is None:
        return None
    if "error" in resp:
        log_err(f"Airdrop failed: {resp['error']}")
        return None

    sig = extract_result(resp)
    if not sig:
        log_err(f"Airdrop returned no signature: {resp}")
        return None

    log_ok(f"Airdrop requested: {sig}")

    # Wait for confirmation
    confirmed = wait_for_confirmation(sig, "airdrop")
    ms = timer.mark("Airdrop + confirm", t0)
    if confirmed:
        log_ok(f"Airdrop confirmed in {ms/1000:.1f}s")
    else:
        log_warn("Airdrop not confirmed yet — proceeding anyway")
    return sig


def step_create_nonce(payer: Keypair, tmpdir: str, timer: Timer) -> tuple[Keypair, str] | tuple[None, None]:
    """Create a durable nonce account on devnet."""
    log_info("Creating durable nonce account...")
    t0 = time.monotonic()

    nonce_kp = Keypair()
    nonce_path = os.path.join(tmpdir, "demo_nonce.json")
    with open(nonce_path, "w") as f:
        json.dump(list(bytes(nonce_kp)), f)

    payer_pubkey = payer.pubkey()
    nonce_pubkey = nonce_kp.pubkey()

    # Get rent exemption minimum
    resp = mesh_rpc("getMinimumBalanceForRentExemption", [NONCE_ACCOUNT_LENGTH])
    if resp is None or "error" in resp:
        log_err(f"Failed to get rent exemption: {resp}")
        return None, None
    rent_lamports = extract_result(resp)
    log_info(f"Rent-exempt: {rent_lamports:,} lamports")

    # Get recent blockhash for the create tx
    resp = mesh_rpc("getLatestBlockhash", [{"commitment": "confirmed"}])
    if resp is None or "error" in resp:
        log_err(f"Failed to get blockhash: {resp}")
        return None, None
    bh_val = extract_result(resp)
    if isinstance(bh_val, dict):
        blockhash = bh_val.get("blockhash")
    else:
        blockhash = bh_val
    if not blockhash:
        log_err(f"No blockhash in response: {resp}")
        return None, None

    # Build create + init nonce tx
    create_ix = create_account(CreateAccountParams(
        from_pubkey=payer_pubkey,
        to_pubkey=nonce_pubkey,
        lamports=rent_lamports,
        space=NONCE_ACCOUNT_LENGTH,
        owner=Pubkey.from_string("11111111111111111111111111111111"),
    ))
    init_ix = initialize_nonce_account(InitializeNonceAccountParams(
        nonce_pubkey=nonce_pubkey,
        authority=payer_pubkey,
    ))

    bh = Hash.from_string(blockhash)
    msg = Message.new_with_blockhash([create_ix, init_ix], payer_pubkey, bh)
    tx = Transaction.new_unsigned(msg)
    tx.sign([payer, nonce_kp], bh)

    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    log_info(f"Nonce create tx: {len(tx_b64)} chars, sending via mesh...")

    resp = mesh_rpc("sendTransaction", [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}])
    if resp is None or "error" in resp:
        err = resp.get("error", "no response") if resp else "no response"
        log_err(f"Nonce create tx rejected: {err}")
        return None, None

    sig = resp.get("result", "?")
    log_ok(f"Nonce create tx sent: {sig}")

    confirmed = wait_for_confirmation(sig, "nonce create")
    ms = timer.mark("Create nonce account", t0)
    if confirmed:
        log_ok(f"Nonce account created in {ms/1000:.1f}s: {nonce_pubkey}")
    else:
        log_warn(f"Nonce tx not confirmed yet — proceeding")

    return nonce_kp, str(nonce_pubkey)


def step_fetch_nonce(nonce_pubkey_str: str, timer: Timer) -> str | None:
    """Fetch the current nonce value from chain."""
    log_info(f"Fetching nonce value for {nonce_pubkey_str[:16]}...")
    t0 = time.monotonic()

    resp = mesh_rpc("getAccountInfo", [
        nonce_pubkey_str,
        {"encoding": "jsonParsed", "commitment": "confirmed"},
    ])
    if resp is None or "error" in resp:
        log_err(f"Failed to fetch nonce account: {resp}")
        return None

    account = extract_result(resp)
    if account is None:
        log_err("Nonce account not found")
        return None

    try:
        parsed = account["data"]["parsed"]
        if parsed.get("type") != "initialized":
            log_err(f"Nonce not initialized: type={parsed.get('type')}")
            return None
        nonce_value = parsed["info"]["blockhash"]
    except (KeyError, TypeError) as exc:
        log_err(f"Could not parse nonce account: {exc}")
        return None

    ms = timer.mark("Fetch nonce value", t0)
    log_ok(f"Nonce value: {nonce_value[:16]}...  ({ms:.0f}ms)")
    return nonce_value


def step_sign_nonce_transfer(
    payer: Keypair,
    to_address: str,
    lamports: int,
    nonce_pubkey_str: str,
    nonce_value: str,
    timer: Timer,
) -> str | None:
    """Build and sign an offline SOL transfer using the durable nonce."""
    log_info(f"Signing offline transfer: {lamports:,} lamports → {to_address[:16]}...")
    t0 = time.monotonic()

    payer_pubkey = payer.pubkey()
    nonce_pubkey = Pubkey.from_string(nonce_pubkey_str)
    to_pubkey = Pubkey.from_string(to_address)
    nonce_hash = Hash.from_string(nonce_value)

    advance_ix = advance_nonce_account(AdvanceNonceAccountParams(
        nonce_pubkey=nonce_pubkey,
        authorized_pubkey=payer_pubkey,
    ))
    transfer_ix = transfer(TransferParams(
        from_pubkey=payer_pubkey,
        to_pubkey=to_pubkey,
        lamports=lamports,
    ))

    msg = Message.new_with_blockhash([advance_ix, transfer_ix], payer_pubkey, nonce_hash)
    tx = Transaction.new_unsigned(msg)
    tx.sign([payer], nonce_hash)

    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    ms = timer.mark("Sign offline tx (durable nonce)", t0)
    log_ok(f"Signed offline: {len(tx_b64)} chars  ({ms:.0f}ms)")
    log_info(f"{DIM}This tx never expires — relay it whenever ready{RESET}")
    return tx_b64


def step_relay_tx(tx_b64: str, timer: Timer) -> str | None:
    """Relay the signed transaction through the mesh."""
    log_info("Relaying signed tx through mesh bridge...")
    t0 = time.monotonic()

    resp = mesh_rpc("sendTransaction", [tx_b64, {"encoding": "base64", "preflightCommitment": "confirmed"}])
    if resp is None:
        return None
    if "error" in resp:
        err = resp["error"]
        if isinstance(err, dict):
            err = err.get("message", err)
        log_err(f"Transaction rejected: {err}")
        return None

    sig = resp.get("result", "?")
    ms = timer.mark("Relay tx via mesh", t0)
    log_ok(f"Relayed in {ms:.0f}ms: {sig}")
    return sig


def step_confirm_tx(sig: str, timer: Timer) -> bool:
    """Wait for on-chain confirmation."""
    log_info(f"Waiting for on-chain confirmation...")
    t0 = time.monotonic()

    confirmed = wait_for_confirmation(sig, "transfer")
    ms = timer.mark("On-chain confirmation", t0)
    if confirmed:
        log_ok(f"Confirmed in {ms/1000:.1f}s")
    else:
        log_warn(f"Not confirmed after {CONFIRM_TIMEOUT}s — check explorer")
    return confirmed


# ═════════════════════════════════════════════════════════════════════════════
# Confirmation poller
# ═════════════════════════════════════════════════════════════════════════════

def wait_for_confirmation(sig: str, label: str, timeout: float = CONFIRM_TIMEOUT) -> bool:
    """Poll getSignatureStatuses until confirmed or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(CONFIRM_POLL)
        resp = mesh_rpc("getSignatureStatuses", [[sig]])
        if resp is None:
            continue
        statuses = extract_result(resp)
        if statuses and isinstance(statuses, list) and statuses[0]:
            status = statuses[0]
            conf = status.get("confirmationStatus", "")
            if conf in ("confirmed", "finalized"):
                return True
            err = status.get("err")
            if err is not None:
                log_err(f"{label} tx failed on-chain: {err}")
                return False
    return False


# ═════════════════════════════════════════════════════════════════════════════
# Beacon discovery / connection
# ═════════════════════════════════════════════════════════════════════════════

def setup_mesh(args) -> None:
    """Initialize Reticulum and connect to beacon(s)."""
    start_reticulum(args.config)
    state.pool = BeaconPool(strategy="race", request_timeout=float(args.timeout))

    if args.discover:
        RNS.Transport.register_announce_handler(BeaconAnnounceHandler(state.pool))
        log_info("Discovery active — listening for beacon announces...")

    if args.beacon:
        log_info(f"Connecting to {len(args.beacon)} beacon(s)...")
        for h in args.beacon:
            ok = state.pool.add(h, label=h[:12] + "...", connect=True)
            if ok:
                log_ok(f"Connected: {h[:12]}...")
            else:
                log_warn(f"Failed: {h[:12]}...")

    if not state.pool.active_links():
        if args.discover:
            log_info("Waiting for beacon discovery (60s)...")
            deadline = time.time() + 60
            while not state.pool.active_links() and time.time() < deadline:
                time.sleep(0.5)

    if not state.pool.active_links():
        log_err("No active beacons — cannot proceed")
        sys.exit(1)

    print(state.pool.status_table())


# ═════════════════════════════════════════════════════════════════════════════
# Main
# ═════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="anon0mesh Demo — durable nonce SOL transfer over mesh"
    )
    parser.add_argument("--beacon", "-b", nargs="+", metavar="HASH", default=[],
                        help="Beacon/exit node destination hash(es)")
    parser.add_argument("--discover", "-d", action="store_true",
                        help="Auto-discover beacons via mesh announces")
    parser.add_argument("--config", "-c", default=None,
                        help="Reticulum config dir")
    parser.add_argument("--timeout", "-t", default=30, type=int,
                        help="RPC request timeout in seconds")
    parser.add_argument("--recipient", "-r", default=None, metavar="ADDRESS",
                        help="SOL recipient address (default: send to self)")
    parser.add_argument("--lamports", "-l", default=TRANSFER_LAMPORTS, type=int,
                        help=f"Lamports to transfer (default: {TRANSFER_LAMPORTS:,})")
    parser.add_argument("--airdrop", default=AIRDROP_LAMPORTS, type=int,
                        help=f"Airdrop amount in lamports (default: {AIRDROP_LAMPORTS:,})")
    args = parser.parse_args()

    if not args.beacon and not args.discover:
        parser.error("Provide --beacon HASH and/or --discover")

    banner("DEMO  ·  Durable Nonce Relay")

    print(f"  {BOLD}Demo flow:{RESET}")
    print(f"  1. Generate keypair")
    print(f"  2. Airdrop {args.airdrop / 1e9:.1f} SOL (devnet)")
    print(f"  3. Create durable nonce account")
    print(f"  4. Sign offline transfer ({args.lamports:,} lamports)")
    print(f"  5. Relay through mesh")
    print(f"  6. Confirm on-chain")
    print()

    # ── Setup ──────────────────────────────────────────────────────────────────
    timer = Timer()
    setup_mesh(args)

    with tempfile.TemporaryDirectory(prefix="anon0mesh_demo_") as tmpdir:
        # ── Step 1: Generate keypair ───────────────────────────────────────────
        print(f"\n  {BOLD}{CYAN}━━━ Step 1: Generate Keypair ━━━{RESET}")
        t0 = time.monotonic()
        payer, wallet_path = step_generate_keypair(tmpdir)
        payer_addr = str(payer.pubkey())
        recipient = args.recipient or payer_addr  # send to self if no recipient
        timer.mark("Generate keypair", t0)

        if recipient == payer_addr:
            log_info("No --recipient specified — sending to self")

        # ── Step 2: Airdrop ────────────────────────────────────────────────────
        print(f"\n  {BOLD}{CYAN}━━━ Step 2: Airdrop ━━━{RESET}")
        sig = step_airdrop(payer_addr, args.airdrop, timer)
        if sig is None:
            log_err("Airdrop failed — cannot continue")
            sys.exit(1)

        # Wait for balance to arrive (devnet propagation can lag)
        bal = 0
        for attempt in range(15):
            resp = mesh_rpc("getBalance", [payer_addr, {"commitment": "confirmed"}])
            if resp and "result" in resp:
                bal = extract_result(resp)
                if isinstance(bal, int) and bal > 0:
                    log_ok(f"Balance: {bal / 1e9:.9f} SOL")
                    break
            if attempt < 14:
                time.sleep(2)
        if not bal:
            log_warn("Balance still 0 after retries — proceeding anyway")

        # ── Step 3: Create nonce account ───────────────────────────────────────
        print(f"\n  {BOLD}{CYAN}━━━ Step 3: Create Durable Nonce Account ━━━{RESET}")
        nonce_kp, nonce_pubkey_str = step_create_nonce(payer, tmpdir, timer)
        if nonce_kp is None:
            log_err("Nonce account creation failed — cannot continue")
            sys.exit(1)

        # Small delay for state to settle
        time.sleep(2)

        # Fetch nonce value
        nonce_value = step_fetch_nonce(nonce_pubkey_str, timer)
        if nonce_value is None:
            log_err("Could not fetch nonce value — cannot continue")
            sys.exit(1)

        # ── Step 4: Sign offline transfer ──────────────────────────────────────
        print(f"\n  {BOLD}{CYAN}━━━ Step 4: Sign Offline Transfer (Durable Nonce) ━━━{RESET}")
        tx_b64 = step_sign_nonce_transfer(
            payer, recipient, args.lamports,
            nonce_pubkey_str, nonce_value, timer,
        )
        if tx_b64 is None:
            log_err("Signing failed — cannot continue")
            sys.exit(1)

        # ── Step 5: Relay through mesh ─────────────────────────────────────────
        print(f"\n  {BOLD}{CYAN}━━━ Step 5: Relay Through Mesh ━━━{RESET}")
        relay_sig = step_relay_tx(tx_b64, timer)
        if relay_sig is None:
            log_err("Relay failed — tx may still be valid, try manually")
            sys.exit(1)

        # ── Step 6: Confirm on-chain ───────────────────────────────────────────
        print(f"\n  {BOLD}{CYAN}━━━ Step 6: Confirm On-Chain ━━━{RESET}")
        confirmed = step_confirm_tx(relay_sig, timer)

        # ── Final report ───────────────────────────────────────────────────────
        print(timer.report())

        if confirmed:
            print(f"  {GREEN}{BOLD}{'═' * 52}{RESET}")
            print(f"  {GREEN}{BOLD}  DEMO COMPLETE — Transaction confirmed on-chain!{RESET}")
            print(f"  {GREEN}{BOLD}{'═' * 52}{RESET}")
        else:
            print(f"  {YELLOW}{BOLD}Demo finished — tx sent but not yet confirmed{RESET}")

        print(f"\n  Payer:     {BOLD}{payer_addr}{RESET}")
        print(f"  Recipient: {BOLD}{recipient}{RESET}")
        print(f"  Nonce:     {BOLD}{nonce_pubkey_str}{RESET}")
        print(f"  Amount:    {BOLD}{args.lamports:,} lamports{RESET}")
        print(f"  Signature: {BOLD}{GREEN}{relay_sig}{RESET}")
        print(f"  Explorer:  {DIM}https://explorer.solana.com/tx/{relay_sig}?cluster=devnet{RESET}")
        print()

    state.pool.teardown_all()


if __name__ == "__main__":
    main()
