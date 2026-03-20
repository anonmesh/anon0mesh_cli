"""
arcium_client.py — Arcium MPC integration for the anon0mesh beacon
====================================================================
No anchorpy, no IDL parsing — mirrors useBleRevshareContract.ts exactly.

Transaction building is done in rescue_shim.mjs (where the Arcium JS SDK
lives) using raw TransactionInstruction, just like the real hook.
Python handles only: subprocess calls, solana-py RPC polling, sync wrapper.

Setup
-----
  npm install @arcium-hq/client @coral-xyz/anchor @solana/web3.js
  pip install solders solana

  ARCIUM_ENABLED=1
  ARCIUM_RPC_URL=https://api.devnet.solana.com
  ARCIUM_PAYER_KEYPAIR=~/.config/solana/id.json
  ARCIUM_MXE_PROGRAM_ID=<from arcium deploy>
  ARCIUM_MXE_PUBKEY_HEX=<from: node rescue_shim.mjs mxe_pubkey <PROGRAM_ID>>
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

# solana-py for polling only — no anchorpy
try:
    from solders.keypair import Keypair
    from solders.pubkey  import Pubkey
    from solana.rpc.async_api import AsyncClient
    from solana.rpc.commitment  import Confirmed
    HAS_SOLANA = True
except ImportError:
    HAS_SOLANA = False

from shared import log_info, log_ok, log_warn, log_err

CLUSTER_OFFSET_DEVNET  = 456
CLUSTER_OFFSET_MAINNET = 2026
POLL_INTERVAL          = 2.0
POLL_TIMEOUT           = 120.0
SHIM_PATH              = Path(__file__).parent / "rescue_shim.mjs"

# Computation account data layout offset for the result field
# Anchor account discriminator = 8 bytes, then the account data
# We just check if the account is non-empty and has data beyond discriminator
RESULT_DISCRIMINATOR_LEN = 8


# ── Shim helpers ───────────────────────────────────────────────────────────────

def _run_shim(*args: str, timeout: int = 30) -> dict:
    if not SHIM_PATH.exists():
        raise FileNotFoundError(
            f"rescue_shim.mjs not found at {SHIM_PATH}\n"
            "Run: npm install @arcium-hq/client @coral-xyz/anchor @solana/web3.js"
        )
    result = subprocess.run(
        ["node", str(SHIM_PATH), *args],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"rescue_shim.js stderr: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"rescue_shim.js non-JSON output: {result.stdout[:300]}")
    if not data.get("ok"):
        raise RuntimeError(f"rescue_shim.js error: {data.get('error', 'unknown')}")
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


# ── ArciumBeaconClient — no anchorpy, no IDL ───────────────────────────────────

class ArciumBeaconClient:
    """
    Sends Arcium computations by delegating transaction building to the
    Node.js shim (queue_computation command), then polls for the result
    using plain solana-py RPC account reads.

    This mirrors exactly what useBleRevshareContract.ts does:
    - TransactionInstruction with manually computed discriminator
    - getArciumAccounts() PDAs from @arcium-hq/client helpers
    - No anchorpy, no IDL parsing
    """

    def __init__(
        self,
        rpc_url:        str,
        payer_keypair:  "Keypair",
        mxe_program_id: str,
        mxe_pubkey_hex: str,
        cluster_offset: int = CLUSTER_OFFSET_DEVNET,
    ):
        if not HAS_SOLANA:
            raise ImportError("pip install solders solana")
        self.rpc_url        = rpc_url
        self.payer          = payer_keypair
        self.mxe_program_id = mxe_program_id          # string — passed to shim
        self.mxe_pubkey_hex = mxe_pubkey_hex
        self.cluster_offset = cluster_offset
        self._client: Optional[AsyncClient] = None

        # Serialize payer secret key as hex for passing to the shim
        self._payer_hex = bytes(payer_keypair).hex()

    async def connect(self) -> None:
        """Open solana-py RPC client for polling."""
        self._client = AsyncClient(self.rpc_url, commitment=Confirmed)
        # Quick connectivity check
        resp = await self._client.get_slot()
        log_ok(f"Arcium RPC connected  slot={resp.value}  program={self.mxe_program_id[:16]}...")

    async def confidential_get_balance(
        self,
        encrypted_address: list[int],
        client_pubkey_hex: str,
        nonce_bn: str,
        comp_def_name: str = "payment_stats",
    ) -> dict:
        """
        Queue a confidential getBalance computation.
        Transaction is built + sent by rescue_shim.mjs (no anchorpy).
        Result is polled via raw account reads.
        """
        import random
        computation_offset = random.randint(0, 2**63)

        log_info(f"Queueing Arcium computation  offset={computation_offset}  def={comp_def_name}")

        # Delegate the entire tx build + send to the shim
        shim_args = json.dumps({
            "rpcUrl":             self.rpc_url,
            "programId":          self.mxe_program_id,
            "payerKeypairHex":    self._payer_hex,
            "compDefName":        comp_def_name,
            "clusterOffset":      str(self.cluster_offset),
            "computationOffset":  str(computation_offset),
            "pubKeyHex":          client_pubkey_hex,
            "nonceBn":            nonce_bn,
            "encryptedAddress":   encrypted_address,
        })

        try:
            result = _run_shim("queue_computation", shim_args, timeout=60)
            sig               = result["signature"]
            comp_account_b58  = result["computationAccount"]
            log_ok(f"Computation queued  sig={sig[:20]}...")
        except Exception as exc:
            log_err(f"queue_computation failed: {exc}")
            return {"status": "error", "message": str(exc)}

        # Poll the computation account until Arx nodes write the result
        comp_pubkey = Pubkey.from_string(comp_account_b58)
        poll_result = await self._poll_computation_result(comp_pubkey)
        if poll_result is None:
            return {"status": "error", "message": "Computation timed out"}

        return {"status": "ok", **poll_result, "computation_sig": sig}

    async def _poll_computation_result(self, comp_pda: "Pubkey") -> dict | None:
        """
        Poll the computation account until Arx nodes write the encrypted result.
        Uses raw account data read — no IDL needed.
        The result is stored in the account after the callback instruction fires.
        """
        deadline = time.time() + POLL_TIMEOUT
        attempt  = 0

        while time.time() < deadline:
            attempt += 1
            try:
                resp = await self._client.get_account_info(comp_pda, encoding="base64")
                acct = resp.value
                if acct is not None and acct.data:
                    raw = bytes(acct.data)
                    # Account must have at least discriminator (8) + result data
                    # Arcium callback writes: [disc 8B][status 1B][ephem_pub 32B][nonce 16B][ciphertext 32B]
                    if len(raw) >= 8 + 1 + 32 + 16 + 32:
                        offset = RESULT_DISCRIMINATOR_LEN
                        status = raw[offset]
                        if status == 1:  # 1 = result ready
                            ephem_pub  = raw[offset+1  : offset+33]
                            nonce_raw  = raw[offset+33 : offset+49]
                            ciphertext = raw[offset+49 : offset+81]
                            log_ok(f"Arcium result received  (poll #{attempt})")
                            return {
                                "enc_balance":    list(ciphertext),
                                "mxe_pubkey_hex": ephem_pub.hex(),
                                "nonce_hex":      nonce_raw.hex(),
                            }
            except Exception:
                pass

            if attempt % 5 == 0:
                log_info(f"Waiting for Arcium result...  poll #{attempt}  "
                         f"{int(deadline - time.time())}s left")
            await asyncio.sleep(POLL_INTERVAL)

        log_err(f"Arcium computation timed out after {POLL_TIMEOUT}s")
        return None

    async def close(self):
        if self._client:
            await self._client.close()


# ── ArciumBeacon — sync wrapper ────────────────────────────────────────────────

class ArciumBeacon:
    """Synchronous facade for beacon.py."""

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

        # Validate required vars are set and non-empty
        required = {
            "ARCIUM_PAYER_KEYPAIR":  os.getenv("ARCIUM_PAYER_KEYPAIR",  "").strip(),
            "ARCIUM_MXE_PROGRAM_ID": os.getenv("ARCIUM_MXE_PROGRAM_ID", "").strip(),
            "ARCIUM_MXE_PUBKEY_HEX": os.getenv("ARCIUM_MXE_PUBKEY_HEX", "").strip(),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            for k in missing:
                log_warn(f"  {k} is not set")
            log_warn("Arcium disabled — deploy your MXE first:")
            log_warn("  arcium deploy --cluster-offset 456 --recovery-set-size 4")
            log_warn("    --keypair-path ~/.config/solana/id.json")
            log_warn("    --rpc-url https://api.devnet.solana.com")
            if "ARCIUM_MXE_PUBKEY_HEX" in missing:
                log_warn("  node rescue_shim.mjs mxe_pubkey <PROGRAM_ID>")
            return cls(None)

        try:
            kp_path = os.path.expanduser(required["ARCIUM_PAYER_KEYPAIR"])
            with open(kp_path) as f:
                payer = Keypair.from_bytes(bytes(json.load(f)))

            cluster_offset = int(os.getenv("ARCIUM_CLUSTER_OFFSET", str(CLUSTER_OFFSET_DEVNET)))

            client = ArciumBeaconClient(
                rpc_url        = os.getenv("ARCIUM_RPC_URL", "https://api.devnet.solana.com"),
                payer_keypair  = payer,
                mxe_program_id = required["ARCIUM_MXE_PROGRAM_ID"],
                mxe_pubkey_hex = required["ARCIUM_MXE_PUBKEY_HEX"],
                cluster_offset = cluster_offset,
            )
            log_ok(f"Arcium client ready  cluster_offset={cluster_offset}")
            return cls(client)

        except (KeyError, FileNotFoundError) as exc:
            log_err(f"Arcium env error: {exc}")
            return cls(None)

    def confidential_get_balance(
        self,
        encrypted_address: list[int],
        client_pubkey_hex: str,
        nonce_bn: str,
        comp_def_name: str = "payment_stats",
    ) -> dict | None:
        if not self.enabled:
            return None
        fut = asyncio.run_coroutine_threadsafe(
            self._client.confidential_get_balance(
                encrypted_address, client_pubkey_hex, nonce_bn, comp_def_name),
            self._loop,
        )
        try:
            return fut.result(timeout=POLL_TIMEOUT + 15)
        except Exception as exc:
            log_err(f"Arcium call failed: {exc}")
            return None
