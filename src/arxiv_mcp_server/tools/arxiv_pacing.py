"""Cross-process rate pacing for arXiv API requests.

arXiv asks for at least 3 seconds between requests **per IP, globally** — not per
process. Multiple agent sessions on one machine are separate processes, so an
in-process pacer alone cannot keep them under the limit (the headline failure
mode behind B17: 8 parallel sessions on one IP drew sustained HTTP 429s).

This module layers a cross-process gate on top of the in-process one. Sibling
processes coordinate through an advisory lock on a lock file in the shared
storage directory (``Settings().STORAGE_PATH / "arxiv_api.lock"``); the file's
mtime records the wall-clock time of the last global request.

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
import logging
import os
import time
from pathlib import Path

from ..config import Settings

logger = logging.getLogger("arxiv-mcp-pro")

settings = Settings()

_LOCK_FILENAME = "arxiv_api.lock"

# In-process gate — module-global, mirrors the pacer that previously lived in
# search.py. Coordinates coroutines/threads within a single process.
_last_request_time: float = 0.0
_request_lock = asyncio.Lock()

# Cross-process lock acquisition is non-blocking, retried in a short sleep loop,
# and bounded so a wedged sibling can never hang a request indefinitely.
_LOCK_ACQUIRE_TIMEOUT = 60.0  # seconds
_LOCK_POLL_INTERVAL = 0.1  # seconds

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


def _acquire_file_lock(fd: int) -> bool:
    """Acquire an exclusive advisory lock on ``fd``.

    Non-blocking attempts in a poll loop, bounded at ``_LOCK_ACQUIRE_TIMEOUT``.
    Returns True if the lock was acquired, False on timeout (caller proceeds
    without the lock — fail-open). If no advisory-locking primitive is available
    on this platform, returns False so the caller degrades gracefully.
    """
    if not (_HAVE_FCNTL or _HAVE_MSVCRT):
        return False

    deadline = time.monotonic() + _LOCK_ACQUIRE_TIMEOUT
    while True:
        try:
            if _HAVE_FCNTL:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:  # Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
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
        if not _acquire_file_lock(fd):
            logger.warning(
                "arXiv cross-process lock not acquired within %.0fs; proceeding "
                "without cross-process pacing",
                _LOCK_ACQUIRE_TIMEOUT,
            )
            return
        try:
            # The file's mtime is the wall-clock time of the last global
            # request. A stat failure is treated as "no prior request" (0).
            try:
                mtime = os.fstat(fd).st_mtime
            except OSError:
                mtime = 0.0
            wait = interval - (time.time() - mtime)
            if wait > 0:
                time.sleep(wait)
            # Mark this request as the new "last global request".
            try:
                os.utime(str(path), None)
            except OSError:
                pass
        finally:
            _release_file_lock(fd)
    finally:
        os.close(fd)


async def pace_arxiv_request() -> None:
    """Pace an outbound arXiv API request across coroutines and processes.

    Call immediately before an arXiv API request. With the interval at 0 all
    pacing is disabled (no waiting, no lock file). Otherwise: acquire the
    in-process lock, apply the in-process monotonic gate, then run the
    cross-process gate off the event loop. Fail-open — any cross-process error
    leaves the in-process pacing in force and never raises.
    """
    interval = _min_interval()
    if interval <= 0:
        return

    global _last_request_time
    async with _request_lock:
        # In-process gate (cheap, always correct within this process).
        elapsed = time.monotonic() - _last_request_time
        if elapsed < interval:
            await asyncio.sleep(interval - elapsed)

        # Cross-process gate (blocking file/lock work, off the loop).
        try:
            await asyncio.to_thread(_cross_process_gate, interval)
        except OSError as exc:  # fail-open: in-process pacing already applied
            logger.debug("arXiv cross-process pacing skipped (OSError): %s", exc)

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
        logger.debug("arXiv record_arxiv_request skipped (OSError): %s", exc)
