from __future__ import annotations
"""
wallet.py — keypair management and offline transaction signing
===============================================================
Wallet generation/import, SOL transfers, and durable nonce account operations.
All functions here require solders (pip install solders).
"""

import os
import json
import base64
import hashlib
import secrets as _secrets
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
_ERR_NONCE = "Could not fetch nonce account"


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



# ── SPL / ATA helpers ──────────────────────────────────────────────────────────

_TOKEN_PROGRAM     = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
_ATA_PROGRAM       = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
_SYSTEM_PROGRAM    = "11111111111111111111111111111111"
_MXE_PROGRAM_ID    = "7xeQNUggKc2e5q6AQxsFBLBkXGg2p54kSx11zVainMks"
_WSOL_MINT         = "So11111111111111111111111111111111111111112"
_USDC_MINT          = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"


def _get_ata(wallet: "Pubkey", mint: "Pubkey") -> "Pubkey":
    """Derive an Associated Token Account address (no RPC needed)."""
    ata, _ = Pubkey.find_program_address(
        [bytes(wallet), bytes(Pubkey.from_string(_TOKEN_PROGRAM)), bytes(mint)],
        Pubkey.from_string(_ATA_PROGRAM),
    )
    return ata


def _account_exists(pubkey: "Pubkey") -> bool:
    """Return True if account exists on chain at confirmed commitment."""
    from rpc import rpc_call, _extract_result

    resp = rpc_call("getAccountInfo", [
        str(pubkey),
        {"encoding": "base64", "commitment": "confirmed"},
    ])
    if resp is None or "error" in resp:
        return False
    return _extract_result(resp) is not None


def _create_ata_ix(
    payer_pubkey: "Pubkey",
    owner_pubkey: "Pubkey",
    mint_pubkey: "Pubkey",
    ata_pubkey: "Pubkey",
) -> "Instruction":
    """Create an ATA via create_idempotent (no-op if already exists)."""
    return Instruction(
        program_id=Pubkey.from_string(_ATA_PROGRAM),
        accounts=[
            AccountMeta(pubkey=payer_pubkey, is_signer=True, is_writable=True),
            AccountMeta(pubkey=ata_pubkey, is_signer=False, is_writable=True),
            AccountMeta(pubkey=owner_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint_pubkey, is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(_SYSTEM_PROGRAM), is_signer=False, is_writable=False),
            AccountMeta(pubkey=Pubkey.from_string(_TOKEN_PROGRAM), is_signer=False, is_writable=False),
        ],
        data=bytes([1]),  # discriminator 1 = create_idempotent
    )


