"""Tests for cross-process arXiv rate pacing (B17).

Mock-based only — no live arXiv calls. Must pass on Windows CI too (the
cross-process gate falls back to msvcrt there; these tests exercise the shared
mtime/interval logic, not the OS lock primitive itself).

KNOWN COVERAGE GAP (MINOR-6): these tests run in a single process and so cannot
catch a lock acquired *non-exclusively* — e.g. a LOCK_SH-for-LOCK_EX typo in
_acquire_file_lock — because true cross-process contention never occurs here. A
real contention test (spawn a subprocess that holds the lock while the parent
paces, asserting the parent blocks) was considered and deliberately rejected: it
adds process-spawn + timing flakiness to CI for a low-probability regression that
code review already guards. The exclusivity of the OS primitive is trusted, not
asserted.
"""

import os
import time
from unittest.mock import AsyncMock, MagicMock

import httpx
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

    # Generous ceiling — the point is "no interval-sized wait", not sub-100ms
    # scheduling precision on a loaded CI runner.
    assert elapsed < 0.5
    assert not (paced / "arxiv_api.lock").exists()


@pytest.mark.asyncio
async def test_second_sequential_call_is_delayed(paced, monkeypatch):
    """Two sequential paces with a small interval: the second is delayed by the
    cross-process gate (the lock-file mtime the first call left behind)."""
    _set_interval(monkeypatch, 0.3)

    await arxiv_pacing.pace_arxiv_request()  # first call primes the lock-file mtime

    # Reset the in-process clock so the second call's delay comes purely from the
    # cross-process lock-file mtime, not the in-process monotonic gate — this is
    # what isolates the cross-process wait path.
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", 0.0)

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.25, f"second call not paced (elapsed={elapsed:.3f}s)"
    assert (paced / "arxiv_api.lock").exists()


@pytest.mark.asyncio
async def test_pace_waits_and_bumps_lockfile_mtime(paced, monkeypatch):
    """A paced request waits, then bumps the lock file's mtime.

    The lock file's mtime is set ~10s in the past so the ``st_mtime > pre_mtime``
    bump assertion has generous headroom on coarse-granularity filesystems (a
    fresh 'now' mtime would leave only ~one interval of margin). A 10s-old mtime
    imposes no cross-process wait, so the wait here is driven by the in-process
    gate (the cross-process wait itself is covered by
    ``test_second_sequential_call_is_delayed``).
    """
    _set_interval(monkeypatch, 0.3)

    lock_file = paced / "arxiv_api.lock"
    lock_file.touch()
    past = time.time() - 10
    os.utime(str(lock_file), (past, past))
    pre_mtime = os.stat(str(lock_file)).st_mtime

    # Prime the in-process clock so the in-process gate produces the wait.
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", time.monotonic())

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.25, f"pace did not wait (elapsed={elapsed:.3f}s)"
    assert os.stat(str(lock_file)).st_mtime > pre_mtime


@pytest.mark.asyncio
async def test_future_mtime_wait_is_clamped_to_one_interval(paced, monkeypatch):
    """A future lock-file mtime (clock skew / NTP step-back) can never demand
    more than one interval of wait — without the clamp it would sleep Δ+interval
    while holding both locks (MAJOR-1)."""
    _set_interval(monkeypatch, 0.3)

    lock_file = paced / "arxiv_api.lock"
    lock_file.touch()
    future = time.time() + 100  # 100s ahead — the skew a naive wait would sleep
    os.utime(str(lock_file), (future, future))
    # in-process clock stays 0 (paced fixture) so only the cross-process gate acts

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"future mtime not clamped (elapsed={elapsed:.3f}s)"


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
# pace_arxiv_request_sync (B20) — blocking counterpart for worker-thread callers
#
# The sync pacer shares the SAME in-process clock (_last_request_time), lock
# file, and cooldown channel as the async pacer; these mirror the async tests
# above using the sync entrypoint. Plain sync tests where possible (the sync
# pacer blocks the calling thread); the interleaving test is async because it
# also awaits pace_arxiv_request. _quiesce_interval_gate is defined below in the
# cooldown section — module-level names resolve at call time, so forward use is
# fine.
# ---------------------------------------------------------------------------


