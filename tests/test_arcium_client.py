"""
tests/test_arcium_client.py — unit tests for arcium_client.py
Shim subprocess calls are mocked; no Node.js required.
"""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import arcium_client


# ── helpers ────────────────────────────────────────────────────────────────────

def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    m = MagicMock()
    m.stdout     = stdout
    m.stderr     = stderr
    m.returncode = returncode
    return m


# ── _run_shim ─────────────────────────────────────────────────────────────────

def test_run_shim_missing_file(tmp_path):
    with patch.object(arcium_client, "SHIM_PATH", tmp_path / "missing.mjs"):
        with pytest.raises(FileNotFoundError, match="rescue_shim.mjs not found"):
            arcium_client._run_shim("keygen")


@patch("subprocess.run")
def test_run_shim_success(mock_run, tmp_path):
    shim = tmp_path / "rescue_shim.mjs"
    shim.touch()
    with patch.object(arcium_client, "SHIM_PATH", shim):
        mock_run.return_value = _proc(json.dumps({"ok": True, "result": 42}))
        data = arcium_client._run_shim("keygen")
    assert data["result"] == 42


@patch("subprocess.run")
def test_run_shim_ok_false_raises(mock_run, tmp_path):
    shim = tmp_path / "rescue_shim.mjs"
    shim.touch()
    with patch.object(arcium_client, "SHIM_PATH", shim):
        mock_run.return_value = _proc(json.dumps({"ok": False, "error": "bad input"}))
        with pytest.raises(RuntimeError, match="bad input"):
            arcium_client._run_shim("encrypt", "arg1")


@patch("subprocess.run")
def test_run_shim_non_json_stdout_raises(mock_run, tmp_path):
    shim = tmp_path / "rescue_shim.mjs"
    shim.touch()
    with patch.object(arcium_client, "SHIM_PATH", shim):
        mock_run.return_value = _proc("this is not json", returncode=1, stderr="stderr msg")
        with pytest.raises(RuntimeError, match="shim non-JSON output"):
            arcium_client._run_shim("broken")


@patch("subprocess.run")
def test_run_shim_falls_back_to_stderr_in_error_msg(mock_run, tmp_path):
    shim = tmp_path / "rescue_shim.mjs"
    shim.touch()
    with patch.object(arcium_client, "SHIM_PATH", shim):
        mock_run.return_value = _proc("", returncode=1, stderr="stderr detail")
        with pytest.raises(RuntimeError, match="stderr detail"):
            arcium_client._run_shim("cmd")


@patch("subprocess.run")
def test_run_shim_passes_stdin_data(mock_run, tmp_path):
    shim = tmp_path / "rescue_shim.mjs"
    shim.touch()
    with patch.object(arcium_client, "SHIM_PATH", shim):
        mock_run.return_value = _proc(json.dumps({"ok": True}))
        arcium_client._run_shim("decrypt", "arg1", stdin_data="my_secret")
    assert mock_run.call_args[1]["input"] == "my_secret"


@patch("subprocess.run")
def test_run_shim_no_stdin_when_not_provided(mock_run, tmp_path):
    shim = tmp_path / "rescue_shim.mjs"
    shim.touch()
    with patch.object(arcium_client, "SHIM_PATH", shim):
        mock_run.return_value = _proc(json.dumps({"ok": True}))
        arcium_client._run_shim("keygen")
    assert mock_run.call_args[1]["input"] is None


# ── rescue_keygen ─────────────────────────────────────────────────────────────

@patch("arcium_client._run_shim")
def test_rescue_keygen_calls_keygen(mock_shim):
    mock_shim.return_value = {"ok": True, "privkey_hex": "aabb", "pubkey_hex": "ccdd"}
    priv, pub = arcium_client.rescue_keygen()
    mock_shim.assert_called_once_with("keygen")
    assert priv == "aabb"
    assert pub == "ccdd"


# ── rescue_encrypt ────────────────────────────────────────────────────────────

@patch("arcium_client._run_shim")
def test_rescue_encrypt_passes_mxe_pubkey_and_values(mock_shim):
    mock_shim.return_value = {
        "ok": True, "ciphertexts": [[1, 2]], "pubkey_hex": "ab",
        "nonce_hex": "cd", "nonce_bn": "123", "shared_secret_hex": "ef",
    }
    arcium_client.rescue_encrypt("mxe_pubkey_hex", [999])
    args = mock_shim.call_args[0]
    assert args[0] == "encrypt"
    assert args[1] == "mxe_pubkey_hex"
    assert json.loads(args[2]) == [999]


@patch("arcium_client._run_shim")
def test_rescue_encrypt_with_nonce_appends_arg(mock_shim):
    mock_shim.return_value = {
        "ok": True, "ciphertexts": [], "pubkey_hex": "ab",
        "nonce_hex": "aabbcc", "nonce_bn": "0", "shared_secret_hex": "ef",
    }
    arcium_client.rescue_encrypt("mxe", [1], nonce_hex="aabbcc")
    args = mock_shim.call_args[0]
    assert "aabbcc" in args


