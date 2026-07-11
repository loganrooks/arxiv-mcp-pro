"""Tests for cross-process arXiv rate pacing (B17).

Mock-based only — no live arXiv calls. Must pass on Windows CI too (the
cross-process gate falls back to msvcrt there; these tests exercise the shared
mtime/interval logic, not the OS lock primitive itself).
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from arxiv_mcp_server.tools import arxiv_pacing
from arxiv_mcp_server.tools.search import _rate_limited_get
from arxiv_mcp_server.tools import handle_search


@pytest.fixture
def paced(tmp_path, monkeypatch):
    """Point the pacer at a tmp-backed storage dir with a fresh in-process clock.

    Interval is left for each test to set (the autouse suite fixture pins it to
    0). Overriding STORAGE_PATH on the Settings class keeps the real storage dir
    untouched.
    """
    monkeypatch.setattr(type(arxiv_pacing.settings), "STORAGE_PATH", tmp_path)
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", 0.0)
    return tmp_path


def _set_interval(monkeypatch, value):
    monkeypatch.setattr(arxiv_pacing.settings, "ARXIV_MIN_REQUEST_INTERVAL", value)


# ---------------------------------------------------------------------------
# pace_arxiv_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interval_zero_returns_immediately_no_file(paced, monkeypatch):
    """interval=0 disables pacing entirely: no wait, no lock file created."""
    _set_interval(monkeypatch, 0.0)

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed < 0.1
    assert not (paced / "arxiv_api.lock").exists()


@pytest.mark.asyncio
async def test_second_sequential_call_is_delayed(paced, monkeypatch):
    """Two sequential paces with a small interval: the second is delayed."""
    _set_interval(monkeypatch, 0.3)

    await arxiv_pacing.pace_arxiv_request()  # first call primes the clock

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.25, f"second call not paced (elapsed={elapsed:.3f}s)"
    assert (paced / "arxiv_api.lock").exists()


@pytest.mark.asyncio
async def test_recent_mtime_forces_wait_and_bumps(paced, monkeypatch):
    """A lock file whose mtime is 'now' forces a wait; mtime is bumped after."""
    _set_interval(monkeypatch, 0.3)

    lock_file = paced / "arxiv_api.lock"
    lock_file.touch()
    now = time.time()
    os.utime(str(lock_file), (now, now))
    pre_mtime = os.stat(str(lock_file)).st_mtime

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.25, f"pace did not wait (elapsed={elapsed:.3f}s)"
    assert os.stat(str(lock_file)).st_mtime > pre_mtime


@pytest.mark.asyncio
async def test_record_arxiv_request_bumps_clock(paced, monkeypatch):
    """record_arxiv_request creates/bumps the lock file when pacing is enabled."""
    _set_interval(monkeypatch, 3.0)
    lock_file = paced / "arxiv_api.lock"
    assert not lock_file.exists()

    arxiv_pacing.record_arxiv_request()
    assert lock_file.exists()
    first = os.stat(str(lock_file)).st_mtime

    time.sleep(0.05)
    arxiv_pacing.record_arxiv_request()
    assert os.stat(str(lock_file)).st_mtime >= first


@pytest.mark.asyncio
async def test_record_arxiv_request_no_file_when_disabled(paced, monkeypatch):
    """With pacing disabled, record leaves no cross-process file behind."""
    _set_interval(monkeypatch, 0.0)
    arxiv_pacing.record_arxiv_request()
    assert not (paced / "arxiv_api.lock").exists()


# ---------------------------------------------------------------------------
# Retry-After handling in _rate_limited_get
# ---------------------------------------------------------------------------


def _resp(status_code, headers=None):
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    r.raise_for_status = MagicMock()
    r.text = ""
    return r


@pytest.mark.asyncio
async def test_retry_after_short_retries_once_and_succeeds(monkeypatch):
    """429 with a short Retry-After is slept out, then retried once → 200."""
    client = MagicMock()
    client.get = AsyncMock(side_effect=[_resp(429, {"Retry-After": "1"}), _resp(200)])
    # Keep the test fast: don't actually sleep the header value.
    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr("arxiv_mcp_server.tools.search.asyncio.sleep", _fake_sleep)

    response = await _rate_limited_get(client, "https://example.test/q")

    assert response.status_code == 200
    assert client.get.await_count == 2
    assert 1.0 in slept


@pytest.mark.asyncio
async def test_retry_after_long_fails_fast_with_value(monkeypatch):
    """429 with a long Retry-After fails fast, message names the delay."""
    client = MagicMock()
    client.get = AsyncMock(return_value=_resp(429, {"Retry-After": "300"}))

    with pytest.raises(RuntimeError) as excinfo:
        await _rate_limited_get(client, "https://example.test/q")

    assert "300" in str(excinfo.value)
    assert client.get.await_count == 1  # no retry


@pytest.mark.asyncio
async def test_no_retry_after_header_fails_fast_with_cooldown_note(monkeypatch):
    """429 with no Retry-After fails fast with the observed-cooldowns wording."""
    client = MagicMock()
    client.get = AsyncMock(return_value=_resp(429, {}))

    with pytest.raises(RuntimeError) as excinfo:
        await _rate_limited_get(client, "https://example.test/q")

    msg = str(excinfo.value)
    assert "observed cooldowns" in msg
    assert client.get.await_count == 1


@pytest.mark.asyncio
async def test_retry_after_still_limited_falls_through(monkeypatch):
    """A 429 that stays 429 after the single retry surfaces a fail-fast error."""
    client = MagicMock()
    client.get = AsyncMock(
        side_effect=[_resp(429, {"Retry-After": "1"}), _resp(429, {})]
    )

    async def _fake_sleep(secs):
        pass

    monkeypatch.setattr("arxiv_mcp_server.tools.search.asyncio.sleep", _fake_sleep)

    with pytest.raises(RuntimeError) as excinfo:
        await _rate_limited_get(client, "https://example.test/q")

    assert "rate limiting this IP" in str(excinfo.value)
    assert client.get.await_count == 2


# ---------------------------------------------------------------------------
# handle_search invokes the pacer exactly once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_search_paces_once(mock_client, monkeypatch):
    """search_papers (client path) calls pace_arxiv_request exactly once."""
    calls = []

    async def _recording_pace():
        calls.append(1)

    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search.pace_arxiv_request", _recording_pace
    )
    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search.get_arxiv_client",
        lambda *a, **k: mock_client,
    )

    result = await handle_search({"query": "test", "max_results": 1})

    assert len(calls) == 1
    assert result  # sanity: a result was produced


# ---------------------------------------------------------------------------
# _parse_retry_after unit coverage
# ---------------------------------------------------------------------------


def test_parse_retry_after_forms():
    from arxiv_mcp_server.tools.search import _parse_retry_after

    assert _parse_retry_after(None) is None
    assert _parse_retry_after("") is None
    assert _parse_retry_after("garbage") is None
    assert _parse_retry_after("5") == 5.0
    # HTTP-date in the past clamps to 0.
    assert _parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0