def test_sync_interval_zero_returns_immediately_no_file(paced, monkeypatch):
    """interval=0 disables the sync pacer entirely: no wait, no lock file."""
    _set_interval(monkeypatch, 0.0)

    start = time.monotonic()
    arxiv_pacing.pace_arxiv_request_sync()
    elapsed = time.monotonic() - start

    # Generous ceiling — the point is "no interval-sized wait", not sub-100ms
    # precision on a loaded CI runner.
    assert elapsed < 0.5
    assert not (paced / "arxiv_api.lock").exists()


def test_sync_second_sequential_call_is_delayed(paced, monkeypatch):
    """Two sequential sync paces with a small interval: the second is delayed by
    the cross-process gate (the lock-file mtime the first call left behind).
    Mirrors test_second_sequential_call_is_delayed for the sync entrypoint."""
    _set_interval(monkeypatch, 0.3)

    arxiv_pacing.pace_arxiv_request_sync()  # first call primes the lock-file mtime

    # Reset the in-process clock so the second call's delay comes purely from the
    # cross-process lock-file mtime, not the in-process monotonic gate.
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", 0.0)

    start = time.monotonic()
    arxiv_pacing.pace_arxiv_request_sync()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.25, f"second sync call not paced (elapsed={elapsed:.3f}s)"
    assert (paced / "arxiv_api.lock").exists()


def test_sync_record_cooldown_makes_pace_wait(paced, monkeypatch):
    """record_arxiv_cooldown writes the cooldown file; a following sync pace with
    a small interval sleeps until the cooldown passes. Mirrors
    test_record_cooldown_makes_pace_wait for the sync entrypoint."""
    _set_interval(monkeypatch, 0.3)
    _quiesce_interval_gate(paced)

    arxiv_pacing.record_arxiv_cooldown(0.4)
    assert (paced / "arxiv_api.cooldown").exists()

    start = time.monotonic()
    arxiv_pacing.pace_arxiv_request_sync()
    elapsed = time.monotonic() - start

    assert (
        elapsed >= 0.35
    ), f"cooldown not honored by sync pacer (elapsed={elapsed:.3f}s)"


@pytest.mark.asyncio
async def test_sync_then_async_pace_off_one_clock(paced, monkeypatch):
    """The sync and async lanes pace off ONE clock: after pace_arxiv_request_sync
    advances the shared _last_request_time and bumps the lock-file mtime, an
    immediately following ``await pace_arxiv_request`` is delayed by that shared
    state. The interval gate is quiesced (old lock mtime) so the sync call itself
    does not wait and the async delay is attributable to the shared clock."""
    _set_interval(monkeypatch, 0.3)
    _quiesce_interval_gate(paced)

    lock_file = paced / "arxiv_api.lock"
    pre_mtime = os.stat(str(lock_file)).st_mtime

    arxiv_pacing.pace_arxiv_request_sync()

    # The sync lane advanced BOTH shared channels: the in-process monotonic clock
    # and the cross-process lock-file mtime.
    assert arxiv_pacing._last_request_time > 0.0
    assert os.stat(str(lock_file)).st_mtime > pre_mtime

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert (
        elapsed >= 0.25
    ), f"async pace not delayed by the sync lane's clock (elapsed={elapsed:.3f}s)"


@pytest.mark.asyncio
async def test_sync_then_async_share_inprocess_clock_even_without_flock(
    paced, monkeypatch
):
    """MAJOR-1: the sync and async lanes share the SAME in-process clock even when
    the cross-process file lock is fully failed-open. With _cross_process_gate
    stubbed to a no-op the lock-file mtime channel is disabled, so the async
    delay can ONLY come from the shared _last_request_time the sync lane set —
    which pins that both lanes route their tail through the one in-process gate
    (test_sync_then_async_pace_off_one_clock alone could pass via the mtime)."""
    _set_interval(monkeypatch, 0.3)
    _quiesce_interval_gate(paced)

    arxiv_pacing.pace_arxiv_request_sync()

    # Disable the cross-process (lock-file mtime) channel entirely: now the only
    # remaining shared state is the in-process _last_request_time clock.
    monkeypatch.setattr(arxiv_pacing, "_cross_process_gate", lambda interval: None)

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert (
        elapsed >= 0.25
    ), f"in-process clock not shared across lanes in fail-open (elapsed={elapsed:.3f}s)"


