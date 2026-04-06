from __future__ import annotations
"""
rpc.py — Solana JSON-RPC helpers
==================================
All functions that query or relay to the Solana network via the beacon pool.
No local signing happens here — see wallet.py for that.
"""

import os
import json
import concurrent.futures

import state
from shared import (
    log_info, log_ok, log_warn, log_err,
    BOLD, CYAN, GREEN, RED, RESET, DIM,
)

# ── Optional Arcium confidential-query support ─────────────────────────────────
try:
    from arcium_client import rescue_encrypt, rescue_decrypt, rescue_shared_secret
    HAS_ARCIUM = True
except ImportError:
    HAS_ARCIUM = False

_NO_BEACON_RESP = "No response from beacon"


# ═══════════════════════════════════════════════════════════════════════════════
# Core RPC dispatcher
# ═══════════════════════════════════════════════════════════════════════════════

def rpc_call(method, params=None):
    return state.pool.call(method, params)


def _extract_result(resp: dict):
    """
    Solana RPC wraps some results in {"context":..., "value":...}.
    Others return the value directly. Handle both.
    """
    if resp is None:
        return None
    r = resp.get("result")
    if isinstance(r, dict) and "value" in r:
        return r["value"]
    return r


# ═══════════════════════════════════════════════════════════════════════════════
# Query functions
# ═══════════════════════════════════════════════════════════════════════════════

def get_balance(address):
    resp = rpc_call("getBalance", [address])
    if resp is None:
        return
    if "error" in resp:
        log_err(f"RPC error: {resp['error'].get('message', resp['error'])}")
        return
    lamports = resp.get("result", {})
    if isinstance(lamports, dict):
        lamports = lamports.get("value", 0)
    sol = lamports / 1_000_000_000
    print(f"\n  {GREEN}{BOLD}{address}{RESET}")
    print(f"  Balance: {BOLD}{sol:.9f} SOL{RESET}  ({lamports:,} lamports)\n")


def confidential_get_balance(address: str) -> None:
    """
    Fetch SOL balance via Arcium MPC — the beacon never sees the address or balance.
    Requires ARCIUM_MXE_PUBKEY_HEX in .env and arcium_client.py present.
    Falls back to plain get_balance if Arcium is unavailable.
    """
    if not HAS_ARCIUM:
        log_warn("arcium_client.py not found — falling back to plain balance")
        get_balance(address)
        return

    mxe_pubkey_hex = os.getenv("ARCIUM_MXE_PUBKEY_HEX", "").strip()
    if not mxe_pubkey_hex:
        log_warn("ARCIUM_MXE_PUBKEY_HEX not set — falling back to plain balance")
        get_balance(address)
        return

    try:
        from solders.pubkey import Pubkey as _Pubkey
        address_bytes = list(bytes(_Pubkey.from_string(address)))
    except Exception as exc:
        log_err(f"Invalid address: {exc}")
        return

    log_info("Encrypting address for Arcium MPC...")
    try:
        enc = rescue_encrypt(mxe_pubkey_hex, address_bytes)
    except Exception as exc:
        log_err(f"Encryption failed: {exc}")
        return

    resp = rpc_call("getBalance", [{
        "enc_address":   enc["ciphertexts"][0],
        "ephem_pub":     enc["pubkey_hex"],
        "nonce":         int(enc["nonce_bn"]),
        "comp_def_name": "payment_stats",
    }])

    if resp is None:
        return
    if resp.get("status") == "error":
        log_err(f"Arcium error: {resp.get('message', '?')}")
        return

    r = resp.get("result", resp)
    if r.get("status") == "error":
        log_err(f"Arcium error: {r.get('message', '?')}")
        return

    try:
        shared_secret = rescue_shared_secret(
            enc.get("shared_secret_hex") or enc.get("privkey_hex", ""),
            r.get("mxe_pubkey_hex", ""),
        )
        values   = rescue_decrypt(shared_secret, [r["enc_balance"]], r["nonce_hex"])
        lamports = values[0]
        sol      = lamports / 1_000_000_000
        print()
        print(f"  {GREEN}{BOLD}{address}{RESET}  {DIM}(confidential via Arcium MPC){RESET}")
        print(f"  Balance: {BOLD}{sol:.9f} SOL{RESET}  ({lamports:,} lamports)")
        print(f"  {DIM}Beacon never saw this address or balance{RESET}")
        print()
    except Exception as exc:
        log_err(f"Decryption failed: {exc}")
        log_warn("Falling back to plain balance check")
        get_balance(address)


