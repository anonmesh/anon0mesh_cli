"""
wallet.py — keypair management and offline transaction signing
===============================================================
Wallet generation/import, SOL transfers, and durable nonce account operations.
All functions here require solders (pip install solders).
"""

import json
import base64
from pathlib import Path

import state
from shared import (
    log_info, log_ok, log_warn, log_err,
    BOLD, GREEN, YELLOW, RED, RESET, DIM,
)

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
    from solders.instruction import Instruction, AccountMeta
    HAS_SOLDERS = True
except ImportError:
    HAS_SOLDERS = False

# Fixed size of a nonce account on Solana (defined by the runtime)
NONCE_ACCOUNT_LENGTH = 80


# ═══════════════════════════════════════════════════════════════════════════════
# Auto-detection
# ═══════════════════════════════════════════════════════════════════════════════

def auto_load_wallet() -> None:
    """
    Scan the working directory for wallet JSON files and load the most recently
    modified one into state.active_wallet.

    Search order:
      1. wallet.json          (exact name)
      2. wallet_*.json        (generated wallets, sorted newest-first)
    """
    if not HAS_SOLDERS:
        return

    candidates: list[Path] = []

    exact = Path("wallet.json")
    if exact.exists():
        candidates.append(exact)

    candidates += sorted(
        Path(".").glob("wallet_*.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    for path in candidates:
        try:
            with open(path) as f:
                kp = Keypair.from_bytes(bytes(json.load(f)))
            pubkey = str(kp.pubkey())
            state.active_wallet = {"pubkey": pubkey, "path": str(path)}
            log_ok(f"Wallet loaded: {pubkey}  ({path})")
            return
        except Exception:
            continue  # corrupt or unrecognised file — try next


# ═══════════════════════════════════════════════════════════════════════════════
# Wallet generation and import
# ═══════════════════════════════════════════════════════════════════════════════

def generate_wallet(save_path: str | None = None) -> str | None:
    """
    Generate a fresh Solana keypair and save it as a standard JSON key file.
    Returns the save path on success, or None on failure.
    """
    if not HAS_SOLDERS:
        log_err("Wallet generation requires: pip install solders")
        return None

    kp     = Keypair()
    pubkey = str(kp.pubkey())
    path   = save_path or f"wallet_{pubkey[:8]}.json"

    try:
        with open(path, "w") as f:
            json.dump(list(bytes(kp)), f)
    except OSError as exc:
        log_err(f"Failed to save keypair: {exc}")
        return None

    state.active_wallet = {"pubkey": pubkey, "path": path}

    print(f"\n  {GREEN}{BOLD}New wallet generated{RESET}")
    print(f"  Public key:  {BOLD}{pubkey}{RESET}")
    print(f"  Saved to:    {path}")
    print(f"\n  {YELLOW}{BOLD}IMPORTANT:{RESET} Back up {path!r} — it contains your private key.")
    print(f"  {DIM}Anyone with this file can spend funds from this wallet.{RESET}\n")
    return path


def import_wallet(raw: str, save_path: str) -> str | None:
    """
    Import a private key and save it as a standard Solana JSON keypair file.

    Accepted formats
    ----------------
    JSON array    [1, 2, ..., 64]   standard 64-byte Solana keypair
    Base58 string                   64-byte keypair encoded in base58
    Hex string    128 chars         64-byte keypair as hex
    Hex string     64 chars         32-byte seed (secret scalar only)

    Returns the public key string on success, or None on failure.
    """
    if not HAS_SOLDERS:
        log_err("Wallet import requires: pip install solders")
        return None

    raw = raw.strip()
    kp: "Keypair | None" = None

    if raw.startswith("["):
        try:
            kp = Keypair.from_bytes(bytes(json.loads(raw)))
        except Exception as exc:
            log_err(f"JSON array import failed: {exc}")
            return None

    elif all(c in "0123456789abcdefABCDEF" for c in raw):
        try:
            raw_bytes = bytes.fromhex(raw)
            if len(raw_bytes) == 64:
                kp = Keypair.from_bytes(raw_bytes)
            elif len(raw_bytes) == 32:
                kp = Keypair.from_seed(raw_bytes)
            else:
                log_err(f"Hex key must be 64 or 128 hex chars, got {len(raw)}")
                return None
        except Exception as exc:
            log_err(f"Hex import failed: {exc}")
            return None

    else:
        try:
            kp = Keypair.from_base58_string(raw)
        except Exception as exc:
            log_err(f"Base58 import failed: {exc}")
            return None

    try:
        with open(save_path, "w") as f:
            json.dump(list(bytes(kp)), f)
    except OSError as exc:
        log_err(f"Failed to save keypair: {exc}")
        return None

    pubkey = str(kp.pubkey())
    state.active_wallet = {"pubkey": pubkey, "path": save_path}

    print(f"\n  {GREEN}{BOLD}Wallet imported{RESET}")
    print(f"  Public key:  {BOLD}{pubkey}{RESET}")
    print(f"  Saved to:    {save_path}")
    print(f"\n  {YELLOW}{BOLD}IMPORTANT:{RESET} Back up {save_path!r} — it contains your private key.\n")
    return pubkey


# ═══════════════════════════════════════════════════════════════════════════════
# Nonce account discovery
# ═══════════════════════════════════════════════════════════════════════════════

def scan_nonce_accounts() -> list[dict]:
    """
    Scan CWD for nonce_*.json keypair files.
    Returns list of {"path": str, "pubkey": str}, sorted by filename.
    """
    if not HAS_SOLDERS:
        return []
    result = []
    for path in sorted(Path(".").glob("nonce_*.json")):
        try:
            with open(path) as f:
                kp = Keypair.from_bytes(bytes(json.load(f)))
            result.append({"path": str(path), "pubkey": str(kp.pubkey())})
        except Exception:
            continue
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Offline signing
# ═══════════════════════════════════════════════════════════════════════════════

def offline_sign_transfer(keypair_json_path, to_address, lamports, blockhash=None):
    if not HAS_SOLDERS:
        log_err("Offline signing requires: pip install solders")
        return None
    try:
        with open(keypair_json_path) as f:
            keypair = Keypair.from_bytes(bytes(json.load(f)))
    except Exception as exc:
        log_err(f"Failed to load keypair: {exc}")
        return None

    from_pubkey = keypair.pubkey()
    to_pubkey   = Pubkey.from_string(to_address)
    log_info(f"Signing  from={from_pubkey}  to={to_pubkey}  lamports={lamports}")

    if blockhash is None:
        from rpc import get_recent_blockhash
        blockhash = get_recent_blockhash()
        if blockhash is None:
            log_err("Could not obtain blockhash")
            return None

    ix  = transfer(TransferParams(from_pubkey=from_pubkey, to_pubkey=to_pubkey, lamports=lamports))
    msg = Message.new_with_blockhash([ix], from_pubkey, Hash.from_string(blockhash))
    tx  = Transaction.new_unsigned(msg)
    tx.sign([keypair], Hash.from_string(blockhash))
    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    log_ok(f"Transaction signed offline ({len(tx_b64)} chars)")
    print(f"\n  Signed TX: {BOLD}{tx_b64[:72]}...{RESET}\n")
    return tx_b64


def partial_sign_arcium_transfer(
    payer_keypair_path: str,
    beacon_pubkey_str: str,
    nonce_account_str: str,
    to_address: str,
    lamports: int,
    nonce_value: str | None = None,
) -> str | None:
    """
    Build and PARTIALLY sign a SOL transfer using a Durable Nonce + beacon co-sign.

    Why Durable Nonce (not recent blockhash):
      - Replay protection: nonce is consumed on-chain the moment the tx lands;
        the same tx bytes cannot be resubmitted.
      - Double-spend protection: a second submission with the same nonce fails.
      - No expiry: the tx stays valid while it travels the mesh waiting for
        the beacon's co-signature (regular blockhash txs expire after ~90 s).

    Transaction layout (Solana requires advanceNonceAccount as instruction 0):
      Instruction 0: SystemProgram.advanceNonceAccount(nonce_account, authority=payer)
      Instruction 1: SPL Memo("anon0mesh:relay") — lists payer AND beacon as signers,
                     making beacon's co-signature mandatory on-chain.
      Instruction 2: SystemProgram.transfer(payer → recipient, lamports)

    Signer slots: [payer (slot 0), beacon (slot 1)]
      - Client fills slot 0 via partial_sign.
      - Beacon fills slot 1 via cosignTransaction before submitting to Solana.

    Returns the partially-signed tx as base64, or None on failure.
    """
    if not HAS_SOLDERS:
        log_err("Requires: pip install solders")
        return None

    try:
        with open(payer_keypair_path) as f:
            payer = Keypair.from_bytes(bytes(json.load(f)))
    except Exception as exc:
        log_err(f"Failed to load keypair: {exc}")
        return None

    try:
        beacon_pubkey = Pubkey.from_string(beacon_pubkey_str)
        nonce_pubkey  = Pubkey.from_string(nonce_account_str)
        to_pubkey     = Pubkey.from_string(to_address)
    except Exception as exc:
        log_err(f"Invalid address: {exc}")
        return None

    payer_pubkey = payer.pubkey()

    if nonce_value is None:
        log_info("Fetching nonce value from chain...")
        from rpc import get_nonce_account
        nonce_info = get_nonce_account(nonce_account_str)
        if nonce_info is None:
            log_err("Could not fetch nonce account")
            return None
        nonce_value = nonce_info["nonce"]

    log_info("Building Arcium co-sign transfer (durable nonce)")
    log_info(f"  From:         {payer_pubkey}")
    log_info(f"  To:           {to_pubkey}")
    log_info(f"  Amount:       {lamports:,} lamports")
    log_info(f"  Nonce acct:   {nonce_pubkey}")
    log_info(f"  Nonce value:  {nonce_value}")
    log_info(f"  Beacon:       {beacon_pubkey}")

    # SPL Memo program — standard on all Solana clusters
    MEMO_PROGRAM = Pubkey.from_string("MemoSq4gqABAXKb96qnH8TysNcWxMyWCqXgDLGmfcHr")

    advance_ix = advance_nonce_account(AdvanceNonceAccountParams(
        nonce_pubkey=nonce_pubkey,
        authorized_pubkey=payer_pubkey,
    ))
    # Memo ix lists both payer and beacon as required signers.
    # This sets num_required_signatures = 2 on the message header,
    # so Solana rejects the tx unless both signatures are present.
    memo_ix = Instruction(
        program_id=MEMO_PROGRAM,
        accounts=[
            AccountMeta(pubkey=payer_pubkey,  is_signer=True, is_writable=False),
            AccountMeta(pubkey=beacon_pubkey, is_signer=True, is_writable=False),
        ],
        data=b"anon0mesh:relay",
    )
    transfer_ix = transfer(TransferParams(
        from_pubkey=payer_pubkey,
        to_pubkey=to_pubkey,
        lamports=lamports,
    ))

    nonce_hash = Hash.from_string(nonce_value)
    msg = Message.new_with_blockhash([advance_ix, memo_ix, transfer_ix], payer_pubkey, nonce_hash)
    tx  = Transaction.new_unsigned(msg)

    # Partial sign — fills payer slot (0) only; beacon slot (1) left zeroed
    tx.partial_sign([payer], nonce_hash)

    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    log_ok(f"Transaction partially signed ({len(tx_b64)} chars) — awaiting beacon co-sign")
    print(f"\n  Partial TX: {BOLD}{tx_b64[:72]}...{RESET}")
    print(f"  {DIM}Durable nonce — tx won't expire on the mesh.{RESET}")
    print(f"  {DIM}Sending to beacon for co-signature...{RESET}\n")
    return tx_b64


# ═══════════════════════════════════════════════════════════════════════════════
# Durable nonce account operations
# ═══════════════════════════════════════════════════════════════════════════════

def create_nonce_account(
    payer_keypair_path: str,
    nonce_keypair_path: str | None = None,
    authority_address: str | None = None,
) -> str | None:
    """
    Create a durable nonce account on Solana.

    Builds and sends a transaction with two instructions:
      1. SystemProgram.createAccount  — allocates the 80-byte account and funds it
      2. SystemProgram.initializeNonceAccount — sets the nonce authority

    Returns the nonce account public key string, or None on failure.
    """
    if not HAS_SOLDERS:
        log_err("Nonce account creation requires: pip install solders")
        return None

    try:
        with open(payer_keypair_path) as f:
            payer = Keypair.from_bytes(bytes(json.load(f)))
    except Exception as exc:
        log_err(f"Failed to load payer keypair: {exc}")
        return None

    if nonce_keypair_path:
        try:
            with open(nonce_keypair_path) as f:
                nonce_kp = Keypair.from_bytes(bytes(json.load(f)))
            log_info(f"Using existing nonce keypair: {nonce_kp.pubkey()}")
        except Exception as exc:
            log_err(f"Failed to load nonce keypair: {exc}")
            return None
    else:
        nonce_kp  = Keypair()
        save_path = f"nonce_{str(nonce_kp.pubkey())[:8]}.json"
        with open(save_path, "w") as f:
            json.dump(list(bytes(nonce_kp)), f)
        log_ok(f"Generated nonce keypair → {save_path}")

    payer_pubkey     = payer.pubkey()
    nonce_pubkey     = nonce_kp.pubkey()
    authority_pubkey = Pubkey.from_string(authority_address) if authority_address else payer_pubkey

    log_info(f"Payer:         {payer_pubkey}")
    log_info(f"Nonce account: {nonce_pubkey}")
    log_info(f"Authority:     {authority_pubkey}")

    from rpc import rpc_call, get_recent_blockhash, _extract_result

    log_info("Fetching minimum balance for rent exemption...")
    resp = rpc_call("getMinimumBalanceForRentExemption", [NONCE_ACCOUNT_LENGTH])
    if resp is None:
        log_err("No response from beacon"); return None
    if "error" in resp:
        log_err(f"RPC error: {resp['error']}"); return None
    rent_lamports = _extract_result(resp)
    if not isinstance(rent_lamports, int):
        log_err(f"Unexpected getMinimumBalanceForRentExemption response: {rent_lamports}")
        return None
    log_info(f"Rent-exempt minimum: {rent_lamports:,} lamports ({rent_lamports / 1e9:.9f} SOL)")

    blockhash = get_recent_blockhash()
    if blockhash is None:
        log_err("Could not fetch blockhash")
        return None

    create_ix = create_account(CreateAccountParams(
        from_pubkey=payer_pubkey,
        to_pubkey=nonce_pubkey,
        lamports=rent_lamports,
        space=NONCE_ACCOUNT_LENGTH,
        owner=Pubkey.from_string("11111111111111111111111111111111"),
    ))
    init_ix = initialize_nonce_account(InitializeNonceAccountParams(
        nonce_pubkey=nonce_pubkey,
        authority=authority_pubkey,
    ))

    bh  = Hash.from_string(blockhash)
    msg = Message.new_with_blockhash([create_ix, init_ix], payer_pubkey, bh)
    tx  = Transaction.new_unsigned(msg)
    tx.sign([payer, nonce_kp], bh)

    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    resp   = rpc_call("sendTransaction", [tx_b64, {"encoding": "base64"}])
    if resp is None:
        log_err("No response from beacon for sendTransaction")
        return None
    if "error" in resp:
        log_err(f"Transaction rejected: {resp['error'].get('message', resp['error'])}")
        return None

    sig = resp.get("result", "?")
    print(f"\n  {GREEN}{BOLD}Nonce account created!{RESET}")
    print(f"  Nonce account pubkey: {BOLD}{nonce_pubkey}{RESET}")
    print(f"  Authority:            {authority_pubkey}")
    print(f"  Funded:               {rent_lamports:,} lamports")
    print(f"  Signature:            {sig}")
    print(f"\n  {DIM}Fetch the nonce value:      get-nonce {nonce_pubkey}{RESET}")
    print(f"  {DIM}Sign with nonce:  sign-nonce-tx <payer> {nonce_pubkey} <auth> <to> <lamports>{RESET}\n")
    return str(nonce_pubkey)


def offline_sign_nonce_transfer(
    payer_keypair_path: str,
    nonce_account_str: str,
    authority_keypair_path: str,
    to_address: str,
    lamports: int,
    nonce_value: str | None = None,
) -> str | None:
    """
    Build and sign a SOL transfer using a durable nonce instead of a recent blockhash.
    The signed transaction does not expire.

    Transaction layout (required by the Solana runtime):
      Instruction 0: SystemProgram.advanceNonceAccount  ← MUST be first
      Instruction 1: SystemProgram.transfer
    """
    if not HAS_SOLDERS:
        log_err("Offline signing requires: pip install solders")
        return None

    try:
        with open(payer_keypair_path) as f:
            payer = Keypair.from_bytes(bytes(json.load(f)))
    except Exception as exc:
        log_err(f"Failed to load payer keypair: {exc}")
        return None

    try:
        with open(authority_keypair_path) as f:
            authority_kp = Keypair.from_bytes(bytes(json.load(f)))
    except Exception as exc:
        log_err(f"Failed to load authority keypair: {exc}")
        return None

    payer_pubkey     = payer.pubkey()
    authority_pubkey = authority_kp.pubkey()
    nonce_pubkey     = Pubkey.from_string(nonce_account_str)
    to_pubkey        = Pubkey.from_string(to_address)

    if nonce_value is None:
        log_info("Fetching current nonce value from chain...")
        from rpc import get_nonce_account
        nonce_info = get_nonce_account(nonce_account_str)
        if nonce_info is None:
            log_err("Could not fetch nonce account")
            return None
        nonce_value = nonce_info["nonce"]

    log_info(f"Payer:         {payer_pubkey}")
    log_info(f"To:            {to_pubkey}")
    log_info(f"Lamports:      {lamports:,}")
    log_info(f"Nonce account: {nonce_pubkey}")
    log_info(f"Nonce value:   {nonce_value}")

    advance_ix = advance_nonce_account(AdvanceNonceAccountParams(
        nonce_pubkey=nonce_pubkey,
        authorized_pubkey=authority_pubkey,
    ))
    transfer_ix = transfer(TransferParams(
        from_pubkey=payer_pubkey,
        to_pubkey=to_pubkey,
        lamports=lamports,
    ))

    nonce_hash = Hash.from_string(nonce_value)
    msg = Message.new_with_blockhash([advance_ix, transfer_ix], payer_pubkey, nonce_hash)
    tx  = Transaction.new_unsigned(msg)

    signers = [payer] if payer_pubkey == authority_pubkey else [payer, authority_kp]
    tx.sign(signers, nonce_hash)

    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    log_ok(f"Durable nonce transaction signed ({len(tx_b64)} chars)")
    print(f"\n  Signed TX (durable nonce): {BOLD}{tx_b64[:72]}...{RESET}")
    print(f"  {DIM}This transaction does not expire — relay it whenever ready.{RESET}\n")
    return tx_b64