# ---------------------------------------------------------------------------
# Shared cooldown channel (C-1)
# ---------------------------------------------------------------------------


def _quiesce_interval_gate(paced):
    """Pre-create the interval lock file with a 10s-old mtime so the interval
    gate imposes no wait — isolating the cooldown as the sole source of delay."""
    lock_file = paced / "arxiv_api.lock"
    lock_file.touch()
    old = time.time() - 10
    os.utime(str(lock_file), (old, old))


@pytest.mark.asyncio
async def test_record_cooldown_makes_pace_wait(paced, monkeypatch):
    """record_arxiv_cooldown writes the cooldown file; a following pace with a
    small interval sleeps until the cooldown passes."""
    _set_interval(monkeypatch, 0.3)
    _quiesce_interval_gate(paced)

    arxiv_pacing.record_arxiv_cooldown(0.4)
    assert (paced / "arxiv_api.cooldown").exists()

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.35, f"cooldown not honored (elapsed={elapsed:.3f}s)"


@pytest.mark.asyncio
async def test_cooldown_honored_from_file_channel(paced, monkeypatch):
    """Cross-channel: fresh in-process state + a cooldown FILE whose content
    carries a future not_before → pace waits (the file channel alone drives it)."""
    _set_interval(monkeypatch, 0.3)
    monkeypatch.setattr(arxiv_pacing, "_not_before", 0.0)
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", 0.0)
    _quiesce_interval_gate(paced)
    (paced / "arxiv_api.cooldown").write_text(repr(time.time() + 0.4))

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()
    elapsed = time.monotonic() - start

    assert elapsed >= 0.35, f"file cooldown not honored (elapsed={elapsed:.3f}s)"


@pytest.mark.asyncio
async def test_cooldown_is_capped(paced, monkeypatch):
    """An absurd cooldown is capped at 120s in both channels."""
    _set_interval(monkeypatch, 3.0)
    before = time.time()

    arxiv_pacing.record_arxiv_cooldown(10_000)

    file_not_before = float((paced / "arxiv_api.cooldown").read_text())
    assert file_not_before <= before + 121
    assert arxiv_pacing._not_before <= time.monotonic() + 121


@pytest.mark.asyncio
async def test_garbage_cooldown_content_is_ignored(paced, monkeypatch):
    """Unparseable cooldown content → no cooldown, no exception (fail-open)."""
    _set_interval(monkeypatch, 0.3)
    monkeypatch.setattr(arxiv_pacing, "_not_before", 0.0)
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", 0.0)
    _quiesce_interval_gate(paced)
    # Non-UTF-8 bytes, not just a non-float string: the reader must fail open
    # on decode errors too (and writing bytes avoids Windows' cp1252 default
    # encoding choking on exotic characters in the test itself).
    (paced / "arxiv_api.cooldown").write_bytes(b"not-a-float \xff\xfe\x99")

    start = time.monotonic()
    await arxiv_pacing.pace_arxiv_request()  # must not raise
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, f"garbage content caused a wait (elapsed={elapsed:.3f}s)"


@pytest.mark.asyncio
async def test_record_cooldown_no_file_when_disabled(paced, monkeypatch):
    """With pacing disabled, record_arxiv_cooldown writes no cross-process file."""
    _set_interval(monkeypatch, 0.0)
    arxiv_pacing.record_arxiv_cooldown(30.0)
    assert not (paced / "arxiv_api.cooldown").exists()


