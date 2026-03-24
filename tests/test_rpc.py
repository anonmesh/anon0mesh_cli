"""
tests/test_rpc.py — unit tests for rpc.py
All network calls are intercepted by mocking state.pool.call().
"""

import pytest
from unittest.mock import MagicMock
import state
import rpc


@pytest.fixture(autouse=True)
def mock_pool():
    state.pool = MagicMock()
    return state.pool


# ── _extract_result ────────────────────────────────────────────────────────────

def test_extract_result_value_dict():
    assert rpc._extract_result({"result": {"value": 42}}) == 42


def test_extract_result_direct_int():
    assert rpc._extract_result({"result": 99}) == 99


def test_extract_result_direct_dict_without_value():
    inner = {"blockhash": "abc"}
    assert rpc._extract_result({"result": inner}) == inner


def test_extract_result_none_resp():
    assert rpc._extract_result(None) is None


def test_extract_result_value_none():
    assert rpc._extract_result({"result": {"value": None}}) is None


# ── get_balance ────────────────────────────────────────────────────────────────

def test_get_balance_value_dict(mock_pool, capsys):
    mock_pool.call.return_value = {"result": {"value": 2_000_000_000}}
    rpc.get_balance("addr1")
    assert "2.000000000 SOL" in capsys.readouterr().out


def test_get_balance_plain_int(mock_pool, capsys):
    mock_pool.call.return_value = {"result": 500_000_000}
    rpc.get_balance("addr1")
    assert "0.500000000 SOL" in capsys.readouterr().out


def test_get_balance_zero(mock_pool, capsys):
    mock_pool.call.return_value = {"result": 0}
    rpc.get_balance("addr1")
    assert "0.000000000 SOL" in capsys.readouterr().out


def test_get_balance_shows_address(mock_pool, capsys):
    mock_pool.call.return_value = {"result": 1_000}
    rpc.get_balance("MyWalletAddr")
    assert "MyWalletAddr" in capsys.readouterr().out


def test_get_balance_error(mock_pool, capsys):
    mock_pool.call.return_value = {"error": {"message": "invalid base58"}}
    rpc.get_balance("bad")
    assert "invalid base58" in capsys.readouterr().out


def test_get_balance_none_response(mock_pool, capsys):
    mock_pool.call.return_value = None
    rpc.get_balance("addr1")  # must not raise
    capsys.readouterr()


# ── get_slot ───────────────────────────────────────────────────────────────────

def test_get_slot_ok(mock_pool, capsys):
    mock_pool.call.return_value = {"result": 1_234_567}
    rpc.get_slot()
    assert "1,234,567" in capsys.readouterr().out


def test_get_slot_none(mock_pool, capsys):
    mock_pool.call.return_value = None
    rpc.get_slot()
    assert "No response" in capsys.readouterr().out


def test_get_slot_error(mock_pool, capsys):
    mock_pool.call.return_value = {"error": "bad request"}
    rpc.get_slot()
    assert "RPC error" in capsys.readouterr().out


# ── get_block_height ──────────────────────────────────────────────────────────

def test_get_block_height_ok(mock_pool, capsys):
    mock_pool.call.return_value = {"result": 9_000_000}
    rpc.get_block_height()
    assert "9,000,000" in capsys.readouterr().out


def test_get_block_height_none(mock_pool, capsys):
    mock_pool.call.return_value = None
    rpc.get_block_height()
    assert "No response" in capsys.readouterr().out


# ── get_transaction_count ─────────────────────────────────────────────────────

def test_get_transaction_count_ok(mock_pool, capsys):
    mock_pool.call.return_value = {"result": 1_000_000}
    rpc.get_transaction_count()
    assert "1,000,000" in capsys.readouterr().out


def test_get_transaction_count_none(mock_pool, capsys):
    mock_pool.call.return_value = None
    rpc.get_transaction_count()
    assert "No response" in capsys.readouterr().out


# ── get_recent_blockhash ──────────────────────────────────────────────────────

def test_get_recent_blockhash_ok(mock_pool, capsys):
    bh = "4vJ9JU1bJJE96FWSJKvHsmmFADCg4gpZQff4P3bkLKi"
    mock_pool.call.return_value = {"result": {"value": {"blockhash": bh}}}
    result = rpc.get_recent_blockhash()
    assert result == bh
    assert bh in capsys.readouterr().out


def test_get_recent_blockhash_none(mock_pool):
    mock_pool.call.return_value = None
    assert rpc.get_recent_blockhash() is None


def test_get_recent_blockhash_error(mock_pool):
    mock_pool.call.return_value = {"error": {"message": "unavailable"}}
    assert rpc.get_recent_blockhash() is None


# ── get_beacon_pubkey ─────────────────────────────────────────────────────────

def test_get_beacon_pubkey_ok(mock_pool):
    mock_pool.call.return_value = {"result": "BeaconPubkeyABC"}
    assert rpc.get_beacon_pubkey() == "BeaconPubkeyABC"


