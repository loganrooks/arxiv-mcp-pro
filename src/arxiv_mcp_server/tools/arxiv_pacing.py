"""Cross-process rate pacing for arXiv API requests.

arXiv asks for at least 3 seconds between requests **per IP, globally** — not per
process. Multiple agent sessions on one machine are separate processes, so an
in-process pacer alone cannot keep them under the limit (the headline failure
mode behind B17: 8 parallel sessions on one IP drew sustained HTTP 429s).

This module layers a cross-process gate on top of the in-process one. Sibling
processes coordinate through an advisory lock on a lock file in the shared
storage directory (``Settings().STORAGE_PATH / "arxiv_api.lock"``); the file's
mtime records the wall-clock time of the last global request.

Two coordination channels share the storage dir:
  * **Interval clock** — ``arxiv_api.lock`` (flock + mtime). Serialises requests
    to one-per-interval.
  * **Cooldown** — ``arxiv_api.cooldown`` (a small file whose content is an
    optional ``not_before`` wall-clock timestamp). When one lane hits a 429 with
    a ``Retry-After``, it publishes a shared back-off so sibling coroutines *and*
    processes stop firing into a server that asked the IP to wait, instead of
    each discovering the 429 independently.

    DEVIATION (C-1): the Codex design put ``not_before`` in the *lock file's*
    content, written via temp+``os.replace``. But ``os.replace`` swaps the inode,
    which orphans any ``flock`` a concurrent process holds on the old inode —
    silently breaking the interval-gate's mutual exclusion exactly during a 429
    storm (when the most lanes are active). The cooldown therefore lives in a
    *separate* file so the flock'd inode is never renamed; the atomic-write /
    lock-free-read / fail-open semantics are otherwise as specified.

Design constraints:
  * Stdlib + config only — no new runtime dependencies and no imports from other
    ``tools`` modules (avoids circular imports).
  * The asyncio event loop is never blocked: all file/lock work runs in a worker
    thread via :func:`asyncio.to_thread`.
  * **Fail-open.** The pacer must never break a request. Any error in the
    cross-process path degrades to in-process pacing only; the lock acquisition
    is bounded and, on timeout, the request proceeds without the lock.
"""

import asyncio
import errno
import logging
import os
import time
from pathlib import Path

from ..config import Settings

logger = logging.getLogger("arxiv-mcp-pro")

settings = Settings()

_LOCK_FILENAME = "arxiv_api.lock"
_COOLDOWN_FILENAME = "arxiv_api.cooldown"

# Upper bound on any honored cooldown (seconds). Bounds both the published
# back-off and the wait a reader will honor, so a bogus/huge Retry-After can
# never park a lane for an unreasonable time.
_COOLDOWN_CAP = 120.0

# In-process gate — module-global, mirrors the pacer that previously lived in
# search.py. Coordinates coroutines/threads within a single process.
_last_request_time: float = 0.0
_request_lock = asyncio.Lock()

# In-process cooldown deadline (monotonic). Mirrors the cross-process cooldown
# file so same-process sibling coroutines back off without a file read.
_not_before: float = 0.0

# Cross-process lock acquisition is non-blocking, retried in a short sleep loop,
# and bounded so a wedged sibling can never hang a request indefinitely.
_LOCK_ACQUIRE_TIMEOUT = 60.0  # seconds (floor; scaled with interval, see below)
_LOCK_POLL_INTERVAL = 0.1  # seconds

# Liveness cap on the re-check sleep loops (cooldown re-check and the
# cross-process gate's mtime re-stat): each extra iteration requires a fresh
# external event, so a well-behaved system converges in one or two passes.
_RECHECK_MAX_ITERS = 10

# errnos that mean "the lock is held by someone else" (transient contention) —
# worth retrying. Anything else (ENOLCK on NFS, EPERM, EBADF, ...) means locking
# is unavailable here, so we fail open immediately rather than burn the whole
# timeout. EACCES is what msvcrt.locking raises on contention on Windows.
_LOCK_CONTENTION_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})

# Warn once per process when a file/lock OSError forces a fail-open, so a
# persistently unwritable storage dir surfaces at least one WARNING instead of
# silently voiding the cross-process guarantee at DEBUG.
_fail_open_warned = False