@pytest.mark.asyncio
async def test_cooldown_published_while_queued_is_honored(paced, monkeypatch):
    """FIX-B: a cooldown published while a caller is queued behind the interval
    lock is caught by the in-lock re-check (the pre-lock snapshot would miss it).

    Deterministic: _cooldown_remaining is scripted so the pre-lock check sees
    nothing, the first in-lock re-check sees a fresh 0.4s cooldown, then clears.
    """
    _set_interval(monkeypatch, 0.3)
    _quiesce_interval_gate(paced)  # old lock mtime → the interval gate adds no wait
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", 0.0)

    seq = iter([0.0, 0.4, 0.0])

    async def _scripted_remaining():
        return next(seq, 0.0)

    monkeypatch.setattr(arxiv_pacing, "_cooldown_remaining", _scripted_remaining)

    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(arxiv_pacing.asyncio, "sleep", _fake_sleep)

    await arxiv_pacing.pace_arxiv_request()

    # 0.4 came from the in-lock re-check, not the (0.0) pre-lock snapshot.
    assert 0.4 in slept


@pytest.mark.asyncio
async def test_cooldown_never_shortens_deadline(paced, monkeypatch):
    """FIX-C: a later, smaller cooldown must not shrink an earlier larger one,
    in either channel (file + in-process)."""
    _set_interval(monkeypatch, 3.0)

    arxiv_pacing.record_arxiv_cooldown(120.0)
    long_file = float((paced / "arxiv_api.cooldown").read_text())
    long_inproc = arxiv_pacing._not_before

    arxiv_pacing.record_arxiv_cooldown(60.0)  # smaller, later — must not win
    assert float((paced / "arxiv_api.cooldown").read_text()) == pytest.approx(long_file)
    assert arxiv_pacing._not_before == pytest.approx(long_inproc)


@pytest.mark.asyncio
async def test_cooldown_write_converges_on_concurrent_shorter(paced, monkeypatch):
    """FIX-1: if a competing shorter publish lands right after our os.replace, the
    verify-and-retry converges the file back to the longer deadline."""
    cooldown_file = paced / "arxiv_api.cooldown"
    long_deadline = time.time() + 120.0
    short_deadline = time.time() + 5.0

    real_replace = arxiv_pacing.os.replace
    injected = {"done": False}

    def _replace_then_clobber(src, dst):
        real_replace(src, dst)
        if not injected["done"]:
            injected["done"] = True  # simulate a competing shorter writer landing
            cooldown_file.write_text(repr(short_deadline))

    monkeypatch.setattr(arxiv_pacing.os, "replace", _replace_then_clobber)

    arxiv_pacing._write_cooldown_file(long_deadline)

    # Converged: the file keeps the longer deadline despite the mid-write clobber.
    assert float(cooldown_file.read_text()) == pytest.approx(long_deadline)


@pytest.mark.asyncio
async def test_inlock_cooldown_wait_is_bounded_by_cap(paced, monkeypatch):
    """FIX-4: a persistently far-future cooldown (corrupt file / clock step) is
    bounded to ~one cap of cumulative in-lock wait, not cap × iterations."""
    _set_interval(monkeypatch, 0.3)
    monkeypatch.setattr(arxiv_pacing, "_COOLDOWN_CAP", 0.5)
    _quiesce_interval_gate(paced)  # old lock mtime → the interval gate adds no wait
    monkeypatch.setattr(arxiv_pacing, "_last_request_time", 0.0)
    # A far-future in-process cooldown that never clears.
    monkeypatch.setattr(arxiv_pacing, "_not_before", time.monotonic() + 10_000)

    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(arxiv_pacing.asyncio, "sleep", _fake_sleep)

    await arxiv_pacing.pace_arxiv_request()

    # Pre-lock (<= one cap) + in-lock (bounded to one cap by FIX-4) <= 2 caps;
    # without the bound the in-lock loop alone would sleep ~10 caps.
    assert sum(slept) <= 2 * 0.5 + 0.05, f"unbounded cooldown wait: {slept}"