def test_get_beacon_pubkey_none(mock_pool):
    mock_pool.call.return_value = None
    assert rpc.get_beacon_pubkey() is None


def test_get_beacon_pubkey_error(mock_pool, capsys):
    mock_pool.call.return_value = {"error": {"message": "not configured"}}
    assert rpc.get_beacon_pubkey() is None
    assert "not configured" in capsys.readouterr().out


# ── cosign_and_send ───────────────────────────────────────────────────────────

def test_cosign_and_send_success(mock_pool, capsys):
    mock_pool.call.return_value = {"result": "SIG123abc"}
    sig = rpc.cosign_and_send("tx_b64")
    assert sig == "SIG123abc"
    assert "SIG123abc" in capsys.readouterr().out


def test_cosign_and_send_error(mock_pool, capsys):
    mock_pool.call.return_value = {"error": {"message": "co-sign rejected"}}
    assert rpc.cosign_and_send("tx_b64") is None
    assert "co-sign rejected" in capsys.readouterr().out


def test_cosign_and_send_none_response(mock_pool):
    mock_pool.call.return_value = None
    assert rpc.cosign_and_send("tx_b64") is None


def test_cosign_and_send_arcium_meta_forwarded(mock_pool):
    mock_pool.call.return_value = {"result": "SIG"}
    rpc.cosign_and_send("tx_b64", arcium_meta={"amount": 1000, "mint": "ABC"})
    _, params = mock_pool.call.call_args[0]
    assert params[1] == {"arcium": {"amount": 1000, "mint": "ABC"}}


def test_cosign_and_send_no_meta_single_param(mock_pool):
    mock_pool.call.return_value = {"result": "SIG"}
    rpc.cosign_and_send("tx_b64")
    _, params = mock_pool.call.call_args[0]
    assert len(params) == 1


# ── send_transaction ──────────────────────────────────────────────────────────

def test_send_transaction_ok(mock_pool, capsys):
    mock_pool.call.return_value = {"result": "TX_SIGNATURE"}
    rpc.send_transaction("b64tx")
    assert "TX_SIGNATURE" in capsys.readouterr().out


def test_send_transaction_error(mock_pool, capsys):
    mock_pool.call.return_value = {"error": {"message": "blockhash expired"}}
    rpc.send_transaction("b64tx")
    assert "blockhash expired" in capsys.readouterr().out


def test_send_transaction_none(mock_pool, capsys):
    mock_pool.call.return_value = None
    rpc.send_transaction("b64tx")  # must not raise
    capsys.readouterr()


# ── simulate_transaction ──────────────────────────────────────────────────────

def test_simulate_transaction_success(mock_pool, capsys):
    mock_pool.call.return_value = {
        "result": {"value": {"err": None, "logs": ["Program log: OK"]}}
    }
    rpc.simulate_transaction("b64tx")
    assert "Program log: OK" in capsys.readouterr().out


def test_simulate_transaction_error(mock_pool, capsys):
    mock_pool.call.return_value = {
        "result": {"value": {"err": {"InstructionError": [0, "Custom"]}, "logs": []}}
    }
    rpc.simulate_transaction("b64tx")
    assert "Simulation error" in capsys.readouterr().out


# ── get_nonce_account ─────────────────────────────────────────────────────────

_NONCE_RESP = {
    "result": {"value": {
        "data": {"parsed": {
            "type": "initialized",
            "info": {
                "blockhash": "NONCE_HASH_VALUE",
                "authority": "AUTHORITY_PUBKEY",
            }
        }}
    }}
}


def test_get_nonce_account_ok(mock_pool, capsys):
    mock_pool.call.return_value = _NONCE_RESP
    result = rpc.get_nonce_account("NONCE_PUBKEY")
    assert result == {"nonce": "NONCE_HASH_VALUE", "authority": "AUTHORITY_PUBKEY"}


def test_get_nonce_account_shows_info(mock_pool, capsys):
    mock_pool.call.return_value = _NONCE_RESP
    rpc.get_nonce_account("NONCE_PUBKEY")
    out = capsys.readouterr().out
    assert "NONCE_HASH_VALUE" in out
    assert "AUTHORITY_PUBKEY" in out


def test_get_nonce_account_not_found(mock_pool, capsys):
    mock_pool.call.return_value = {"result": {"value": None}}
    assert rpc.get_nonce_account("MISSING") is None


def test_get_nonce_account_uninitialized(mock_pool, capsys):
    mock_pool.call.return_value = {
        "result": {"value": {"data": {"parsed": {"type": "uninitialized", "info": {}}}}}
    }
    assert rpc.get_nonce_account("NONCE_PUBKEY") is None


def test_get_nonce_account_none_response(mock_pool, capsys):
    mock_pool.call.return_value = None
    assert rpc.get_nonce_account("NONCE_PUBKEY") is None


def test_get_nonce_account_rpc_error(mock_pool, capsys):
    mock_pool.call.return_value = {"error": "connection refused"}
    assert rpc.get_nonce_account("NONCE_PUBKEY") is None
