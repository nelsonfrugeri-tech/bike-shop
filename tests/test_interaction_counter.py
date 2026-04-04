"""Tests for agent interaction counter: TTL, human reset, and GC."""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from bike_shop.slack.handler import (
    InteractionState,
    _agent_interactions,
    _check_and_update_interaction,
    _reset_interaction,
    _gc_interactions,
    MAX_AGENT_INTERACTIONS,
    AGENT_INTERACTION_TTL,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_interactions():
    """Ensure _agent_interactions is clean before and after each test."""
    _agent_interactions.clear()
    yield
    _agent_interactions.clear()


# ---------------------------------------------------------------------------
# InteractionState dataclass
# ---------------------------------------------------------------------------


def test_interaction_state_defaults():
    state = InteractionState()
    assert state.count == 0
    assert state.last_activity == 0.0


def test_interaction_state_custom_values():
    state = InteractionState(count=5, last_activity=100.0)
    assert state.count == 5
    assert state.last_activity == 100.0


# ---------------------------------------------------------------------------
# _check_and_update_interaction: happy path
# ---------------------------------------------------------------------------


def test_check_and_update_first_message_allowed():
    """First agent message in a thread is always allowed."""
    allowed = _check_and_update_interaction("thread-1")
    assert allowed is True
    assert _agent_interactions["thread-1"].count == 1


def test_check_and_update_increments_count():
    allowed_results = [_check_and_update_interaction("thread-2") for _ in range(5)]
    assert all(allowed_results)
    assert _agent_interactions["thread-2"].count == 5


def test_check_and_update_blocks_at_limit():
    """Message at exactly MAX_AGENT_INTERACTIONS should be blocked."""
    _agent_interactions["thread-3"] = InteractionState(
        count=MAX_AGENT_INTERACTIONS,
        last_activity=time.monotonic(),
    )
    allowed = _check_and_update_interaction("thread-3")
    assert allowed is False
    # Count must NOT be incremented when blocked
    assert _agent_interactions["thread-3"].count == MAX_AGENT_INTERACTIONS


def test_check_and_update_updates_last_activity():
    before = time.monotonic()
    _check_and_update_interaction("thread-4")
    after = time.monotonic()
    ts = _agent_interactions["thread-4"].last_activity
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# TTL: expired counter is reset
# ---------------------------------------------------------------------------


def test_ttl_expired_counter_resets():
    """Counter past TTL should be reset; the arriving message is allowed."""
    # Simulate an old interaction entry with count at limit
    old_ts = time.monotonic() - (AGENT_INTERACTION_TTL + 1)
    _agent_interactions["thread-ttl"] = InteractionState(
        count=MAX_AGENT_INTERACTIONS,
        last_activity=old_ts,
    )
    allowed = _check_and_update_interaction("thread-ttl")
    assert allowed is True
    assert _agent_interactions["thread-ttl"].count == 1


def test_ttl_not_expired_keeps_count():
    """Counter within TTL should NOT be reset."""
    _agent_interactions["thread-live"] = InteractionState(
        count=MAX_AGENT_INTERACTIONS,
        last_activity=time.monotonic(),
    )
    allowed = _check_and_update_interaction("thread-live")
    assert allowed is False


# ---------------------------------------------------------------------------
# Human reset
# ---------------------------------------------------------------------------


def test_reset_interaction_removes_entry():
    _agent_interactions["thread-human"] = InteractionState(count=10, last_activity=1.0)
    _reset_interaction("thread-human")
    assert "thread-human" not in _agent_interactions


def test_reset_interaction_missing_key_is_noop():
    """pop on a missing key must not raise."""
    _reset_interaction("no-such-thread")  # should not raise


def test_reset_allows_new_messages_after_human():
    _agent_interactions["thread-reset"] = InteractionState(
        count=MAX_AGENT_INTERACTIONS,
        last_activity=time.monotonic(),
    )
    _reset_interaction("thread-reset")
    allowed = _check_and_update_interaction("thread-reset")
    assert allowed is True
    assert _agent_interactions["thread-reset"].count == 1


# ---------------------------------------------------------------------------
# Lazy garbage collection
# ---------------------------------------------------------------------------


def test_gc_removes_stale_entries_beyond_100():
    """GC removes entries older than 2*TTL when dict exceeds 100 entries."""
    stale_ts = time.monotonic() - (2 * AGENT_INTERACTION_TTL + 1)
    fresh_ts = time.monotonic()

    # 101 entries: 50 stale + 51 fresh
    for i in range(50):
        _agent_interactions[f"stale-{i}"] = InteractionState(count=1, last_activity=stale_ts)
    for i in range(51):
        _agent_interactions[f"fresh-{i}"] = InteractionState(count=1, last_activity=fresh_ts)

    assert len(_agent_interactions) == 101

    _gc_interactions()

    # All stale entries removed; fresh entries kept
    remaining_keys = set(_agent_interactions.keys())
    for i in range(50):
        assert f"stale-{i}" not in remaining_keys
    for i in range(51):
        assert f"fresh-{i}" in remaining_keys


def test_gc_does_not_run_under_100_entries():
    """GC must be a no-op when dict has <= 100 entries."""
    stale_ts = time.monotonic() - (2 * AGENT_INTERACTION_TTL + 1)
    for i in range(99):
        _agent_interactions[f"stale-{i}"] = InteractionState(count=1, last_activity=stale_ts)

    _gc_interactions()

    # Nothing removed — threshold not reached
    assert len(_agent_interactions) == 99


def test_gc_triggered_automatically_on_new_interaction():
    """_check_and_update_interaction triggers GC when > 100 entries exist."""
    stale_ts = time.monotonic() - (2 * AGENT_INTERACTION_TTL + 1)
    for i in range(101):
        _agent_interactions[f"stale-{i}"] = InteractionState(count=1, last_activity=stale_ts)

    # Adding one more entry via the public function should trigger GC
    _check_and_update_interaction("trigger-gc")

    # Stale entries should have been GC'd
    for i in range(101):
        assert f"stale-{i}" not in _agent_interactions


# ---------------------------------------------------------------------------
# Parametrize: boundary conditions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("count,expected_allowed", [
    (0, True),
    (MAX_AGENT_INTERACTIONS - 1, True),
    (MAX_AGENT_INTERACTIONS, False),
    (MAX_AGENT_INTERACTIONS + 5, False),
])
def test_boundary_counts(count: int, expected_allowed: bool):
    _agent_interactions["thread-boundary"] = InteractionState(
        count=count,
        last_activity=time.monotonic(),
    )
    allowed = _check_and_update_interaction("thread-boundary")
    assert allowed is expected_allowed