def test_lock_acquire_timeout_scales_with_interval():
    """FIX-D: the effective lock-acquisition deadline is max(60, 2*interval)."""
    assert arxiv_pacing._lock_acquire_timeout(3.0) == 60.0  # floor dominates
    assert arxiv_pacing._lock_acquire_timeout(30.0) == 60.0  # 2*30 == floor
    assert arxiv_pacing._lock_acquire_timeout(90.0) == 180.0  # 2*interval wins


# ---------------------------------------------------------------------------
# Cross-process gate recheck loop (C-2)
#
# A true cross-process contention race cannot be reproduced single-process (see
# the KNOWN COVERAGE GAP note above), so the recheck LOGIC is exercised directly:
# a monkeypatched time.sleep bumps the lock file's mtime mid-sleep, standing in
# for a concurrent lock-free record_arxiv_request.
# ---------------------------------------------------------------------------


def test_cross_process_gate_rechecks_after_mtime_bump(paced, monkeypatch):
    """A lock-free mtime bump during the in-lock sleep is re-detected: the gate
    re-sleeps rather than waking on the stale schedule."""
    interval = 0.3
    _set_interval(monkeypatch, interval)
    lock_file = paced / "arxiv_api.lock"
    lock_file.touch()
    now = time.time()
    os.utime(str(lock_file), (now, now))  # fresh → initial wait > 0

    calls = []

    def fake_sleep(secs):
        calls.append(secs)
        if len(calls) == 1:
            # A concurrent request just recorded itself: bump the mtime forward,
            # so the naive (single-shot) path would under-pace. The recheck must
            # notice and sleep again.
            t = time.time()
            os.utime(str(lock_file), (t, t))
        elif len(calls) == 2:
            # Now let the clock "advance" past the interval so the loop converges.
            past = time.time() - 100
            os.utime(str(lock_file), (past, past))

    monkeypatch.setattr(arxiv_pacing.time, "sleep", fake_sleep)

    arxiv_pacing._cross_process_gate(interval)

    # Initial wait + exactly one recheck after the mid-sleep bump.
    assert len(calls) == 2


def test_cross_process_gate_recheck_is_capped(paced, monkeypatch):
    """Liveness: a pathological continuous mtime bump cannot spin the recheck
    loop forever — it is capped at 10 iterations."""
    interval = 0.3
    _set_interval(monkeypatch, interval)
    lock_file = paced / "arxiv_api.lock"
    lock_file.touch()
    now = time.time()
    os.utime(str(lock_file), (now, now))

    calls = []

    def fake_sleep(secs):
        calls.append(secs)
        t = time.time()
        os.utime(str(lock_file), (t, t))  # always fresh → never converges

    monkeypatch.setattr(arxiv_pacing.time, "sleep", fake_sleep)

    arxiv_pacing._cross_process_gate(interval)

    assert len(calls) == 10  # capped, does not hang


def test_cross_process_gate_verifies_before_break(paced, monkeypatch):
    """FIX-3: a lock-free mtime bump between the passing age-check and the utime
    is caught by the verification re-stat — one more wait iteration occurs
    instead of proceeding on the stale check.

    Deterministic: os.fstat is scripted so the first passing check is followed by
    a verify that sees a *moved* (fresh) mtime, forcing the loop to re-evaluate
    and wait one interval that a naive break would have skipped.
    """
    interval = 0.3
    _set_interval(monkeypatch, interval)
    lock_file = paced / "arxiv_api.lock"
    lock_file.touch()

    now = time.time()
    old = now - 10  # age >= interval → passes the age check
    fresh = now  # a just-recorded request (age ~0 → must wait)

    class _Stat:
        def __init__(self, m):
            self.st_mtime = m

    # check→old (pass), verify→fresh (moved!→continue), check→fresh (age~0→sleep),
    # check→old (converged), verify→old (stable→break).
    seq = iter([old, fresh, fresh, old, old])
    monkeypatch.setattr(arxiv_pacing.os, "fstat", lambda fd: _Stat(next(seq, old)))

    calls = []
    monkeypatch.setattr(arxiv_pacing.time, "sleep", lambda s: calls.append(s))

    arxiv_pacing._cross_process_gate(interval)

    # A naive break (no verification) would proceed with zero sleeps.
    assert len(calls) >= 1


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