def get_slot():
    resp = rpc_call("getSlot")
    if resp is None:
        log_err(_NO_BEACON_RESP); return
    if "error" in resp:
        log_err(f"RPC error: {resp['error']}"); return
    val = _extract_result(resp)
    if val is not None:
        print(f"\n  Current slot: {BOLD}{val:,}{RESET}\n")
    else:
        log_warn(f"Unexpected response: {json.dumps(resp)}")


def get_block_height():
    resp = rpc_call("getBlockHeight")
    if resp is None:
        log_err(_NO_BEACON_RESP); return
    if "error" in resp:
        log_err(f"RPC error: {resp['error']}"); return
    val = _extract_result(resp)
    if val is not None:
        print(f"\n  Block height: {BOLD}{val:,}{RESET}\n")
    else:
        log_warn(f"Unexpected response: {json.dumps(resp)}")


def get_transaction_count():
    resp = rpc_call("getTransactionCount")
    if resp is None:
        log_err(_NO_BEACON_RESP); return
    if "error" in resp:
        log_err(f"RPC error: {resp['error']}"); return
    val = _extract_result(resp)
    if val is not None:
        print(f"\n  Transaction count: {BOLD}{val:,}{RESET}\n")
    else:
        log_warn(f"Unexpected response: {json.dumps(resp)}")


def get_recent_blockhash() -> str | None:
    resp = rpc_call("getLatestBlockhash")
    if resp is None:
        log_err(_NO_BEACON_RESP); return None
    if "error" in resp:
        log_err(f"RPC error: {resp['error']}"); return None
    r = resp.get("result")
    if isinstance(r, dict):
        val = r.get("value", r)
        bh  = val.get("blockhash") if isinstance(val, dict) else None
        if bh:
            print(f"\n  Latest blockhash: {BOLD}{bh}{RESET}\n")
            return bh
    log_warn(f"Unexpected response: {json.dumps(resp)}")
    return None


def _print_sol_balance(sol_resp: dict | None) -> None:
    if not (sol_resp and "result" in sol_resp):
        log_warn("Could not fetch SOL balance")
        return
    lamports = sol_resp["result"]
    if isinstance(lamports, dict):
        lamports = lamports.get("value", 0)
    sol = lamports / 1_000_000_000
    print(f"  {BOLD}SOL Balance:{RESET}  {BOLD}{sol:.9f} SOL{RESET}  {DIM}({lamports:,} lamports){RESET}")


def _print_spl_tokens(token_resp: dict | None) -> None:
    if not (token_resp and "result" in token_resp):
        log_warn("Could not fetch SPL token accounts")
        return
    accounts = token_resp["result"]["value"]
    if not accounts:
        print(f"  {DIM}No SPL token accounts{RESET}")
        return
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


def get_token_accounts(owner):
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        fut_sol    = ex.submit(rpc_call, "getBalance", [owner])
        fut_tokens = ex.submit(rpc_call, "getTokenAccountsByOwner", [
            owner,
            {"programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"},
            {"encoding": "jsonParsed"},
        ])
        sol_resp   = fut_sol.result()
        token_resp = fut_tokens.result()

    print(f"\n  {GREEN}{BOLD}{owner}{RESET}")
    _print_sol_balance(sol_resp)
    _print_spl_tokens(token_resp)
    print()


