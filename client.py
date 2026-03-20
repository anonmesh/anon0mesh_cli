"""
arcium_client.py — Arcium MPC integration for the anon0mesh beacon
====================================================================
Pure Python for all Solana interaction (anchorpy).
RescueCipher encryption delegated to rescue_shim.mjs via subprocess.

Encryption correction from previous version
--------------------------------------------
The original used X25519 + ChaCha20-Poly1305 — that is WRONG.
Per the Arcium docs, the correct stack is:
  1. X25519 ECDH -> shared secret
  2. Rescue-Prime hash (over F_{2^255-19}) -> cipher key (NOT SHA-256)
  3. Rescue cipher in CTR mode, m=5, 10 rounds -> ciphertext (NOT ChaCha20)

The Rescue cipher has no Python library. rescue_shim.mjs wraps the
canonical @arcium-hq/client implementation. Python calls it via subprocess.
Node.js is already required by Anchor/yarn so it is always available.

Setup
-----
  npm install @arcium-hq/client           # once, in project dir
  pip install anchorpy solders solana

  ARCIUM_ENABLED=1
  ARCIUM_RPC_URL=https://api.devnet.solana.com
  ARCIUM_PAYER_KEYPAIR=~/.config/solana/id.json
  ARCIUM_MXE_PROGRAM_ID=<from arcium deploy>
  ARCIUM_MXE_PUBKEY_HEX=<from arcium show-mxe>
  ARCIUM_CLUSTER_OFFSET=456              # devnet=456, mainnet=2026
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
    from anchorpy import Program, Provider, Wallet, Idl, Context
    from anchorpy.provider import DEFAULT_OPTIONS
    HAS_ANCHOR = True
except ImportError:
    HAS_ANCHOR = False

from shared import log_info, log_ok, log_warn, log_err

ARCIUM_PROGRAM_ID_DEVNET = "ARCiUR1NWLS27eqgxWNR96sVzVkHhvRPCeGTa8n2caHF"
CLUSTER_OFFSET_DEVNET    = 456
CLUSTER_OFFSET_MAINNET   = 2026
POLL_INTERVAL            = 2.0
POLL_TIMEOUT             = 120.0
SHIM_PATH                = Path(__file__).parent / "rescue_shim.mjs"


# -- RescueCipher Python wrappers (via rescue_shim.mjs) --------------------------

def _run_shim(*args: str) -> dict:
    if not SHIM_PATH.exists():
        raise FileNotFoundError(
            f"rescue_shim.mjs not found at {SHIM_PATH}\n"
            "Run: npm install @arcium-hq/client in your project directory"
        )
    result = subprocess.run(
        ["node", str(SHIM_PATH), *args],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        raise RuntimeError(f"rescue_shim.mjs error: {result.stderr.strip()}")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise RuntimeError(f"rescue_shim.mjs non-JSON: {result.stdout[:200]}")
    if not data.get("ok"):
        raise RuntimeError(f"rescue_shim.mjs failed: {data.get('error', 'unknown')}")
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


# -- ArciumBeaconClient (anchorpy) ---------------------------------------------

class ArciumBeaconClient:

    def __init__(self, rpc_url, payer_keypair, mxe_program_id, mxe_pubkey_hex,
                 cluster_offset=CLUSTER_OFFSET_DEVNET,
                 arcium_program_id=ARCIUM_PROGRAM_ID_DEVNET, idl_path=None):
        if not HAS_ANCHOR:
            raise ImportError("pip install anchorpy solders solana")
        self.rpc_url           = rpc_url
        self.payer             = payer_keypair
        self.mxe_program_id    = Pubkey.from_string(mxe_program_id)
        self.mxe_pubkey_hex    = mxe_pubkey_hex
        self.cluster_offset    = cluster_offset
        self.arcium_program_id = Pubkey.from_string(arcium_program_id)
        self.idl_path          = Path(idl_path or Path(__file__).parent / "arcium_mxe.json")
        self._program          = None
        self._client           = None

    async def connect(self):
        self._client = AsyncClient(self.rpc_url, commitment=Confirmed)
        wallet   = Wallet(self.payer)
        provider = Provider(self._client, wallet, DEFAULT_OPTIONS)
        if not self.idl_path.exists():
            log_warn(f"IDL not found at {self.idl_path} — fetching from chain")
            idl = await Program.fetch_idl(self.mxe_program_id, provider)
            self.idl_path.write_text(idl.to_json())
            log_ok(f"IDL cached -> {self.idl_path}")
        idl = Idl.from_json(self.idl_path.read_text())
        self._program = Program(idl, self.mxe_program_id, provider=provider)
        log_ok(f"Arcium MXE connected  program={self.mxe_program_id}")

    def _arcium_accounts(self, comp_def_name: str, computation_offset: int) -> dict:
        """
        Get all Arcium PDAs via rescue_shim.mjs arcium_accounts command.
        Mirrors useBleRevshareContract.getArciumAccounts() exactly:

            compDefOffset = Buffer.from(getCompDefAccOffset(name)).readUInt32LE()
            computationAccount = getComputationAccAddress(clusterOffset, computationOffset)
            clusterAccount     = getClusterAccAddress(clusterOffset)
            mxeAccount         = getMXEAccAddress(programId)
            mempoolAccount     = getMempoolAccAddress(clusterOffset)
            executingPool      = getExecutingPoolAccAddress(clusterOffset)
            compDefAccount     = getCompDefAccAddress(programId, compDefOffset)
            poolAccount        = getFeePoolAccAddress()
            clockAccount       = getClockAccAddress()
        """
        data = _run_shim(
            "arcium_accounts",
            str(self.mxe_program_id),
            comp_def_name,
            str(self.cluster_offset),
            str(computation_offset),
        )
        return {k: Pubkey.from_string(v) if isinstance(v, str) and k != "compDefOffset"
                else v for k, v in data.items() if k != "ok"}

    def _signer_pda(self) -> "Pubkey":
        # Seed matches the real contract: b"ArciumSignerAccount"
        pda, _ = Pubkey.find_program_address([b"ArciumSignerAccount"], self.mxe_program_id)
        return pda

    async def confidential_get_balance(
        self,
        encrypted_address: list[int],  # [u8;32] ciphertext
        client_pubkey_hex: str,         # x25519 ephemeral pubkey hex
        nonce_bn: str,                  # u128 LE as decimal string
        comp_def_name: str = "payment_stats",  # matches COMP_DEF_NAME in real contract
    ) -> dict:
        """
        Queue a confidential computation via the anon0mesh Arcium MXE.
        Account structure mirrors useBleRevshareContract.getArciumAccounts().
        """
        if self._program is None:
            raise RuntimeError("Call connect() first")

        import random
        computation_offset = random.randint(0, 2**63)

        # Get all PDAs from shim — exact match to getArciumAccounts() in real contract
        log_info(f"Resolving Arcium accounts  comp_def={comp_def_name}  offset={computation_offset}")
        accounts = self._arcium_accounts(comp_def_name, computation_offset)

        log_info(f"Queueing confidential getBalance  offset={computation_offset}")
        try:
            tx_sig = await self._program.rpc["queue_confidential_balance"](
                computation_offset,
                list(bytes.fromhex(client_pubkey_hex)),  # x25519 pubkey [u8;32]
                int(nonce_bn),                            # u128 nonce
                encrypted_address,                        # [u8;32] ciphertext
                ctx=Context(
                    accounts={
                        "payer":               self.payer.pubkey(),
                        "sign_pda_account":    self._signer_pda(),   # "ArciumSignerAccount"
                        "computation_account": accounts["computationAccount"],
                        "cluster_account":     accounts["clusterAccount"],
                        "mxe_account":         accounts["mxeAccount"],
                        "mempool_account":     accounts["mempoolAccount"],
                        "executing_pool":      accounts["executingPool"],
                        "comp_def_account":    accounts["compDefAccount"],
                        "pool_account":        accounts["poolAccount"],   # getFeePoolAccAddress()
                        "clock_account":       accounts["clockAccount"],  # getClockAccAddress()
                        "arcium_program":      self.arcium_program_id,
                    },
                    signers=[self.payer],
                ),
            )
            log_ok(f"Computation queued  sig={tx_sig}")
        except Exception as exc:
            log_err(f"queue_computation failed: {exc}")
            return {"status": "error", "message": str(exc)}

        result = await self._poll_computation_result(accounts["computationAccount"])
        if result is None:
            return {"status": "error", "message": "Computation timed out"}
        return {"status": "ok", **result, "computation_sig": str(tx_sig)}

    async def _poll_computation_result(self, comp_pda):
        deadline = time.time() + POLL_TIMEOUT
        attempt  = 0
        while time.time() < deadline:
            attempt += 1
            try:
                account = await self._program.account["ComputationAccount"].fetch(comp_pda)
                if hasattr(account, "result") and account.result is not None:
                    log_ok(f"Result received  (poll #{attempt})")
                    r = account.result
                    return {
                        "enc_balance":    list(bytes(r.ciphertext)),
                        "mxe_pubkey_hex": bytes(r.ephem_pub).hex(),
                        "nonce_hex":      r.nonce.to_bytes(16, "little").hex(),
                    }
            except Exception:
                pass
            if attempt % 5 == 0:
                log_info(f"Waiting for result... (poll #{attempt}, "
                         f"{int(deadline - time.time())}s left)")
            await asyncio.sleep(POLL_INTERVAL)
        log_err(f"Timed out after {POLL_TIMEOUT}s")
        return None

    async def close(self):
        if self._client:
            await self._client.close()


# -- ArciumBeacon sync wrapper -------------------------------------------------

class ArciumBeacon:
    """Synchronous wrapper for beacon.py. See docstring at top for integration."""

    def __init__(self, client):
        self._client = client
        self._loop   = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self.enabled = client is not None
        if self.enabled:
            self._thread.start()
            fut = asyncio.run_coroutine_threadsafe(self._client.connect(), self._loop)
            try:
                fut.result(timeout=30)
            except Exception as exc:
                log_err(f"Arcium init failed: {exc}")
                self.enabled = False

    @classmethod
    def from_env(cls) -> "ArciumBeacon":
        env_file = Path(__file__).parent / ".env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

        if os.getenv("ARCIUM_ENABLED", "0") != "1":
            log_info("Arcium disabled (set ARCIUM_ENABLED=1 to enable)")
            return cls(None)
        if not HAS_ANCHOR:
            log_warn("pip install anchorpy solders solana")
            return cls(None)
        try:
            rescue_keygen()
        except Exception as exc:
            log_warn(f"rescue_shim.mjs not working: {exc}")
            log_warn("Run: npm install @arcium-hq/client")
            return cls(None)
        try:
            with open(os.path.expanduser(os.environ["ARCIUM_PAYER_KEYPAIR"])) as f:
                payer = Keypair.from_bytes(bytes(json.load(f)))
            cluster_offset = int(os.getenv("ARCIUM_CLUSTER_OFFSET", str(CLUSTER_OFFSET_DEVNET)))
            client = ArciumBeaconClient(
                rpc_url        = os.getenv("ARCIUM_RPC_URL", "https://api.devnet.solana.com"),
                payer_keypair  = payer,
                mxe_program_id = os.environ["ARCIUM_MXE_PROGRAM_ID"],
                mxe_pubkey_hex = os.environ["ARCIUM_MXE_PUBKEY_HEX"],
                cluster_offset = cluster_offset,
            )
            log_ok(f"Arcium client ready  cluster_offset={cluster_offset}")
            return cls(client)
        except (KeyError, FileNotFoundError) as exc:
            log_err(f"Arcium env incomplete: {exc}")
            return cls(None)

    def confidential_get_balance(self, encrypted_address, client_pubkey_hex,
                                  nonce_bn, comp_def_name="payment_stats") -> dict | None:
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