def _count_pace_calls(monkeypatch):
    """Replace search's pacer with a counter and neutralise the recorders so a
    _rate_limited_get test never touches the real storage dir."""
    pace_calls = []

    async def _counting_pace():
        pace_calls.append(1)

    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search.pace_arxiv_request", _counting_pace
    )
    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search.record_arxiv_request", MagicMock()
    )
    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search.record_arxiv_cooldown", MagicMock()
    )
    return pace_calls


@pytest.mark.asyncio
async def test_timeout_retry_re_paces(monkeypatch):
    """FIX-A: the timeout retry re-enters the pacer — pace is called once per
    loop attempt — so a raised interval isn't undercut by the fixed 5s backoff."""
    pace_calls = _count_pace_calls(monkeypatch)

    async def _fake_sleep(secs):
        pass

    monkeypatch.setattr("arxiv_mcp_server.tools.search.asyncio.sleep", _fake_sleep)

    client = MagicMock()
    client.get = AsyncMock(side_effect=[httpx.TimeoutException("boom"), _resp(200)])

    response = await _rate_limited_get(client, "https://example.test/q")

    assert response.status_code == 200
    assert len(pace_calls) == 2  # initial attempt + timeout retry


@pytest.mark.asyncio
async def test_429_retry_re_paces(monkeypatch):
    """FIX-A: the 429-retry GET is re-paced (not fired straight after the
    Retry-After sleep), so callers that slept the same header don't stampede."""
    pace_calls = _count_pace_calls(monkeypatch)

    async def _fake_sleep(secs):
        pass

    monkeypatch.setattr("arxiv_mcp_server.tools.search.asyncio.sleep", _fake_sleep)

    client = MagicMock()
    client.get = AsyncMock(side_effect=[_resp(429, {"Retry-After": "1"}), _resp(200)])

    response = await _rate_limited_get(client, "https://example.test/q")

    assert response.status_code == 200
    assert len(pace_calls) == 2  # loop-top pace + re-pace before the retry GET


@pytest.mark.asyncio
async def test_retry_after_zero_still_paces_retry(monkeypatch):
    """Retry-After 0 (also negative / past HTTP-date → 0.0) no longer sleeps a
    floored interval — the floor is gone — but the retry is still paced by the
    re-pace before the retry GET, so it cannot fire an immediate unpaced request
    (replaces the MAJOR-3 sleep-floor)."""
    pace_calls = _count_pace_calls(monkeypatch)
    slept = []

    async def _fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr("arxiv_mcp_server.tools.search.asyncio.sleep", _fake_sleep)

    client = MagicMock()
    client.get = AsyncMock(side_effect=[_resp(429, {"Retry-After": "0"}), _resp(200)])

    response = await _rate_limited_get(client, "https://example.test/q")

    assert response.status_code == 200
    assert client.get.await_count == 2
    assert len(pace_calls) == 2  # retry is paced, not fired immediately
    assert slept == [0.0]  # sleep is the raw Retry-After (no interval floor)


@pytest.mark.asyncio
async def test_429_retry_timeout_does_not_triple_request(monkeypatch):
    """FIX-2: a TimeoutException on the 429-retry GET fails fast with a clear
    error and does NOT re-enter the outer loop for a third request."""
    _count_pace_calls(monkeypatch)  # neutralise pacer/recorders (no real storage)

    async def _fake_sleep(secs):
        pass

    monkeypatch.setattr("arxiv_mcp_server.tools.search.asyncio.sleep", _fake_sleep)

    client = MagicMock()
    client.get = AsyncMock(
        side_effect=[_resp(429, {"Retry-After": "1"}), httpx.TimeoutException("boom")]
    )

    with pytest.raises(RuntimeError) as excinfo:
        await _rate_limited_get(client, "https://example.test/q")

    assert "timed out" in str(excinfo.value)
    assert client.get.await_count == 2  # initial + one retry, never a third


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
# 429/503 → shared cooldown wiring (C-1)
# ---------------------------------------------------------------------------