def _warn_fail_open_once(context: str, exc: OSError) -> None:
    """Log the first cross-process fail-open at WARNING, the rest at DEBUG."""
    global _fail_open_warned
    if not _fail_open_warned:
        _fail_open_warned = True
        logger.warning(
            "arXiv cross-process pacing degraded to in-process only "
            "(%s: %s); cross-machine/-process coordination may be voided. "
            "Further occurrences log at DEBUG.",
            context,
            exc,
        )
    else:
        logger.debug("arXiv cross-process pacing fail-open (%s: %s)", context, exc)


# Platform-specific advisory file locking. POSIX gets fcntl.flock; Windows gets
# msvcrt.locking. Both release automatically on process death, so there is no
# stale-lock handling to do.
try:  # POSIX
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
    _HAVE_FCNTL = False

try:  # Windows
    import msvcrt

    _HAVE_MSVCRT = True
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]
    _HAVE_MSVCRT = False


def _min_interval() -> float:
    """Return the configured minimum arXiv request interval in seconds.

    Read dynamically (not captured at import) so tests and env changes take
    effect. Any non-numeric value falls back to the 3s default.
    """
    try:
        return float(settings.ARXIV_MIN_REQUEST_INTERVAL)
    except (TypeError, ValueError):
        return 3.0


def _lock_path() -> Path:
    """Absolute path to the shared cross-process lock file."""
    return Path(settings.STORAGE_PATH) / _LOCK_FILENAME


def _cooldown_path() -> Path:
    """Absolute path to the shared cross-process cooldown file."""
    return Path(settings.STORAGE_PATH) / _COOLDOWN_FILENAME


def _cooldown_remaining_inprocess() -> float:
    """Seconds until the in-process cooldown deadline (>= 0)."""
    return max(0.0, _not_before - time.monotonic())


def _read_cooldown_not_before() -> float:
    """Absolute wall-clock ``not_before`` published in the cooldown file (>= 0).

    Missing file, garbage content, or any read error → 0.0 (no cooldown,
    fail-open). A benign "no cooldown yet" (FileNotFoundError) is silent; an
    unexpected OSError warns once. Lock-free.
    """
    try:
        raw = _cooldown_path().read_bytes()
    except FileNotFoundError:
        return 0.0
    except OSError as exc:
        _warn_fail_open_once("cooldown read", exc)
        return 0.0
    try:
        return max(0.0, float(raw.strip()))
    except (ValueError, TypeError):
        return 0.0


def _cooldown_remaining_file() -> float:
    """Seconds until the cross-process cooldown deadline (>= 0), lock-free."""
    return max(0.0, _read_cooldown_not_before() - time.time())


def _write_cooldown_file(not_before: float) -> None:
    """Atomically publish ``not_before`` (wall clock), never SHORTENING an
    existing deadline (monotonic-max), with verify-and-retry convergence.

    A later, smaller cooldown (e.g. a 60s default) must not clobber an earlier
    larger one (e.g. a 120s Retry-After) — so we read the current value and write
    ``max(existing, requested)``. The read-modify-write is lock-free, so two
    concurrent publishers can both read the old value and the shorter one can
    ``os.replace`` last, losing the longer deadline. To converge without a lock:
    after each replace, re-read; if the file is now shorter than the deadline
    *this* call intended to guarantee, another writer clobbered us — redo the
    read-max-write. Bounded at 3 attempts; on exhaustion, proceed fail-open
    (cooldown is best-effort). Temp file + :func:`os.replace` keeps a concurrent
    lock-free reader from ever seeing a torn value. Raises OSError on write
    failure (caller handles).
    """
    guarantee = float(not_before)
    path = _cooldown_path()
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        for _ in range(3):
            target = max(guarantee, _read_cooldown_not_before())
            with open(tmp, "w") as fh:
                fh.write(repr(target))
            os.replace(tmp, str(path))
            # Verify a concurrent shorter publish didn't land after ours.
            if _read_cooldown_not_before() >= guarantee:
                return
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    # Could not confirm our deadline stuck after 3 passes — a sibling under-backs
    # off by the difference; acceptable for a best-effort channel.
    _warn_fail_open_once(
        "cooldown converge",
        OSError("cooldown deadline not confirmed after 3 write attempts"),
    )


def _lock_acquire_timeout(interval: float) -> float:
    """Effective lock-acquisition deadline (seconds) for the given interval.

    A sibling can legitimately hold the lock for one full interval while it
    sleeps out its own pacing, so the fixed 60s floor is too short once the
    configured interval approaches or exceeds it (queued callers would fail open
    early and violate pacing). Scale to ``2 * interval`` — room for the holder's
    wait plus our turn. Deep multi-process queues can still exceed this and fail
    open; that is the documented liveness valve, scaled rather than removed.
    """
    return max(_LOCK_ACQUIRE_TIMEOUT, 2.0 * interval)


