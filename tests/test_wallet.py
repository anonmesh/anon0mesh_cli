"""
tests/test_wallet.py — unit tests for wallet.py
Requires: pip install solders
"""

import json
import base64
from pathlib import Path

import pytest
import state
import wallet

pytestmark = pytest.mark.skipif(
    not wallet.HAS_SOLDERS, reason="solders not installed"
)

from solders.keypair import Keypair

# A known valid base58 Hash string (all 1s — the default/zero hash)
ZERO_HASH = "11111111111111111111111111111111"


def _write_keypair(path: Path) -> Keypair:
    kp = Keypair()
    path.write_text(json.dumps(list(bytes(kp))))
    return kp


# ── generate_wallet ───────────────────────────────────────────────────────────

def test_generate_wallet_creates_file(tmp_path):
    path = str(tmp_path / "w.json")
    result = wallet.generate_wallet(path)
    assert result == path
    assert Path(path).exists()
    data = json.loads(Path(path).read_text())
    assert isinstance(data, list)
    assert len(data) == 64


def test_generate_wallet_sets_active_wallet(tmp_path):
    path = str(tmp_path / "w.json")
    wallet.generate_wallet(path)
    assert state.active_wallet is not None
    assert state.active_wallet["path"] == path
    assert len(state.active_wallet["pubkey"]) >= 32


def test_generate_wallet_default_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = wallet.generate_wallet(None)
    assert result is not None
    assert Path(result).exists()


def test_generate_wallet_keypair_is_valid(tmp_path):
    path = str(tmp_path / "w.json")
    wallet.generate_wallet(path)
    kp = Keypair.from_bytes(bytes(json.loads(Path(path).read_text())))
    assert str(kp.pubkey()) == state.active_wallet["pubkey"]


def test_generate_wallet_bad_path(capsys):
    result = wallet.generate_wallet("/nonexistent_dir/wallet.json")
    assert result is None
    assert "Failed to save" in capsys.readouterr().out


# ── import_wallet ─────────────────────────────────────────────────────────────

def test_import_wallet_hex64(tmp_path):
    kp = Keypair()
    path = str(tmp_path / "imported.json")
    result = wallet.import_wallet(bytes(kp).hex(), path)
    assert result == str(kp.pubkey())
    assert Path(path).exists()


def test_import_wallet_hex64_sets_active_wallet(tmp_path):
    kp = Keypair()
    path = str(tmp_path / "imported.json")
    wallet.import_wallet(bytes(kp).hex(), path)
    assert state.active_wallet["pubkey"] == str(kp.pubkey())
    assert state.active_wallet["path"] == path


def test_import_wallet_json_array(tmp_path):
    kp = Keypair()
    path = str(tmp_path / "imported.json")
    result = wallet.import_wallet(json.dumps(list(bytes(kp))), path)
    assert result == str(kp.pubkey())


def test_import_wallet_json_array_saves_file(tmp_path):
    kp = Keypair()
    path = str(tmp_path / "imported.json")
    wallet.import_wallet(json.dumps(list(bytes(kp))), path)
    saved = json.loads(Path(path).read_text())
    assert saved == list(bytes(kp))


def test_import_wallet_invalid_json_array(tmp_path, capsys):
    path = str(tmp_path / "bad.json")
    result = wallet.import_wallet("[not valid json}", path)
    assert result is None
    assert "failed" in capsys.readouterr().out.lower()


def test_import_wallet_hex_wrong_length(tmp_path, capsys):
    path = str(tmp_path / "bad.json")
    result = wallet.import_wallet("deadbeef", path)  # 4 bytes — not 32 or 64
    assert result is None
    assert "must be 64 or 128" in capsys.readouterr().out


def test_import_wallet_invalid_base58(tmp_path, capsys):
    path = str(tmp_path / "bad.json")
    result = wallet.import_wallet("not_a_valid_base58_keypair!", path)
    assert result is None


# ── scan_nonce_accounts ───────────────────────────────────────────────────────

def test_scan_nonce_accounts_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert wallet.scan_nonce_accounts() == []