def _capture_cooldowns(monkeypatch):
    """Neutralise the pacer/recorders (no real storage) and capture every
    record_arxiv_cooldown value the handler publishes."""
    cooldowns = []
    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search.record_arxiv_cooldown",
        lambda seconds: cooldowns.append(seconds),
    )
    monkeypatch.setattr("arxiv_mcp_server.tools.search.pace_arxiv_request", AsyncMock())
    monkeypatch.setattr(
        "arxiv_mcp_server.tools.search.record_arxiv_request", MagicMock()
    )
    return cooldowns


@pytest.mark.asyncio
async def test_429_honored_retry_publishes_cooldown(monkeypatch):
    """The honored-retry path publishes the server's Retry-After as the cooldown,
    before sleeping (the interval is enforced separately by the re-pace)."""
    monkeypatch.setattr(arxiv_pacing.settings, "ARXIV_MIN_REQUEST_INTERVAL", 0.3)
    cooldowns = _capture_cooldowns(monkeypatch)

    async def _fake_sleep(secs):
        pass

    monkeypatch.setattr("arxiv_mcp_server.tools.search.asyncio.sleep", _fake_sleep)

    client = MagicMock()
    client.get = AsyncMock(side_effect=[_resp(429, {"Retry-After": "1"}), _resp(200)])

    response = await _rate_limited_get(client, "https://example.test/q")

    assert response.status_code == 200
    # cooldown == raw retry_after (1.0), published before the sleep.
    assert cooldowns == [1.0]


@pytest.mark.asyncio
async def test_429_fail_fast_publishes_retry_after_cooldown(monkeypatch):
    """A long Retry-After fail-fast publishes the parsed value as the cooldown."""
    cooldowns = _capture_cooldowns(monkeypatch)

    client = MagicMock()
    client.get = AsyncMock(return_value=_resp(429, {"Retry-After": "300"}))

    with pytest.raises(RuntimeError):
        await _rate_limited_get(client, "https://example.test/q")

    assert cooldowns == [300.0]


@pytest.mark.asyncio
async def test_429_no_header_publishes_default_cooldown(monkeypatch):
    """A headerless 429 fail-fast publishes the conservative 60s default."""
    cooldowns = _capture_cooldowns(monkeypatch)

    client = MagicMock()
    client.get = AsyncMock(return_value=_resp(429, {}))

    with pytest.raises(RuntimeError):
        await _rate_limited_get(client, "https://example.test/q")

    assert cooldowns == [60.0]


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


def test_rate_limit_message_zero_and_cap():
    """NIT-8: retry_after==0 uses the generic wording (never 'asks for 0s'); an
    absurd value is capped in the display."""
    from arxiv_mcp_server.tools.search import _rate_limit_message

    # Zero → no "asks for Ns", falls back to the observed-cooldowns wording.
    zero_msg = _rate_limit_message(429, 0.0)
    assert "asks for" not in zero_msg
    assert "observed cooldowns" in zero_msg

    # A sane value is reported verbatim.
    assert "Server asks for 12s" in _rate_limit_message(429, 12.0)

    # An absurd value is capped at 1 day (86400s), not echoed literally.
    capped = _rate_limit_message(429, 10**9)
    assert "86400s" in capped
    assert "1000000000" not in capped


def test_min_interval_rejects_non_finite(monkeypatch):
    """nan/inf intervals would escape the OSError fail-open path via
    time.sleep raising ValueError/OverflowError — fall back to the default."""
    for bad in (float("nan"), float("inf"), float("-inf")):
        monkeypatch.setattr(arxiv_pacing.settings, "ARXIV_MIN_REQUEST_INTERVAL", bad)
        assert arxiv_pacing._min_interval() == 3.0


def test_parse_retry_after_overflow_is_parse_failure():
    from arxiv_mcp_server.tools.search import _parse_retry_after

    assert _parse_retry_after("9" * 400) is None