def _acquire_file_lock(fd: int, interval: float) -> bool:
    """Acquire an exclusive advisory lock on ``fd``.

    Non-blocking attempts in a poll loop, bounded at
    ``_lock_acquire_timeout(interval)``. Returns True if the lock was acquired,
    False on timeout (caller proceeds without the lock — fail-open). If no
    advisory-locking primitive is available on this platform, returns False so
    the caller degrades gracefully.
    """
    if not (_HAVE_FCNTL or _HAVE_MSVCRT):
        return False

    deadline = time.monotonic() + _lock_acquire_timeout(interval)
    while True:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError as exc:
            # Only spin on genuine contention. Anything else (ENOLCK on NFS,
            # EPERM, EBADF, ...) means advisory locking isn't working here —
            # fail open immediately instead of burning the full 60s timeout.
            if exc.errno not in _LOCK_CONTENTION_ERRNOS:
                _warn_fail_open_once("lock unavailable", exc)
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(_LOCK_POLL_INTERVAL)


def _release_file_lock(fd: int) -> None:
    """Release the advisory lock held on ``fd`` (best-effort)."""
    try:
        if _HAVE_FCNTL:
            fcntl.flock(fd, fcntl.LOCK_UN)
        elif _HAVE_MSVCRT:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _cross_process_gate(interval: float) -> None:
    """Serialize arXiv requests across processes via the lock file (blocking).

    Runs in a worker thread. Holds the advisory lock while sleeping out the
    remaining interval so that concurrent processes are forced to wait their
    turn, then bumps the lock file's mtime to mark this request. Any OSError
    outside the request path is swallowed (fail-open).
    """
    path = _lock_path()
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        if not _acquire_file_lock(fd, interval):
            logger.warning(
                "arXiv cross-process lock not acquired within %.0fs; proceeding "
                "without cross-process pacing",
                _lock_acquire_timeout(interval),
            )
            return
        try:
            # Re-stat / re-sleep loop (C-2): record_arxiv_request bumps the mtime
            # lock-free, so a sleeper that read the old mtime would wake on a
            # stale schedule and under-pace relative to that just-recorded
            # request. After each sleep, re-stat and recompute; stop once a full
            # interval has elapsed since the last request. Capped for liveness —
            # each extra iteration requires a *fresh* external bump, so a
            # well-behaved system converges in one or two passes.
            for _ in range(_RECHECK_MAX_ITERS):
                # The file's mtime is the wall-clock time of the last global
                # request. A stat failure is treated as "no prior request" (0).
                try:
                    mtime = os.fstat(fd).st_mtime
                except OSError:
                    mtime = 0.0
                age = time.time() - mtime
                if age >= interval:
                    # A full interval has elapsed — but a lock-free
                    # record_arxiv_request bump could have landed between this
                    # passing check and our utime, which would let us send
                    # without pacing off that just-recorded request (FIX-3).
                    # Verify with one more fstat; if the mtime moved, re-loop
                    # (against the same budget) to re-evaluate the fresh mtime.
                    try:
                        verify_mtime = os.fstat(fd).st_mtime
                    except OSError:
                        verify_mtime = mtime
                    if verify_mtime == mtime:
                        break  # stable → safe to proceed
                    continue  # a bump landed; re-evaluate
                # Clamp a future mtime (age < 0 — clock skew / NTP step-back /
                # coarse-FS rounding) to at most one interval (MAJOR-1); a genuine
                # recent-past mtime waits only its remainder.
                time.sleep(min(interval - age, interval))
                if age < 0:
                    # Skew won't resolve by re-checking, and MAJOR-1 caps its cost
                    # at one interval — don't re-loop (which would sleep again).
                    break
            # The µs window between the verifying fstat and the utime below is the
            # irreducible cost of the lock-free record channel — a bump landing
            # there is not observed by this request.
            # Mark this request as the new "last global request".
            try:
                os.utime(str(path), None)
            except OSError:
                pass
        finally:
            _release_file_lock(fd)
    finally:
        os.close(fd)


async def _cooldown_remaining() -> float:
    """Seconds to wait for the shared cooldown across both channels (>= 0).

    Reads the in-process deadline (cheap) and the lock-free file (off the loop),
    takes the max, and caps at ``_COOLDOWN_CAP``. Fail-open on file errors.
    """
    remaining = _cooldown_remaining_inprocess()
    try:
        remaining = max(remaining, await asyncio.to_thread(_cooldown_remaining_file))
    except OSError as exc:  # fail-open: cooldown is best-effort
        _warn_fail_open_once("cooldown read", exc)
    return min(remaining, _COOLDOWN_CAP)