# ═══════════════════════════════════════════════════════════════════════════════
# Beacon co-sign protocol
# ═══════════════════════════════════════════════════════════════════════════════

def get_beacon_pubkey() -> str | None:
    """Fetch the beacon's co-signing pubkey (needed to build Arcium co-sign txs)."""
    resp = rpc_call("getBeaconPubkey")
    if resp is None:
        return None
    if "error" in resp:
        log_err(f"getBeaconPubkey: {resp['error'].get('message', resp['error'])}")
        return None
    return _extract_result(resp)


def cosign_and_send(partial_tx_b64: str, arcium_meta: dict | None = None) -> str | None:
    """
    Send a partially-signed transaction to the beacon for co-signing and relay.
    arcium_meta: optional dict forwarded to the beacon for post-relay stats logging,
                e.g. {"amount": 1000, "mint": "...", "payer_ta": "...", ...}
    Returns the Solana transaction signature on success, or None.
    """
    params = [partial_tx_b64]
    if arcium_meta:
        params.append({"arcium": arcium_meta})
    resp = rpc_call("cosignTransaction", params)
    if resp is None:
        return None
    if "error" in resp:
        log_err(f"Co-sign rejected: {resp['error'].get('message', resp['error'])}")
        return None
    sig = _extract_result(resp)
    if sig:
        log_ok("Co-signed transaction relayed via beacon!")
        print(f"\n  Signature: {BOLD}{GREEN}{sig}{RESET}\n")
    return sig


# ═══════════════════════════════════════════════════════════════════════════════
# Transaction relay
# ═══════════════════════════════════════════════════════════════════════════════

def send_transaction(signed_tx_b64):
    resp = rpc_call("sendTransaction", [signed_tx_b64, {"encoding": "base64"}])
    if resp is None:
        return
    if "error" in resp:
        log_err(f"Transaction rejected: {resp['error'].get('message', resp['error'])}")
        return
    log_ok("Transaction relayed via mesh!")
    print(f"\n  Signature: {BOLD}{GREEN}{resp.get('result', '?')}{RESET}\n")


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


# ═══════════════════════════════════════════════════════════════════════════════
# Nonce account query
# ═══════════════════════════════════════════════════════════════════════════════

def get_nonce_account(nonce_pubkey_str: str) -> dict | None:
    """
    Fetch a durable nonce account and return its current state.
    Returns {"nonce": <blockhash_str>, "authority": <pubkey_str>} or None on error.
    """
    resp = rpc_call("getAccountInfo", [
        nonce_pubkey_str,
        {"encoding": "jsonParsed", "commitment": "confirmed"},
    ])
    if resp is None:
        log_err(_NO_BEACON_RESP); return None
    if "error" in resp:
        log_err(f"RPC error: {resp['error']}"); return None

    account = _extract_result(resp)
    if account is None:
        log_err(f"Account {nonce_pubkey_str} not found (does it exist on this network?)")
        return None

    try:
        parsed    = account["data"]["parsed"]
        if parsed.get("type") != "initialized":
            log_err(f"Nonce account is not initialized (type={parsed.get('type')!r})")
            return None
        info      = parsed["info"]
        nonce_val = info["blockhash"]
        authority = info["authority"]
    except (KeyError, TypeError) as exc:
        log_err(f"Could not parse nonce account data: {exc}")
        log_warn('Confirm this is a nonce account: raw getAccountInfo ["<pubkey>",{"encoding":"jsonParsed"}]')
        return None

    print(f"\n  {GREEN}{BOLD}{nonce_pubkey_str}{RESET}  {DIM}(durable nonce account){RESET}")
    print(f"  Nonce value (use as blockhash): {BOLD}{nonce_val}{RESET}")
    print(f"  Authority:                      {authority}\n")
    return {"nonce": nonce_val, "authority": authority}