@patch("arcium_client._run_shim")
def test_rescue_encrypt_without_nonce_omits_arg(mock_shim):
    mock_shim.return_value = {
        "ok": True, "ciphertexts": [], "pubkey_hex": "ab",
        "nonce_hex": "cd", "nonce_bn": "0", "shared_secret_hex": "ef",
    }
    arcium_client.rescue_encrypt("mxe", [1])
    args = mock_shim.call_args[0]
    assert len(args) == 3  # "encrypt", mxe_pubkey, values_json


# ── rescue_decrypt ────────────────────────────────────────────────────────────

@patch("arcium_client._run_shim")
def test_rescue_decrypt_passes_secret_via_stdin(mock_shim):
    mock_shim.return_value = {"ok": True, "values": ["100", "200"]}
    arcium_client.rescue_decrypt("my_shared_secret", [[1, 2, 3]], "nonce_hex")
    assert mock_shim.call_args[1]["stdin_data"] == "my_shared_secret"


@patch("arcium_client._run_shim")
def test_rescue_decrypt_returns_ints(mock_shim):
    mock_shim.return_value = {"ok": True, "values": ["42", "0", "999"]}
    result = arcium_client.rescue_decrypt("secret", [[]], "nonce")
    assert result == [42, 0, 999]
    assert all(isinstance(v, int) for v in result)


@patch("arcium_client._run_shim")
def test_rescue_decrypt_passes_ciphertexts_and_nonce_as_args(mock_shim):
    mock_shim.return_value = {"ok": True, "values": []}
    ciphertexts = [[1, 2, 3], [4, 5, 6]]
    arcium_client.rescue_decrypt("secret", ciphertexts, "my_nonce")
    args = mock_shim.call_args[0]
    assert args[0] == "decrypt"
    assert json.loads(args[1]) == ciphertexts
    assert args[2] == "my_nonce"


# ── rescue_shared_secret ──────────────────────────────────────────────────────

@patch("arcium_client._run_shim")
def test_rescue_shared_secret_passes_privkey_via_stdin(mock_shim):
    mock_shim.return_value = {"ok": True, "shared_secret_hex": "deadbeef"}
    arcium_client.rescue_shared_secret("my_privkey", "mxe_pubkey")
    assert mock_shim.call_args[1]["stdin_data"] == "my_privkey"


@patch("arcium_client._run_shim")
def test_rescue_shared_secret_passes_mxe_pubkey_as_arg(mock_shim):
    mock_shim.return_value = {"ok": True, "shared_secret_hex": "deadbeef"}
    arcium_client.rescue_shared_secret("privkey", "mxe_pubkey")
    args = mock_shim.call_args[0]
    assert args[0] == "shared_secret"
    assert args[1] == "mxe_pubkey"


@patch("arcium_client._run_shim")
def test_rescue_shared_secret_returns_hex(mock_shim):
    mock_shim.return_value = {"ok": True, "shared_secret_hex": "cafebabe"}
    result = arcium_client.rescue_shared_secret("priv", "pub")
    assert result == "cafebabe"


# ── ArciumBeacon (disabled path) ──────────────────────────────────────────────

def test_arcium_beacon_disabled_when_env_not_set(monkeypatch):
    monkeypatch.setenv("ARCIUM_ENABLED", "0")
    beacon = arcium_client.ArciumBeacon.from_env()
    assert not beacon.enabled


def test_arcium_beacon_none_client_is_disabled():
    beacon = arcium_client.ArciumBeacon(None)
    assert not beacon.enabled


def test_arcium_beacon_log_payment_stats_returns_none_when_disabled():
    beacon = arcium_client.ArciumBeacon(None)
    result = beacon.log_payment_stats(
        amount=1000,
        payer_token_account="pTA",
        recipient="recip",
        recipient_token_account="rTA",
        mint="mint",
    )
    assert result is None


def test_arcium_beacon_from_env_no_solana_package_disables(monkeypatch):
    """When solana package is missing, Arcium must disable even if ARCIUM_ENABLED=1."""
    monkeypatch.setenv("ARCIUM_ENABLED", "1")
    with patch.object(arcium_client, "HAS_SOLANA", False):
        beacon = arcium_client.ArciumBeacon.from_env()
    assert not beacon.enabled


# ── constants ─────────────────────────────────────────────────────────────────

def test_mxe_program_id_is_idl_address():
    # Must match declare_id! in programs/ble-revshare/src/lib.rs
    assert arcium_client.MXE_PROGRAM_ID == "7xeQNUggKc2e5q6AQxsFBLBkXGg2p54kSx11zVainMks"


def test_arcium_signer_pda_format():
    assert len(arcium_client.ARCIUM_SIGNER_PDA) >= 32
