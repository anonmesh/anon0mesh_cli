"""
tests/test_shared.py — unit tests for shared.py
"""

import json
import shared


# ── build_rpc ─────────────────────────────────────────────────────────────────

def test_build_rpc_structure():
    payload = json.loads(shared.build_rpc("getSlot"))
    assert payload["jsonrpc"] == "2.0"
    assert payload["method"] == "getSlot"
    assert payload["params"] == []
    assert payload["id"] == 1


def test_build_rpc_with_params():
    payload = json.loads(shared.build_rpc("getBalance", ["addr123"], req_id=7))
    assert payload["params"] == ["addr123"]
    assert payload["id"] == 7


def test_build_rpc_none_params_becomes_empty_list():
    payload = json.loads(shared.build_rpc("getSlot", None))
    assert payload["params"] == []


def test_build_rpc_returns_bytes():
    result = shared.build_rpc("getSlot")
    assert isinstance(result, bytes)


# ── build_response ─────────────────────────────────────────────────────────────

def test_build_response_result():
    payload = json.loads(shared.build_response(result=42))
    assert payload["result"] == 42
    assert "error" not in payload


def test_build_response_result_dict():
    payload = json.loads(shared.build_response(result={"value": 99}))
    assert payload["result"] == {"value": 99}


def test_build_response_error():
    payload = json.loads(shared.build_response(error="something went wrong"))
    assert payload["error"]["message"] == "something went wrong"
    assert payload["error"]["code"] == -32000
    assert "result" not in payload


def test_build_response_req_id():
    payload = json.loads(shared.build_response(result=1, req_id=99))
    assert payload["id"] == 99


def test_build_response_returns_bytes():
    assert isinstance(shared.build_response(result=1), bytes)


# ── decode_json ────────────────────────────────────────────────────────────────

def test_decode_json_roundtrip():
    original = {"method": "getSlot", "params": [1, 2, 3]}
    assert shared.decode_json(json.dumps(original).encode()) == original


def test_decode_json_nested():
    data = {"result": {"value": {"blockhash": "abc"}}}
    assert shared.decode_json(json.dumps(data).encode()) == data


# ── quiet mode ─────────────────────────────────────────────────────────────────

def test_log_info_visible_by_default(capsys):
    shared.set_quiet(False)
    shared.log_info("hello from log_info")
    assert "hello from log_info" in capsys.readouterr().out


def test_log_info_suppressed_when_quiet(capsys):
    shared.set_quiet(True)
    shared.log_info("should be hidden")
    assert "should be hidden" not in capsys.readouterr().out


def test_log_tx_suppressed_when_quiet(capsys):
    shared.set_quiet(True)
    shared.log_tx("tx hidden")
    assert "tx hidden" not in capsys.readouterr().out


def test_log_tx_visible_when_not_quiet(capsys):
    shared.set_quiet(False)
    shared.log_tx("tx visible")
    assert "tx visible" in capsys.readouterr().out


def test_log_ok_always_visible_even_when_quiet(capsys):
    shared.set_quiet(True)
    shared.log_ok("ok message")
    assert "ok message" in capsys.readouterr().out


def test_log_warn_always_visible_even_when_quiet(capsys):
    shared.set_quiet(True)
    shared.log_warn("warn message")
    assert "warn message" in capsys.readouterr().out


def test_log_err_always_visible_even_when_quiet(capsys):
    shared.set_quiet(True)
    shared.log_err("err message")
    assert "err message" in capsys.readouterr().out


def test_set_quiet_false_restores_log_info(capsys):
    shared.set_quiet(True)
    shared.set_quiet(False)
    shared.log_info("restored")
    assert "restored" in capsys.readouterr().out


def test_log_ok_contains_checkmark(capsys):
    shared.log_ok("done")
    assert "✔" in capsys.readouterr().out


def test_log_err_contains_cross(capsys):
    shared.log_err("failed")
    assert "✘" in capsys.readouterr().out


def test_log_warn_contains_warning_symbol(capsys):
    shared.log_warn("watch out")
    assert "⚠" in capsys.readouterr().out
