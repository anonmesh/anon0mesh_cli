"""
arcium_client.py — Arcium MPC integration for the anon0mesh beacon
====================================================================
Based on the actual anon0mesh contract:
  Program ID:  7fvHNYVuZP6EYt68GLUa4kU8f8dCBSaGafL9aDhhtMZN
  Instruction: execute_payment(computation_offset, amount, nonce, pub_key)
  Purpose:     Log ENCRYPTED payment statistics after a transaction is relayed

How it fits into the beacon flow
---------------------------------
  Client → beacon.forward_to_solana("sendTransaction", [...])
         → Solana confirms tx
         → beacon calls arcium.log_payment_stats(amount, accounts)
         → Arcium MPC nodes process payment_stats circuit
         → Encrypted stats recorded on-chain

  The beacon relays transactions AND logs encrypted stats.
  Arcium never touches balance queries — that's plain RPC.

Setup
-----
  npm install @arcium-hq/client @coral-xyz/anchor @solana/web3.js @solana/spl-token
  pip install solders solana

  ARCIUM_ENABLED=1
  ARCIUM_RPC_URL=https://api.devnet.solana.com
  ARCIUM_PAYER_KEYPAIR=~/.config/solana/id.json
  ARCIUM_MXE_PUBKEY_HEX=<from: node rescue_shim.mjs mxe_pubkey>
  ARCIUM_CLUSTER_OFFSET=456
"""

import os
import json
import time
import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Optional

try:
    from solders.keypair import Keypair
    from solders.pubkey  import Pubkey
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment  import Confirmed
    HAS_SOLANA = True
except ImportError:
    HAS_SOLANA = False

from shared import log_info, log_ok, log_warn, log_err

# ── Constants ──────────────────────────────────────────────────────────────────
# From declare_id! in the contract
MXE_PROGRAM_ID         = "7fvHNYVuZP6EYt68GLUa4kU8f8dCBSaGafL9aDhhtMZN"

# Hardcoded in the contract as ARCIUM_SIGNER_PDA
ARCIUM_SIGNER_PDA      = "nhy7kthZGJjV3yqbyPuSeo2KhNriia4DQrii8jW3KcC"

CLUSTER_OFFSET_DEVNET  = 456
CLUSTER_OFFSET_MAINNET = 2026
POLL_INTERVAL          = 2.0
POLL_TIMEOUT           = 120.0
SHIM_PATH              = Path(__file__).parent / "rescue_shim.mjs"


# ── Shim helpers ───────────────────────────────────────────────────────────────