def partial_sign_execute_payment(
    payer_keypair_path: str,
    beacon_pubkey_str: str,
    nonce_account_str: str,
    recipient_str: str,
    amount: int,
    mxe_pubkey_hex: str,
    mint_str: str,
    treasury_str: str | None = None,
    broadcaster_token_account_str: str | None = None,
    program_id_str: str = _MXE_PROGRAM_ID,
    cluster_offset: int = 456,
    nonce_value: str | None = None,
) -> str | None:
    """
    Build and partially sign a durable-nonce execute_payment transaction
    for the anon0mesh ble_revshare contract.

        Instructions:
            0: SystemProgram.advanceNonceAccount  (required first for durable nonces)
            N: Optionally create missing ATAs for payer/recipient/broadcaster
            N+1: ExecutePayment on the configured MXE program

    Signers: [payer (slot 0), beacon/broadcaster (slot 1)]
    Client fills slot 0 here; beacon fills slot 1 via cosignTransaction.

    Token accounts are derived as ATAs automatically — no prompts needed.
    """
    if not HAS_SOLDERS:
        log_err("Requires: pip install solders")
        return None

    try:
        from arcium_client import rescue_encrypt, _run_shim
    except ImportError:
        log_err("Requires arcium_client.py  (and node + @arcium-hq/client)")
        return None

    try:
        with open(payer_keypair_path) as f:
            payer = Keypair.from_bytes(bytes(json.load(f)))
    except Exception as exc:
        log_err(f"Failed to load keypair: {exc}")
        return None

    payer_pubkey  = payer.pubkey()
    beacon_pubkey = Pubkey.from_string(beacon_pubkey_str)
    nonce_pubkey  = Pubkey.from_string(nonce_account_str)
    prog_pubkey   = Pubkey.from_string(program_id_str)
    mint_pubkey   = Pubkey.from_string(mint_str)
    recipient_pk  = Pubkey.from_string(recipient_str)
    # Treasury defaults to beacon/operator if not explicitly provided
    treasury_pk   = Pubkey.from_string(treasury_str) if treasury_str else beacon_pubkey

    # Random u64 computation offset — uniquely identifies this Arcium computation
    comp_offset = int.from_bytes(_secrets.token_bytes(8), "little")

    log_info("Encrypting amount with Arcium MXE pubkey (x25519 + RescueCipher)...")
    try:
        enc = rescue_encrypt(mxe_pubkey_hex, [amount])
    except Exception as exc:
        log_err(f"Arcium encrypt failed: {exc}")
        return None
    pub_key_hex      = enc["pubkey_hex"]
    nonce_bn         = int(enc["nonce_bn"])
    # Rescue ciphertext for the amount — 32-byte field element passed to Arcium
    encrypted_amount = bytes(enc["ciphertexts"][0])

    log_info("Fetching Arcium PDAs...")
    try:
        accs = _run_shim("arcium_accounts", program_id_str, str(cluster_offset), str(comp_offset))
    except Exception as exc:
        log_err(f"Arcium accounts fetch failed: {exc}")
        return None

    # Derive ATAs (Associated Token Accounts) — deterministic, no RPC needed
    payer_ta       = _get_ata(payer_pubkey,  mint_pubkey)
    recipient_ta   = _get_ata(recipient_pk,  mint_pubkey)
    treasury_ta    = _get_ata(treasury_pk,   mint_pubkey)
    broadcaster_ta = (Pubkey.from_string(broadcaster_token_account_str)
                      if broadcaster_token_account_str
                      else _get_ata(beacon_pubkey, mint_pubkey))

    # sign_pda_account: PDA derived from seeds=[b"ArciumSignerAccount"] on this program
    sign_pda, _   = Pubkey.find_program_address([b"ArciumSignerAccount"], prog_pubkey)
    whitelist_pda, _ = Pubkey.find_program_address(
        [b"whitelist", bytes(mint_pubkey)], prog_pubkey
    )

    # Instruction data layout (fixed contract):
    # [disc 8B][comp_offset 8B LE][amount 8B LE][encrypted_amount 32B][nonce 16B LE][pub_key 32B] = 104 bytes
    disc      = hashlib.sha256(b"global:execute_payment").digest()[:8]
    ix_data   = (
        disc
        + comp_offset.to_bytes(8,  "little")
        + amount.to_bytes(8,       "little")
        + encrypted_amount                      # 32-byte Rescue ciphertext of amount
        + nonce_bn.to_bytes(16,    "little")
        + bytes.fromhex(pub_key_hex)
    )

    TOKEN_PROG          = Pubkey.from_string(_TOKEN_PROGRAM)
    SYSTEM_PROG         = Pubkey.from_string(_SYSTEM_PROGRAM)
    # arcium_program is hardcoded in the IDL — use the value from there, not the shim
    ARCIUM_PROG         = Pubkey.from_string("Arcj82pX7HxYKLR92qvgZUAd7vGS1k4hQvAFcPATFdEQ")
    MXE_ACCOUNT         = Pubkey.from_string(accs["mxeAccount"])
    COMP_DEF_ACCOUNT    = Pubkey.from_string(accs["compDefAccount"])
    MEMPOOL_ACCOUNT     = Pubkey.from_string(accs["mempoolAccount"])
    EXECUTING_POOL      = Pubkey.from_string(accs["executingPool"])
    COMPUTATION_ACCOUNT = Pubkey.from_string(accs["computationAccount"])
    CLUSTER_ACCOUNT     = Pubkey.from_string(accs["clusterAccount"])
    POOL_ACCOUNT        = Pubkey.from_string(accs["poolAccount"])
    CLOCK_ACCOUNT       = Pubkey.from_string(accs["clockAccount"])

    setup_ixs: list[Instruction] = []

    if not _account_exists(COMP_DEF_ACCOUNT):
        log_err("comp_def_account is not initialized for this program deployment")
        log_err("Run the program's init_payment_stats_comp_def initializer once, then retry")
        return None

    # Track which ATAs have already been queued to avoid duplicate creates
    # (e.g. when treasury == broadcaster, both resolve to the same ATA)
    _queued_atas: set[str] = set()

    def _maybe_create_ata(owner: "Pubkey", ata: "Pubkey", label: str) -> None:
        key = str(ata)
        if key in _queued_atas:
            return
        if not _account_exists(ata):
            log_warn(f"{label} ATA missing; adding create ATA ix")
            setup_ixs.append(_create_ata_ix(payer_pubkey, owner, mint_pubkey, ata))
        _queued_atas.add(key)

    _maybe_create_ata(payer_pubkey, payer_ta,    "Payer")
    _maybe_create_ata(recipient_pk, recipient_ta, "Recipient")
    _maybe_create_ata(treasury_pk,  treasury_ta,  "Treasury")

    if broadcaster_token_account_str:
        if not _account_exists(broadcaster_ta):
            log_err("Provided broadcaster token account does not exist")
            return None
    else:
        _maybe_create_ata(beacon_pubkey, broadcaster_ta, "Broadcaster")

    # Account order matches the on-chain IDL for execute_payment exactly:
    #  0 payer              writable signer
    #  1 broadcaster        optional signer
    #  2 recipient
    #  3 mint
    #  4 whitelist_entry    PDA [b"whitelist", mint]
    #  5 payer_token_account        writable
    #  6 recipient_token_account    writable
    #  7 treasury_token_account     writable   ← was missing before
    #  8 broadcaster_token_account  optional writable
    #  9 sign_pda_account   writable PDA [b"ArciumSignerAccount"] on this program
    # 10 mxe_account
    # 11 mempool_account    writable
    # 12 executing_pool     writable
    # 13 computation_account writable
    # 14 comp_def_account
    # 15 cluster_account    writable
    # 16 pool_account       writable  (hardcoded address in IDL)
    # 17 clock_account      writable  (hardcoded address in IDL)
    # 18 token_program
    # 19 system_program
    # 20 arcium_program     (hardcoded address in IDL)
    execute_ix = Instruction(
        program_id=prog_pubkey,
        accounts=[
            AccountMeta(pubkey=payer_pubkey,          is_signer=True,  is_writable=True),
            AccountMeta(pubkey=beacon_pubkey,          is_signer=True,  is_writable=False),
            AccountMeta(pubkey=recipient_pk,           is_signer=False, is_writable=False),
            AccountMeta(pubkey=mint_pubkey,            is_signer=False, is_writable=False),
            AccountMeta(pubkey=whitelist_pda,          is_signer=False, is_writable=False),
            AccountMeta(pubkey=payer_ta,               is_signer=False, is_writable=True),
            AccountMeta(pubkey=recipient_ta,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=treasury_ta,            is_signer=False, is_writable=True),
            AccountMeta(pubkey=broadcaster_ta,         is_signer=False, is_writable=True),
            AccountMeta(pubkey=sign_pda,               is_signer=False, is_writable=True),
            AccountMeta(pubkey=MXE_ACCOUNT,            is_signer=False, is_writable=False),
            AccountMeta(pubkey=MEMPOOL_ACCOUNT,        is_signer=False, is_writable=True),
            AccountMeta(pubkey=EXECUTING_POOL,         is_signer=False, is_writable=True),
            AccountMeta(pubkey=COMPUTATION_ACCOUNT,    is_signer=False, is_writable=True),
            AccountMeta(pubkey=COMP_DEF_ACCOUNT,       is_signer=False, is_writable=False),
            AccountMeta(pubkey=CLUSTER_ACCOUNT,        is_signer=False, is_writable=True),
            AccountMeta(pubkey=POOL_ACCOUNT,           is_signer=False, is_writable=True),
            AccountMeta(pubkey=CLOCK_ACCOUNT,          is_signer=False, is_writable=True),
            AccountMeta(pubkey=TOKEN_PROG,             is_signer=False, is_writable=False),
            AccountMeta(pubkey=SYSTEM_PROG,            is_signer=False, is_writable=False),
            AccountMeta(pubkey=ARCIUM_PROG,            is_signer=False, is_writable=False),
        ],
        data=ix_data,
    )

    # Fetch nonce value if not cached
    if nonce_value is None:
        log_info("Fetching nonce value from chain...")
        from rpc import get_nonce_account
        nonce_info = get_nonce_account(nonce_account_str)
        if nonce_info is None:
            log_err(_ERR_NONCE)
            return None
        nonce_value = nonce_info["nonce"]

    nonce_hash = Hash.from_string(nonce_value)
    advance_ix = advance_nonce_account(AdvanceNonceAccountParams(
        nonce_pubkey=nonce_pubkey,
        authorized_pubkey=payer_pubkey,
    ))

    msg = Message.new_with_blockhash([advance_ix, *setup_ixs, execute_ix], payer_pubkey, nonce_hash)
    tx  = Transaction.new_unsigned(msg)

    # Partial sign — payer fills slot 0; beacon (broadcaster) fills slot 1
    tx.partial_sign([payer], nonce_hash)

    tx_b64 = base64.b64encode(bytes(tx)).decode("utf-8")
    log_ok(f"execute_payment tx partially signed  comp_offset={comp_offset}")
    print(f"\n  Payer:        {payer_pubkey}")
    print(f"  Broadcaster:  {beacon_pubkey}  (beacon co-signs)")
    print(f"  Recipient:    {recipient_pk}")
    print(f"  Mint:         {mint_pubkey}")
    print(f"  Treasury:     {treasury_pk}")
    print(f"  Payer TA:     {payer_ta}")
    print(f"  Recipient TA: {recipient_ta}")
    print(f"  Treasury TA:  {treasury_ta}")
    print(f"  Bcaster TA:   {broadcaster_ta}")
    print(f"  sign_pda:     {sign_pda}")
    print(f"  {DIM}Durable nonce — tx won't expire on mesh.{RESET}")
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
            log_err(_ERR_NONCE)
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
