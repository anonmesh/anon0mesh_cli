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
import argparse
from pathlib import Path

# ── Load .env before any module imports that may read env vars ─────────────────
_env = Path(__file__).parent / ".env"
if _env.exists():
    for _l in _env.read_text().splitlines():
        _l = _l.strip()
        if _l and not _l.startswith("#") and "=" in _l:
            import os as _os
            _k, _v = _l.split("=", 1)
            _os.environ.setdefault(_k.strip(), _v.strip())

# ── Module imports (after .env is loaded) ──────────────────────────────────────
import state
from shared import banner, log_info, log_ok, log_warn, log_err, YELLOW, GREEN, BOLD, DIM, RESET
from mesh import BeaconPool, BeaconAnnounceHandler, start_reticulum, connect_all_parallel
from rpc import (
    get_balance, confidential_get_balance,
    get_slot, get_recent_blockhash,
    send_transaction, simulate_transaction,
    get_nonce_account,
)
from wallet import (
    offline_sign_transfer, offline_sign_nonce_transfer,
    create_nonce_account, auto_load_wallet,
)
from menu import repl, _RELAY_PROMPT


# ═══════════════════════════════════════════════════════════════════════════════
# CLI parser
# ═══════════════════════════════════════════════════════════════════════════════

def _build_parser() -> argparse.ArgumentParser:
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
    parser.add_argument("--cbalance",    metavar="ADDRESS",
                        help="Confidential balance via Arcium MPC")
    parser.add_argument("--slot",        action="store_true")
    parser.add_argument("--blockhash",   action="store_true")
    parser.add_argument("--send-tx",     metavar="BASE64_TX")
    parser.add_argument("--simulate-tx", metavar="BASE64_TX")
    parser.add_argument("--sign-offline", action="store_true")
    parser.add_argument("--from",        dest="from_keypair", metavar="KEYPAIR_JSON")
    parser.add_argument("--to",          metavar="ADDRESS")
    parser.add_argument("--lamports",    type=int, default=0)
    parser.add_argument("--blockhash-value", metavar="HASH")
    # Durable nonce
    parser.add_argument("--create-nonce-account", action="store_true",
                        help="Create a new durable nonce account (requires --from)")
    parser.add_argument("--get-nonce",   metavar="PUBKEY",
                        help="Fetch the current nonce value from a nonce account")
    parser.add_argument("--nonce-account", metavar="PUBKEY",
                        help="Durable nonce account public key (for --sign-nonce-tx)")
    parser.add_argument("--nonce-auth",  metavar="KEYPAIR_JSON",
                        help="Nonce authority keypair (defaults to --from if omitted)")
    parser.add_argument("--nonce-keypair", metavar="KEYPAIR_JSON",
                        help="Nonce account keypair to use/create (--create-nonce-account)")
    parser.add_argument("--nonce-value", metavar="HASH",
                        help="Explicit nonce value — skips fetching from chain")
    parser.add_argument("--sign-nonce-tx", action="store_true",
                        help="Sign a SOL transfer using a durable nonce (requires --from, "
                             "--nonce-account, --to, --lamports)")
    return parser


# ═══════════════════════════════════════════════════════════════════════════════
# Beacon setup
# ═══════════════════════════════════════════════════════════════════════════════

def _connect_beacons(args, one_shot: bool) -> None:
    """Blocking connect for one-shot mode; background connect for interactive mode."""
    if one_shot:
        log_info(f"Connecting to {len(args.beacon)} beacon(s) in parallel...")
        connected = connect_all_parallel(args.beacon)
        log_ok(f"{connected}/{len(args.beacon)} beacon(s) connected")
        if connected == 0 and not args.discover:
            log_err("No beacons connected and discovery is off")
            sys.exit(1)
    else:
        for h in args.beacon:
            state.pool.add_background(h, label=h[:12] + "...")


def _wait_for_discover_beacon() -> None:
    """Block until a beacon is discovered (up to 60 s) for one-shot + discover-only mode."""
    log_info("Waiting for beacon announces (60 s)...")
    deadline = time.time() + 60
    while not state.pool.active_links() and time.time() < deadline:
        time.sleep(0.5)
    if not state.pool.active_links():
        log_warn("No beacons discovered yet — trying anyway")