def _run_shim(*args: str, timeout: int = 60) -> dict:
    if not SHIM_PATH.exists():
        raise FileNotFoundError(
            f"rescue_shim.mjs not found at {SHIM_PATH}\n"
            "Run: npm install @arcium-hq/client @coral-xyz/anchor @solana/web3.js @solana/spl-token"
        )
    result = subprocess.run(
        ["node", str(SHIM_PATH), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"shim stderr: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"shim non-JSON: {result.stdout[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"shim error: {data.get('error', 'unknown')}")
    return data


def rescue_keygen() -> tuple[str, str]:
    data = _run_shim("keygen")
    return data["privkey_hex"], data["pubkey_hex"]


def rescue_encrypt(mxe_pubkey_hex: str, values: list[int], nonce_hex: str | None = None) -> dict:
    args = ["encrypt", mxe_pubkey_hex, json.dumps(values)]
    if nonce_hex:
        args.append(nonce_hex)
    return _run_shim(*args)


def rescue_decrypt(shared_secret_hex: str, ciphertexts: list[list[int]], nonce_hex: str) -> list[int]:
    data = _run_shim("decrypt", shared_secret_hex, json.dumps(ciphertexts), nonce_hex)
    return [int(v) for v in data["values"]]


def rescue_shared_secret(privkey_hex: str, mxe_pubkey_hex: str) -> str:
    return _run_shim("shared_secret", privkey_hex, mxe_pubkey_hex)["shared_secret_hex"]


# ── ArciumBeaconClient ─────────────────────────────────────────────────────────

class ArciumBeaconClient:
    """
    Logs encrypted payment statistics to the anon0mesh Arcium MXE
    after the beacon successfully relays a transaction.

    The execute_payment instruction takes:
      computation_offset: u64
      amount:             u64   (payment amount in lamports/tokens)
      nonce:              u128  (from client x25519 encryption)
      pub_key:            [u8;32] (client x25519 ephemeral pubkey)

    Plus all the token accounts and Arcium PDAs.
    """

    def __init__(
        self,
        rpc_url:        str,
        payer_keypair:  "Keypair",
        mxe_pubkey_hex: str,
        cluster_offset: int = CLUSTER_OFFSET_DEVNET,
        program_id:     str = MXE_PROGRAM_ID,
    ):
        if not HAS_SOLANA:
            raise ImportError("pip install solders solana")
        self.rpc_url        = rpc_url
        self.payer          = payer_keypair
        self.mxe_pubkey_hex = mxe_pubkey_hex
        self.cluster_offset = cluster_offset
        self.program_id     = program_id
        self._payer_hex     = bytes(payer_keypair).hex()
        self._client: Optional[AsyncClient] = None

    async def connect(self) -> None:
        self._client = AsyncClient(self.rpc_url, commitment=Confirmed)
        resp = await self._client.get_slot()
        log_ok(f"Arcium RPC connected  slot={resp.value}")

    async def log_payment_stats(
        self,
        amount:                    int,
        payer_token_account:       str,
        recipient:                 str,
        recipient_token_account:   str,
        mint:                      str,
        broadcaster:               str | None = None,
        broadcaster_token_account: str | None = None,
    ) -> dict:
        """
        Call execute_payment on the anon0mesh MXE to log encrypted payment stats.
        Called by the beacon after sendTransaction succeeds.

        Generates a fresh x25519 keypair per call — the nonce and pubkey are
        included in the instruction so Arcium MPC can decrypt the amount.
        """
        import random

        # Generate ephemeral x25519 keypair for this payment stat entry
        enc = rescue_encrypt(self.mxe_pubkey_hex, [amount])
        pub_key_hex = enc["pubkey_hex"]
        nonce_bn    = enc["nonce_bn"]

        log_info(f"Logging payment stats  amount={amount}  via Arcium MPC")

        shim_args = json.dumps({
            "rpcUrl":                    self.rpc_url,
            "programId":                 self.program_id,
            "payerKeypairHex":           self._payer_hex,
            "clusterOffset":             str(self.cluster_offset),
            "amount":                    str(amount),
            "pubKeyHex":                 pub_key_hex,
            "nonceBn":                   nonce_bn,
            "recipientB58":              recipient,
            "mintB58":                   mint,
            "payerTokenAccountB58":      payer_token_account,
            "recipientTokenAccountB58":  recipient_token_account,
            "broadcasterB58":            broadcaster,
            "broadcasterTokenAccountB58": broadcaster_token_account,
        })

        try:
            result = _run_shim("execute_payment", shim_args, timeout=60)
            log_ok(f"Payment stats logged  sig={result['signature'][:20]}...")
            return {"status": "ok", "signature": result["signature"]}
        except Exception as exc:
            log_err(f"execute_payment failed: {exc}")
            return {"status": "error", "message": str(exc)}

    async def close(self):
        if self._client:
            await self._client.close()


# ── ArciumBeacon sync wrapper ──────────────────────────────────────────────────

class ArciumBeacon:
    """
    Synchronous facade for beacon.py.

    Integration in beacon.py — call after sendTransaction succeeds:

        # In forward_to_solana(), after confirming tx:
        if method == "sendTransaction" and arcium and arcium.enabled:
            # Parse token accounts from the original tx if available
            # or accept them as extra params from the client
            arcium.log_payment_stats(
                amount                   = parsed_amount,
                payer_token_account      = payer_ta,
                recipient                = recipient,
                recipient_token_account  = recipient_ta,
                mint                     = mint,
            )
    """

    def __init__(self, client: ArciumBeaconClient | None):
        self._client = client
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self.enabled = client is not None
        if self.enabled:
            self._thread.start()
            fut = asyncio.run_coroutine_threadsafe(self._client.connect(), self._loop)
            try:
                fut.result(timeout=15)
            except Exception as exc:
                log_err(f"Arcium init failed: {exc}")
                self.enabled = False

    @classmethod
    def from_env(cls) -> "ArciumBeacon":
        # Auto-load .env
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

        if os.getenv("ARCIUM_ENABLED", "0") != "1":
            log_info("Arcium disabled (ARCIUM_ENABLED != 1)")
            return cls(None)

        if not HAS_SOLANA:
            log_warn("pip install solders solana")
            return cls(None)

        # Only need MXE pubkey — program ID is hardcoded from the contract
        required = {
            "ARCIUM_PAYER_KEYPAIR":  os.getenv("ARCIUM_PAYER_KEYPAIR",  "").strip(),
            "ARCIUM_MXE_PUBKEY_HEX": os.getenv("ARCIUM_MXE_PUBKEY_HEX", "").strip(),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            for k in missing:
                log_warn(f"  {k} is not set")
            log_warn("Arcium disabled — set env vars:")
            log_warn("  ARCIUM_MXE_PUBKEY_HEX: node rescue_shim.mjs mxe_pubkey")
            return cls(None)

        try:
            kp_path = os.path.expanduser(required["ARCIUM_PAYER_KEYPAIR"])
            with open(kp_path) as f:
                payer = Keypair.from_bytes(bytes(json.load(f)))

            cluster_offset = int(os.getenv("ARCIUM_CLUSTER_OFFSET", str(CLUSTER_OFFSET_DEVNET)))
            program_id     = os.getenv("ARCIUM_MXE_PROGRAM_ID", MXE_PROGRAM_ID)

            client = ArciumBeaconClient(
                rpc_url        = os.getenv("ARCIUM_RPC_URL", "https://api.devnet.solana.com"),
                payer_keypair  = payer,
                mxe_pubkey_hex = required["ARCIUM_MXE_PUBKEY_HEX"],
                cluster_offset = cluster_offset,
                program_id     = program_id,
            )
            log_ok(f"Arcium client ready  program={program_id[:16]}...  cluster={cluster_offset}")
            return cls(client)

        except (KeyError, FileNotFoundError) as exc:
            log_err(f"Arcium env error: {exc}")
            return cls(None)

    def log_payment_stats(
        self,
        amount:                    int,
        payer_token_account:       str,
        recipient:                 str,
        recipient_token_account:   str,
        mint:                      str,
        broadcaster:               str | None = None,
        broadcaster_token_account: str | None = None,
    ) -> dict | None:
        """Fire-and-forget: log payment stats without blocking the beacon response."""
        if not self.enabled:
            return None

        def _run():
            fut = asyncio.run_coroutine_threadsafe(
                self._client.log_payment_stats(
                    amount, payer_token_account, recipient,
                    recipient_token_account, mint,
                    broadcaster, broadcaster_token_account,
                ),
                self._loop,
            )
            try:
                return fut.result(timeout=POLL_TIMEOUT + 15)
            except Exception as exc:
                log_err(f"Arcium log_payment_stats failed: {exc}")

        # Run in background thread — don't block the RPC response to the client
        threading.Thread(target=_run, daemon=True).start()
        return {"status": "queued"}