async def pace_arxiv_request() -> None:
    """Pace an outbound arXiv API request across coroutines and processes.

    Call immediately before an arXiv API request. With the interval at 0 all
    pacing is disabled (no waiting, no lock file). Otherwise: honor the shared
    cooldown, then acquire the in-process lock, re-check the cooldown, apply the
    in-process monotonic gate, and run the cross-process gate off the event loop.
    Fail-open — any cross-process error leaves the in-process pacing in force and
    never raises.
    """
    interval = _min_interval()
    if interval <= 0:
        return

    global _last_request_time
    # Fast-path cooldown check (pre-lock) so a lane backs off before even queuing
    # for the interval lock. Honored BEFORE the locked gate so a cooldown is
    # respected even while another lane holds the lock.
    remaining = await _cooldown_remaining()
    if remaining > 0:
        await asyncio.sleep(remaining)

    async with _request_lock:
        # Re-check the cooldown AFTER acquiring the lock (FIX-B): a caller queued
        # behind the lock for seconds would otherwise miss a cooldown published
        # while it waited (the pre-lock snapshot is stale). Loop check → sleep →
        # re-check until clear, bounded for liveness.
        slept_total = 0.0
        for _ in range(_RECHECK_MAX_ITERS):
            remaining = await _cooldown_remaining()
            if remaining <= 0:
                break
            # Bound the CUMULATIVE in-lock wait at one cap (FIX-4): a persistently
            # far-future not_before (corrupt file / clock step) must not park
            # every arXiv call in this process for cap × iterations while holding
            # the lock. Once a full cap has been waited, proceed (fail-open).
            remaining = min(remaining, _COOLDOWN_CAP - slept_total)
            if remaining <= 0:
                _warn_fail_open_once(
                    "cooldown wait",
                    OSError("cumulative in-lock cooldown wait hit the cap"),
                )
                break
            await asyncio.sleep(remaining)
            slept_total += remaining

        # In-process gate (cheap, always correct within this process).
        elapsed = time.monotonic() - _last_request_time
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)

        # Cross-process gate (blocking file/lock work, off the loop).
        try:
            await asyncio.to_thread(_cross_process_gate, interval)
        except OSError as exc:  # fail-open: in-process pacing already applied
            _warn_fail_open_once("cross-process gate", exc)

        _last_request_time = time.monotonic()


def record_arxiv_request() -> None:
    """Best-effort, non-blocking bump of the in-process + cross-process clocks.

    Use after work that issued its own arXiv request(s) outside
    :func:`pace_arxiv_request` — e.g. an ``arxiv``-library ``client.results``
    iteration that paged internally, or a ``Retry-After`` sleep — so sibling
    lanes pace off the same clock. Never waits on the lock and swallows OSError.
    With the interval at 0 the cross-process file is left untouched.
    """
    global _last_request_time
    _last_request_time = time.monotonic()

    if _min_interval() <= 0:
        return
    try:
        path = _lock_path()
        if path.exists():
            os.utime(str(path), None)
        else:
            # Create the clock file so siblings can pace off it.
            fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
            os.close(fd)
    except OSError as exc:
        _warn_fail_open_once("record request", exc)


def record_arxiv_cooldown(seconds: float) -> None:
    """Publish a shared back-off so sibling lanes stop firing for ``seconds``.

    Call on ANY arXiv 429/503 (whether we retry or fail fast). Best-effort, never
    raises. ``seconds`` is clamped to [0, 120]. Sets the in-process cooldown
    deadline AND (when pacing is enabled) the cross-process cooldown file, and
    bumps the in-process last-request clock. With the interval at 0 the
    cross-process file is left untouched (pacing fully disabled).
    """
    global _not_before, _last_request_time
    try:
        capped = min(max(float(seconds), 0.0), _COOLDOWN_CAP)
    except (TypeError, ValueError):
        capped = 0.0

    now_mono = time.monotonic()
    # Never shorten an in-flight cooldown (monotonic-max), mirroring the file.
    _not_before = max(_not_before, now_mono + capped)
    _last_request_time = now_mono

    if _min_interval() <= 0:
        return
    try:
        _write_cooldown_file(time.time() + capped)
    except OSError as exc:
        _warn_fail_open_once("record cooldown", exc)