def _setup_beacons(args, one_shot: bool) -> None:
    """Register the discover handler and kick off beacon connections."""
    if args.discover:
        import RNS
        RNS.Transport.register_announce_handler(BeaconAnnounceHandler(state.pool))
        log_info("Discovery active — listening for beacon announces...")

    if args.beacon:
        _connect_beacons(args, one_shot)

    if one_shot and args.discover and not args.beacon:
        _wait_for_discover_beacon()

    if one_shot:
        print(state.pool.status_table())


# ═══════════════════════════════════════════════════════════════════════════════
# One-shot command handlers
# ═══════════════════════════════════════════════════════════════════════════════

def _cmd_sign_offline(args) -> None:
    if not (args.from_keypair and args.to and args.lamports):
        log_err("--sign-offline requires --from, --to, --lamports")
        return
    tx_b64 = offline_sign_transfer(
        args.from_keypair, args.to, args.lamports, args.blockhash_value)
    if tx_b64 and input(_RELAY_PROMPT).strip().lower() == "y":
        send_transaction(tx_b64)


def _cmd_create_nonce(args) -> None:
    if not args.from_keypair:
        log_err("--create-nonce-account requires --from <KEYPAIR_JSON>")
        return
    create_nonce_account(args.from_keypair, args.nonce_keypair, None)


def _cmd_sign_nonce_tx(args) -> None:
    if not (args.from_keypair and args.nonce_account and args.to and args.lamports):
        log_err("--sign-nonce-tx requires --from, --nonce-account, --to, --lamports")
        return
    auth_path = args.nonce_auth or args.from_keypair
    tx_b64 = offline_sign_nonce_transfer(
        args.from_keypair, args.nonce_account, auth_path,
        args.to, args.lamports, args.nonce_value,
    )
    if tx_b64 and input(_RELAY_PROMPT).strip().lower() == "y":
        send_transaction(tx_b64)


_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


def _startup_spinner(timeout: float = 15.0) -> None:
    """
    Animate in-place until the first beacon connects, all pending
    connections fail, or `timeout` seconds elapse.
    Skipped immediately if a beacon is already active.
    """
    if state.pool.active_links():
        return

    deadline = time.time() + timeout
    i        = 0
    try:
        while time.time() < deadline:
            active  = len(state.pool.active_links())
            pending = state.pool.pending_count()
            if active > 0 or pending == 0:
                break
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            sys.stdout.write(
                f"\r  {YELLOW}{frame}{RESET}  Syncing with beacons  "
                f"{DIM}{pending} connecting  "
                f"{BOLD}{GREEN}{active} active{RESET}   "
            )
            sys.stdout.flush()
            time.sleep(0.08)
            i += 1
    except KeyboardInterrupt:
        pass

    sys.stdout.write("\r\033[K")   # erase spinner line
    sys.stdout.flush()


def _run_one_shot(args) -> None:
    if args.balance:              get_balance(args.balance)
    if args.cbalance:             confidential_get_balance(args.cbalance)
    if args.slot:                 get_slot()
    if args.blockhash:            get_recent_blockhash()
    if args.send_tx:              send_transaction(args.send_tx)
    if args.simulate_tx:          simulate_transaction(args.simulate_tx)
    if args.sign_offline:         _cmd_sign_offline(args)
    if args.get_nonce:            get_nonce_account(args.get_nonce)
    if args.create_nonce_account: _cmd_create_nonce(args)
    if args.sign_nonce_tx:        _cmd_sign_nonce_tx(args)


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    parser = _build_parser()
    args   = parser.parse_args()

    if not args.beacon and not args.discover:
        parser.error("Provide --beacon HASH [HASH ...] and/or --discover")

    state.identify_self   = not args.no_identify
    state.request_timeout = float(args.timeout)

    banner("CLIENT  ·  Multi-Beacon Mesh RPC")
    start_reticulum(args.config)
    auto_load_wallet()

    state.pool = BeaconPool(strategy=args.strategy, request_timeout=state.request_timeout)

    one_shot = any([args.balance, args.cbalance, args.slot, args.blockhash,
                    args.send_tx, args.simulate_tx, args.sign_offline,
                    args.create_nonce_account, args.get_nonce, args.sign_nonce_tx])

    _setup_beacons(args, one_shot)

    if one_shot:
        _run_one_shot(args)
    else:
        _startup_spinner()
        repl()

    state.pool.teardown_all()
    log_info("Client disconnected.")


if __name__ == "__main__":
    main()
