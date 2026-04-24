"""
tests/test_mesh.py — unit tests for mesh.py
============================================
Covers BeaconLink, BeaconPool, BeaconAnnounceHandler, synthesize_tunnel patch,
and startup helpers.  All RNS primitives are mocked — no network required.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# We need to mock RNS before importing mesh.py.  mesh.py does `import RNS`
# at module level and patches Transport.synthesize_tunnel.  We mock the whole
# module so the import succeeds without Reticulum installed.

import sys
_mock_RNS = MagicMock()
_mock_RNS.Reticulum.TRUNCATED_HASHLENGTH = 128  # 16 bytes → 32 hex chars
_mock_RNS.Destination.OUT = 1
_mock_RNS.Destination.SINGLE = 2
# Give synthesize_tunnel a real callable so the monkey-patch works
_mock_RNS.Transport.synthesize_tunnel = staticmethod(lambda iface: None)
_mock_RNS.Transport.identity = MagicMock()       # non-None by default
sys.modules["RNS"] = _mock_RNS

import state as _state
import mesh
from mesh import BeaconLink, BeaconPool, BeaconAnnounceHandler
from shared import ANNOUNCE_DATA, compress_response, decompress_response


# ── Helpers ──────────────────────────────────────────────────────────────────

VALID_HASH_HEX = "aa" * 16                      # 32 hex chars = 16 bytes
VALID_HASH_BYTES = bytes.fromhex(VALID_HASH_HEX)
VALID_HASH_HEX_2 = "bb" * 16


def _make_mock_identity():
    ident = MagicMock()
    ident.hash = b"\xcc" * 16
    return ident


def _make_established_link(bl: BeaconLink):
    """Simulate a link being established on a BeaconLink."""
    mock_link = MagicMock()
    bl._on_established(mock_link)
    return mock_link


# ═════════════════════════════════════════════════════════════════════════════
# synthesize_tunnel patch
# ═════════════════════════════════════════════════════════════════════════════

class TestSynthesizeTunnelPatch:

    def test_skips_when_transport_identity_is_none(self):
        """When Transport.identity is None the patch must not call the original."""
        original = MagicMock()
        saved = mesh._orig_st
        mesh._orig_st = original
        _mock_RNS.Transport.identity = None
        try:
            mesh._safe_st("fake_interface")
            original.assert_not_called()
        finally:
            mesh._orig_st = saved
            _mock_RNS.Transport.identity = MagicMock()

    def test_calls_original_when_identity_exists(self):
        original = MagicMock()
        saved = mesh._orig_st
        mesh._orig_st = original
        _mock_RNS.Transport.identity = MagicMock()
        try:
            mesh._safe_st("iface")
            original.assert_called_once_with("iface")
        finally:
            mesh._orig_st = saved

    def test_swallows_attribute_error_from_original(self):
        original = MagicMock(side_effect=AttributeError("boom"))
        saved = mesh._orig_st
        mesh._orig_st = original
        _mock_RNS.Transport.identity = MagicMock()
        try:
            mesh._safe_st("iface")  # should not raise
        finally:
            mesh._orig_st = saved


# ═════════════════════════════════════════════════════════════════════════════
# BeaconLink — construction
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconLinkConstruction:

    def test_stores_normalised_hash(self):
        bl = BeaconLink("  " + VALID_HASH_HEX.upper() + "  ")
        assert bl.dest_hash_hex == VALID_HASH_HEX
        assert bl.dest_hash == VALID_HASH_BYTES

    def test_custom_label(self):
        bl = BeaconLink(VALID_HASH_HEX, label="mybeacon")
        assert bl.label == "mybeacon"

    def test_auto_label(self):
        bl = BeaconLink(VALID_HASH_HEX)
        assert bl.label == VALID_HASH_HEX[:12] + "..."

    def test_starts_inactive(self):
        bl = BeaconLink(VALID_HASH_HEX)
        assert bl.active is False
        assert bl.link is None
        assert not bl.ready.is_set()


# ═════════════════════════════════════════════════════════════════════════════
# BeaconLink — link lifecycle
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconLinkLifecycle:

    def test_on_established_sets_active(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = MagicMock()
        _state.identify_self = False
        bl._on_established(mock_link)

        assert bl.active is True
        assert bl.link is mock_link
        assert bl.ready.is_set()
        assert bl._retry_count == 0
        assert bl._reconnecting is False
        mock_link.set_link_closed_callback.assert_called_once_with(bl._on_closed)

    def test_on_established_identifies_when_configured(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = MagicMock()
        fake_id = _make_mock_identity()
        _state.identify_self = True
        _state.client_identity = fake_id
        bl._on_established(mock_link)

        mock_link.identify.assert_called_once_with(fake_id)

    def test_on_closed_clears_active_and_ready(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        mock_link.teardown_reason = "timeout"
        # Prevent actual reconnect thread from spawning
        bl._removed = True
        bl._on_closed(mock_link)

        assert bl.active is False
        assert not bl.ready.is_set()

    def test_on_closed_does_not_reconnect_when_removed(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        mock_link.teardown_reason = "timeout"
        bl._removed = True
        with patch.object(bl, "_schedule_reconnect") as mock_sched:
            bl._on_closed(mock_link)
            mock_sched.assert_not_called()

    def test_on_closed_schedules_reconnect(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        mock_link.teardown_reason = "timeout"
        with patch.object(bl, "_schedule_reconnect") as mock_sched:
            bl._on_closed(mock_link)
            mock_sched.assert_called_once()

    def test_teardown_sets_removed_and_tears_down_link(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        bl.teardown()

        assert bl._removed is True
        assert bl.active is False
        mock_link.teardown.assert_called_once()

    def test_teardown_tolerates_no_link(self):
        bl = BeaconLink(VALID_HASH_HEX)
        bl.teardown()  # should not raise
        assert bl._removed is True

    def test_teardown_swallows_link_exception(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        mock_link.teardown.side_effect = Exception("already torn down")
        bl.teardown()  # should not raise

    def test_repr_active(self):
        bl = BeaconLink(VALID_HASH_HEX, label="test")
        _make_established_link(bl)
        assert "ACTIVE" in repr(bl)

    def test_repr_down(self):
        bl = BeaconLink(VALID_HASH_HEX, label="test")
        assert "DOWN" in repr(bl)


# ═════════════════════════════════════════════════════════════════════════════
# BeaconLink — connect
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconLinkConnect:

    def test_connect_with_cached_path(self):
        bl = BeaconLink(VALID_HASH_HEX)
        _mock_RNS.Transport.has_path.return_value = True
        _mock_RNS.Identity.recall.return_value = _make_mock_identity()

        mock_link_instance = MagicMock()
        _mock_RNS.Link.return_value = mock_link_instance

        # Simulate immediate link establishment in a side effect
        def establish_side_effect(cb):
            cb(mock_link_instance)
        mock_link_instance.set_link_established_callback.side_effect = establish_side_effect
        _state.identify_self = False

        result = bl.connect(timeout=5.0)

        assert result is True
        assert bl.active is True
        _mock_RNS.Identity.recall.assert_called_with(bl.dest_hash)

    def test_connect_path_resolution_timeout(self):
        bl = BeaconLink(VALID_HASH_HEX)
        _mock_RNS.Transport.has_path.return_value = False

        result = bl.connect(timeout=0.3)

        assert result is False
        _mock_RNS.Transport.request_path.assert_called_with(bl.dest_hash)

    def test_connect_identity_recall_fails(self):
        bl = BeaconLink(VALID_HASH_HEX)
        _mock_RNS.Transport.has_path.return_value = True
        _mock_RNS.Identity.recall.return_value = None

        result = bl.connect(timeout=5.0)

        assert result is False

    def test_connect_link_establishment_timeout(self):
        bl = BeaconLink(VALID_HASH_HEX)
        _mock_RNS.Transport.has_path.return_value = True
        _mock_RNS.Identity.recall.return_value = _make_mock_identity()
        # Don't trigger the established callback → times out
        _mock_RNS.Link.return_value = MagicMock()

        result = bl.connect(timeout=0.3)

        assert result is False

    def test_connect_with_identity_fast_path(self):
        bl = BeaconLink(VALID_HASH_HEX)
        ident = _make_mock_identity()
        mock_link_instance = MagicMock()
        _mock_RNS.Link.return_value = mock_link_instance

        def establish_side_effect(cb):
            cb(mock_link_instance)
        mock_link_instance.set_link_established_callback.side_effect = establish_side_effect
        _state.identify_self = False

        result = bl.connect_with_identity(ident, timeout=5.0)

        assert result is True
        assert bl._last_identity is ident

    def test_connect_with_identity_timeout(self):
        bl = BeaconLink(VALID_HASH_HEX)
        _mock_RNS.Link.return_value = MagicMock()

        result = bl.connect_with_identity(_make_mock_identity(), timeout=0.3)

        assert result is False


# ═════════════════════════════════════════════════════════════════════════════
# BeaconLink — refresh_identity
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconLinkRefreshIdentity:

    def test_noop_when_active(self):
        bl = BeaconLink(VALID_HASH_HEX)
        _make_established_link(bl)
        with patch.object(bl, "_schedule_reconnect") as mock_sched:
            bl.refresh_identity(_make_mock_identity())
            mock_sched.assert_not_called()

    def test_triggers_reconnect_when_down_and_not_reconnecting(self):
        bl = BeaconLink(VALID_HASH_HEX)
        with patch.object(bl, "_schedule_reconnect") as mock_sched:
            bl.refresh_identity(_make_mock_identity())
            mock_sched.assert_called_once()

    def test_updates_identity_when_already_reconnecting(self):
        bl = BeaconLink(VALID_HASH_HEX)
        bl._reconnecting = True
        new_ident = _make_mock_identity()
        with patch.object(bl, "_schedule_reconnect") as mock_sched:
            bl.refresh_identity(new_ident)
            mock_sched.assert_not_called()
        assert bl._last_identity is new_ident


# ═════════════════════════════════════════════════════════════════════════════
# BeaconLink — request
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconLinkRequest:

    def test_request_when_not_active_is_noop(self):
        bl = BeaconLink(VALID_HASH_HEX)
        result_holder = [None, None]
        event = threading.Event()
        bl.request(b"payload", event, result_holder, 5.0)
        assert not event.is_set()

    def test_request_sends_to_link(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        result_holder = [None, None]
        event = threading.Event()
        bl.request(b"payload", event, result_holder, 5.0)

        mock_link.request.assert_called_once()
        call_args = mock_link.request.call_args
        assert call_args.kwargs["data"] == b"payload"
        assert call_args.args[0] == "/rpc"

    def test_request_response_callback_decompresses_and_sets_result(self):
        bl = BeaconLink(VALID_HASH_HEX, label="test-beacon")
        mock_link = _make_established_link(bl)
        result_holder = [None, None]
        event = threading.Event()
        bl.request(b"payload", event, result_holder, 5.0)

        # Extract the response callback
        call_args = mock_link.request.call_args
        on_response = call_args.kwargs["response_callback"]

        # Simulate a response with compressed data
        rpc_response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": 42}).encode()
        compressed = compress_response(rpc_response)

        receipt = MagicMock()
        receipt.response = compressed
        on_response(receipt)

        assert event.is_set()
        assert result_holder[0] == {"jsonrpc": "2.0", "id": 1, "result": 42}
        assert result_holder[1] == "test-beacon"

    def test_request_response_callback_handles_uncompressed(self):
        bl = BeaconLink(VALID_HASH_HEX, label="test-beacon")
        mock_link = _make_established_link(bl)
        result_holder = [None, None]
        event = threading.Event()
        bl.request(b"payload", event, result_holder, 5.0)

        on_response = mock_link.request.call_args.kwargs["response_callback"]

        raw = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"}).encode()
        receipt = MagicMock()
        receipt.response = raw
        on_response(receipt)

        assert result_holder[0]["result"] == "ok"

    def test_request_response_ignores_non_rpc(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        result_holder = [None, None]
        event = threading.Event()
        bl.request(b"payload", event, result_holder, 5.0)

        on_response = mock_link.request.call_args.kwargs["response_callback"]
        receipt = MagicMock()
        receipt.response = json.dumps({"random": "data"}).encode()
        on_response(receipt)

        assert not event.is_set()
        assert result_holder[0] is None

    def test_request_response_ignores_none_response(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        result_holder = [None, None]
        event = threading.Event()
        bl.request(b"payload", event, result_holder, 5.0)

        on_response = mock_link.request.call_args.kwargs["response_callback"]
        receipt = MagicMock()
        receipt.response = None
        on_response(receipt)

        assert not event.is_set()

    def test_request_first_response_wins(self):
        """If result_holder already has a value, subsequent responses are ignored."""
        bl = BeaconLink(VALID_HASH_HEX, label="beacon-A")
        mock_link = _make_established_link(bl)
        result_holder = [{"jsonrpc": "2.0", "id": 1, "result": "first"}, "other"]
        event = threading.Event()
        event.set()
        bl.request(b"payload", event, result_holder, 5.0)

        on_response = mock_link.request.call_args.kwargs["response_callback"]
        receipt = MagicMock()
        receipt.response = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "second"}).encode()
        on_response(receipt)

        assert result_holder[0]["result"] == "first"

    def test_request_handles_send_error(self):
        bl = BeaconLink(VALID_HASH_HEX)
        mock_link = _make_established_link(bl)
        mock_link.request.side_effect = Exception("link broken")
        result_holder = [None, None]
        event = threading.Event()
        bl.request(b"payload", event, result_holder, 5.0)  # should not raise


# ═════════════════════════════════════════════════════════════════════════════
# BeaconLink — reconnection backoff
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconLinkReconnect:

    def test_schedule_reconnect_guards_double_entry(self):
        bl = BeaconLink(VALID_HASH_HEX)
        bl._reconnecting = True
        with patch("threading.Thread") as mock_thread:
            bl._schedule_reconnect()
            mock_thread.assert_not_called()

    def test_schedule_reconnect_guards_removed(self):
        bl = BeaconLink(VALID_HASH_HEX)
        bl._removed = True
        with patch("threading.Thread") as mock_thread:
            bl._schedule_reconnect()
            mock_thread.assert_not_called()

    def test_backoff_schedule_values(self):
        assert mesh._BACKOFF == [5, 10, 20, 40, 60, 120, 300]

    def test_backoff_clamps_to_max(self):
        """After exhausting the schedule, delay stays at the last value (300s)."""
        bl = BeaconLink(VALID_HASH_HEX)
        bl._retry_count = 100
        delay = mesh._BACKOFF[min(bl._retry_count, len(mesh._BACKOFF) - 1)]
        assert delay == 300


# ═════════════════════════════════════════════════════════════════════════════
# BeaconPool — add / remove
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconPoolAddRemove:

    def test_add_invalid_hash_length(self):
        pool = BeaconPool()
        result = pool.add("abcd", connect=False)
        assert result is False
        assert pool.size() == 0

    def test_add_no_connect(self):
        pool = BeaconPool()
        result = pool.add(VALID_HASH_HEX, connect=False)
        assert result is True
        assert pool.size() == 1

    def test_add_duplicate_returns_true(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        result = pool.add(VALID_HASH_HEX, connect=False)
        assert result is True
        assert pool.size() == 1

    def test_add_with_connect_success(self):
        pool = BeaconPool()
        with patch.object(BeaconLink, "connect", return_value=True):
            result = pool.add(VALID_HASH_HEX, connect=True)
        assert result is True
        assert pool.size() == 1

    def test_add_with_connect_failure_removes(self):
        pool = BeaconPool()
        with patch.object(BeaconLink, "connect", return_value=False):
            result = pool.add(VALID_HASH_HEX, connect=True)
        assert result is False
        assert pool.size() == 0

    def test_add_normalises_hash(self):
        pool = BeaconPool()
        pool.add("  " + VALID_HASH_HEX.upper() + "  ", connect=False)
        assert pool.size() == 1
        links = pool.all_links()
        assert links[0].dest_hash_hex == VALID_HASH_HEX

    def test_remove_existing(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        pool.remove(VALID_HASH_HEX)
        assert pool.size() == 0

    def test_remove_nonexistent_is_noop(self):
        pool = BeaconPool()
        pool.remove(VALID_HASH_HEX)  # should not raise

    def test_remove_calls_teardown(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        bl = pool.all_links()[0]
        with patch.object(bl, "teardown") as mock_td:
            pool.remove(VALID_HASH_HEX)
            mock_td.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════════
# BeaconPool — queries
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconPoolQueries:

    def test_active_links_filters_correctly(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        pool.add(VALID_HASH_HEX_2, connect=False)
        links = pool.all_links()
        # Make one active
        _make_established_link(links[0])

        active = pool.active_links()
        assert len(active) == 1
        assert active[0].active is True

    def test_all_links_returns_all(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        pool.add(VALID_HASH_HEX_2, connect=False)
        assert len(pool.all_links()) == 2

    def test_size(self):
        pool = BeaconPool()
        assert pool.size() == 0
        pool.add(VALID_HASH_HEX, connect=False)
        assert pool.size() == 1

    def test_teardown_all(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        pool.add(VALID_HASH_HEX_2, connect=False)
        pool.teardown_all()
        assert pool.size() == 0

    def test_pending_count_starts_at_zero(self):
        pool = BeaconPool()
        assert pool.pending_count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# BeaconPool — dispatch (race / fallback)
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconPoolDispatch:

    def test_call_no_active_beacons(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)  # not active
        result = pool.call("getSlot")
        assert result is None

    def test_call_race_returns_first_response(self):
        pool = BeaconPool(strategy="race", request_timeout=2.0)
        pool.add(VALID_HASH_HEX, label="A", connect=False)
        pool.add(VALID_HASH_HEX_2, label="B", connect=False)
        links = pool.all_links()
        for bl in links:
            _make_established_link(bl)

        rpc_result = {"jsonrpc": "2.0", "id": 1, "result": 12345}

        def fake_request(payload, event, holder, timeout):
            holder[0] = rpc_result
            holder[1] = "A"
            event.set()

        for bl in links:
            bl.request = fake_request

        result = pool.call("getSlot")
        assert result == rpc_result

    def test_call_race_all_fail(self):
        pool = BeaconPool(strategy="race", request_timeout=0.3)
        pool.add(VALID_HASH_HEX, connect=False)
        bl = pool.all_links()[0]
        _make_established_link(bl)
        bl.request = lambda p, e, h, t: None  # never fires event

        result = pool.call("getSlot")
        assert result is None

    def test_call_fallback_tries_sequentially(self):
        pool = BeaconPool(strategy="fallback", request_timeout=0.3)
        pool.add(VALID_HASH_HEX, label="A", connect=False)
        pool.add(VALID_HASH_HEX_2, label="B", connect=False)
        links = pool.all_links()
        for bl in links:
            _make_established_link(bl)

        call_order = []
        rpc_result = {"jsonrpc": "2.0", "id": 1, "result": 99}

        def fail_request(payload, event, holder, timeout):
            call_order.append("A")
            # don't set event → simulates timeout

        def success_request(payload, event, holder, timeout):
            call_order.append("B")
            holder[0] = rpc_result
            holder[1] = "B"
            event.set()

        links[0].request = fail_request
        links[1].request = success_request

        result = pool.call("getSlot")
        assert result == rpc_result
        assert call_order == ["A", "B"]

    def test_call_fallback_all_fail(self):
        pool = BeaconPool(strategy="fallback", request_timeout=0.3)
        pool.add(VALID_HASH_HEX, connect=False)
        bl = pool.all_links()[0]
        _make_established_link(bl)
        bl.request = lambda p, e, h, t: None

        result = pool.call("getSlot")
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# BeaconPool — status_table
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconPoolStatus:

    def test_status_table_empty(self):
        pool = BeaconPool()
        table = pool.status_table()
        assert "(empty)" in table

    def test_status_table_shows_strategy(self):
        pool = BeaconPool(strategy="race")
        table = pool.status_table()
        assert "race" in table

    def test_status_table_shows_active_dot(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, label="test", connect=False)
        bl = pool.all_links()[0]
        _make_established_link(bl)
        table = pool.status_table()
        assert "●" in table  # green dot for active

    def test_status_table_shows_inactive_dot(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, label="test", connect=False)
        table = pool.status_table()
        assert "○" in table  # red dot for inactive


# ═════════════════════════════════════════════════════════════════════════════
# BeaconPool — add_from_announce
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconPoolAnnounce:

    def test_add_from_announce_new_beacon(self):
        pool = BeaconPool()
        ident = _make_mock_identity()
        with patch.object(BeaconLink, "connect_with_identity", return_value=True):
            pool.add_from_announce(VALID_HASH_BYTES, ident)
            # Give the background thread time to run
            time.sleep(0.2)
        assert pool.size() == 1

    def test_add_from_announce_existing_refreshes_identity(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        bl = pool.all_links()[0]
        new_ident = _make_mock_identity()
        with patch.object(bl, "refresh_identity") as mock_refresh:
            pool.add_from_announce(VALID_HASH_BYTES, new_ident)
            mock_refresh.assert_called_once_with(new_ident)
        assert pool.size() == 1  # no duplicate

    def test_add_from_announce_failed_connect_removes(self):
        pool = BeaconPool()
        ident = _make_mock_identity()
        with patch.object(BeaconLink, "connect_with_identity", return_value=False):
            pool.add_from_announce(VALID_HASH_BYTES, ident)
            time.sleep(0.2)
        assert pool.size() == 0


# ═════════════════════════════════════════════════════════════════════════════
# BeaconPool — add_background
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconPoolAddBackground:

    def test_add_background_invalid_hash(self):
        pool = BeaconPool()
        pool.add_background("short")
        assert pool.size() == 0

    def test_add_background_duplicate_skipped(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        pool.add_background(VALID_HASH_HEX)
        assert pool.size() == 1

    def test_add_background_success(self):
        pool = BeaconPool()
        with patch.object(BeaconLink, "connect", return_value=True):
            pool.add_background(VALID_HASH_HEX)
            time.sleep(0.2)
        assert pool.size() == 1
        assert pool.pending_count() == 0

    def test_add_background_failure_removes(self):
        pool = BeaconPool()
        with patch.object(BeaconLink, "connect", return_value=False):
            pool.add_background(VALID_HASH_HEX)
            time.sleep(0.2)
        assert pool.size() == 0
        assert pool.pending_count() == 0


# ═════════════════════════════════════════════════════════════════════════════
# BeaconAnnounceHandler
# ═════════════════════════════════════════════════════════════════════════════

class TestBeaconAnnounceHandler:

    def test_valid_announce_adds_to_pool(self):
        pool = BeaconPool()
        handler = BeaconAnnounceHandler(pool)
        ident = _make_mock_identity()
        with patch.object(pool, "add_from_announce") as mock_add:
            handler.received_announce(VALID_HASH_BYTES, ident, ANNOUNCE_DATA)
            mock_add.assert_called_once()

    def test_none_app_data_ignored(self):
        pool = BeaconPool()
        handler = BeaconAnnounceHandler(pool)
        with patch.object(pool, "add_from_announce") as mock_add:
            handler.received_announce(VALID_HASH_BYTES, _make_mock_identity(), None)
            mock_add.assert_not_called()

    def test_wrong_app_data_ignored(self):
        pool = BeaconPool()
        handler = BeaconAnnounceHandler(pool)
        with patch.object(pool, "add_from_announce") as mock_add:
            handler.received_announce(VALID_HASH_BYTES, _make_mock_identity(), b"wrong::data")
            mock_add.assert_not_called()

    def test_no_identity_ignored(self):
        pool = BeaconPool()
        handler = BeaconAnnounceHandler(pool)
        with patch.object(pool, "add_from_announce") as mock_add:
            handler.received_announce(VALID_HASH_BYTES, None, ANNOUNCE_DATA)
            mock_add.assert_not_called()

    def test_already_known_beacon_skipped(self):
        pool = BeaconPool()
        pool.add(VALID_HASH_HEX, connect=False)
        handler = BeaconAnnounceHandler(pool)
        with patch.object(pool, "add_from_announce") as mock_add:
            handler.received_announce(VALID_HASH_BYTES, _make_mock_identity(), ANNOUNCE_DATA)
            mock_add.assert_not_called()

    def test_aspect_filter_is_none(self):
        assert BeaconAnnounceHandler.aspect_filter is None


# ═════════════════════════════════════════════════════════════════════════════
# Startup helpers
# ═════════════════════════════════════════════════════════════════════════════

class TestStartupHelpers:

    def test_start_reticulum(self):
        _mock_RNS.Transport.identity = MagicMock()
        _mock_RNS.Identity.return_value = _make_mock_identity()
        mesh.start_reticulum("/fake/config")
        _mock_RNS.Reticulum.assert_called_with("/fake/config")
        assert _state.client_identity is not None

    def test_connect_all_parallel(self):
        _state.pool = BeaconPool()
        with patch.object(BeaconLink, "connect", return_value=True):
            count = mesh.connect_all_parallel([VALID_HASH_HEX, VALID_HASH_HEX_2])
        assert count == 2
        assert _state.pool.size() == 2

    def test_connect_all_parallel_partial_failure(self):
        _state.pool = BeaconPool()
        call_count = [0]

        def alternating_connect(self_bl, timeout=20.0):
            call_count[0] += 1
            return call_count[0] % 2 == 1  # first succeeds, second fails

        with patch.object(BeaconLink, "connect", alternating_connect):
            count = mesh.connect_all_parallel([VALID_HASH_HEX, VALID_HASH_HEX_2])
        assert count == 1