def test_scan_nonce_accounts_finds_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kp1 = Keypair()
    kp2 = Keypair()
    (tmp_path / "nonce_aaaaaaaa.json").write_text(json.dumps(list(bytes(kp1))))
    (tmp_path / "nonce_bbbbbbbb.json").write_text(json.dumps(list(bytes(kp2))))
    result = wallet.scan_nonce_accounts()
    assert len(result) == 2
    pubkeys = {r["pubkey"] for r in result}
    assert str(kp1.pubkey()) in pubkeys
    assert str(kp2.pubkey()) in pubkeys


def test_scan_nonce_accounts_skips_corrupt(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kp = Keypair()
    (tmp_path / "nonce_good.json").write_text(json.dumps(list(bytes(kp))))
    (tmp_path / "nonce_bad.json").write_text("not json at all!")
    result = wallet.scan_nonce_accounts()
    assert len(result) == 1
    assert result[0]["pubkey"] == str(kp.pubkey())


def test_scan_nonce_accounts_ignores_non_nonce_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kp = Keypair()
    (tmp_path / "wallet.json").write_text(json.dumps(list(bytes(kp))))
    (tmp_path / "wallet_abc.json").write_text(json.dumps(list(bytes(kp))))
    assert wallet.scan_nonce_accounts() == []


def test_scan_nonce_accounts_returns_path_and_pubkey(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kp = Keypair()
    (tmp_path / "nonce_test1234.json").write_text(json.dumps(list(bytes(kp))))
    result = wallet.scan_nonce_accounts()
    assert "path" in result[0]
    assert "pubkey" in result[0]
    assert result[0]["pubkey"] == str(kp.pubkey())


# ── auto_load_wallet ──────────────────────────────────────────────────────────

def test_auto_load_finds_wallet_json(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kp = Keypair()
    (tmp_path / "wallet.json").write_text(json.dumps(list(bytes(kp))))
    state.active_wallet = None
    wallet.auto_load_wallet()
    assert state.active_wallet is not None
    assert state.active_wallet["pubkey"] == str(kp.pubkey())
    assert "wallet.json" in state.active_wallet["path"]


def test_auto_load_finds_wallet_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kp = Keypair()
    (tmp_path / "wallet_abc12345.json").write_text(json.dumps(list(bytes(kp))))
    state.active_wallet = None
    wallet.auto_load_wallet()
    assert state.active_wallet is not None
    assert state.active_wallet["pubkey"] == str(kp.pubkey())


def test_auto_load_prefers_wallet_json_over_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    kp_main   = Keypair()
    kp_prefix = Keypair()
    (tmp_path / "wallet.json").write_text(json.dumps(list(bytes(kp_main))))
    (tmp_path / "wallet_abc.json").write_text(json.dumps(list(bytes(kp_prefix))))
    state.active_wallet = None
    wallet.auto_load_wallet()
    assert state.active_wallet["pubkey"] == str(kp_main.pubkey())


def test_auto_load_skips_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "wallet.json").write_text("not valid json")
    state.active_wallet = None
    wallet.auto_load_wallet()
    assert state.active_wallet is None


def test_auto_load_nothing_found(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    state.active_wallet = None
    wallet.auto_load_wallet()
    assert state.active_wallet is None


# ── offline_sign_nonce_transfer ───────────────────────────────────────────────

def test_offline_sign_nonce_transfer_returns_base64(tmp_path):
    payer    = _write_keypair(tmp_path / "payer.json")
    auth     = _write_keypair(tmp_path / "auth.json")
    nonce_kp = Keypair()
    to       = Keypair()

    tx = wallet.offline_sign_nonce_transfer(
        str(tmp_path / "payer.json"),
        str(nonce_kp.pubkey()),
        str(tmp_path / "auth.json"),
        str(to.pubkey()),
        100_000,
        ZERO_HASH,
    )
    assert tx is not None
    decoded = base64.b64decode(tx)
    assert len(decoded) > 0


def test_offline_sign_nonce_transfer_same_payer_and_auth(tmp_path):
    payer = _write_keypair(tmp_path / "payer.json")
    to    = Keypair()
    tx = wallet.offline_sign_nonce_transfer(
        str(tmp_path / "payer.json"),
        str(Keypair().pubkey()),
        str(tmp_path / "payer.json"),   # authority == payer
        str(to.pubkey()),
        1,
        ZERO_HASH,
    )
    assert tx is not None


def test_offline_sign_nonce_transfer_missing_keypair(tmp_path, capsys):
    result = wallet.offline_sign_nonce_transfer(
        str(tmp_path / "nonexistent.json"),
        str(Keypair().pubkey()),
        str(tmp_path / "nonexistent.json"),
        str(Keypair().pubkey()),
        100,
        ZERO_HASH,
    )
    assert result is None
    assert "Failed to load" in capsys.readouterr().out


def test_offline_sign_nonce_transfer_zero_lamports_still_signs(tmp_path):
    payer = _write_keypair(tmp_path / "payer.json")
    to    = Keypair()
    tx = wallet.offline_sign_nonce_transfer(
        str(tmp_path / "payer.json"),
        str(Keypair().pubkey()),
        str(tmp_path / "payer.json"),
        str(to.pubkey()),
        0,
        ZERO_HASH,
    )
    # 0 lamports is valid for signing (the tx is legal, Solana may reject it)
    assert tx is not None


# ── partial_sign_arcium_transfer ──────────────────────────────────────────────

def test_partial_sign_arcium_transfer_returns_base64(tmp_path):
    payer   = _write_keypair(tmp_path / "payer.json")
    beacon  = Keypair()
    to      = Keypair()
    nonce   = Keypair()

    tx = wallet.partial_sign_arcium_transfer(
        str(tmp_path / "payer.json"),
        str(beacon.pubkey()),
        str(nonce.pubkey()),
        str(to.pubkey()),
        500_000,
        ZERO_HASH,
    )
    assert tx is not None
    decoded = base64.b64decode(tx)
    assert len(decoded) > 0


def test_partial_sign_arcium_transfer_missing_keypair(tmp_path, capsys):
    result = wallet.partial_sign_arcium_transfer(
        str(tmp_path / "missing.json"),
        str(Keypair().pubkey()),
        str(Keypair().pubkey()),
        str(Keypair().pubkey()),
        1_000,
        ZERO_HASH,
    )
    assert result is None
    assert "Failed to load" in capsys.readouterr().out


def test_partial_sign_arcium_transfer_invalid_address(tmp_path, capsys):
    _write_keypair(tmp_path / "payer.json")
    result = wallet.partial_sign_arcium_transfer(
        str(tmp_path / "payer.json"),
        "not-a-valid-pubkey",
        "also-invalid",
        "still-invalid",
        1_000,
        ZERO_HASH,
    )
    assert result is None
    assert "Invalid address" in capsys.readouterr().out


# ── create_nonce_account (instruction build) ──────────────────────────────────

def test_create_nonce_account_authority_param_name(tmp_path, monkeypatch):
    """
    Regression: InitializeNonceAccountParams uses 'authority', not 'authorized_pubkey'.
    Mock out RPC calls and verify the instruction builds without ValueError.
    """
    from unittest.mock import patch, MagicMock
    _write_keypair(tmp_path / "payer.json")

    mock_rpc = MagicMock()
    mock_rpc.return_value = {"result": 1_447_680}          # rent
    mock_blockhash = MagicMock(return_value="4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi")
    mock_send = MagicMock(return_value={"result": "SIG"})

    with patch("wallet.rpc_call", mock_rpc), \
         patch("wallet.get_recent_blockhash", mock_blockhash), \
         patch("wallet.rpc_call", mock_send):
        # The call must not raise ValueError: Missing required key: authority
        try:
            wallet.create_nonce_account(str(tmp_path / "payer.json"), None, None)
        except ValueError as e:
            pytest.fail(f"InitializeNonceAccountParams raised ValueError: {e}")
