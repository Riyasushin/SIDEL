"""Per-context request serialization for long-running dynamic injection."""

from __future__ import annotations

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import AsyncIterator

from router.context import ExperimentContext


_LOCKS: dict[str, asyncio.Lock] = {}
_LOCKS_GUARD = threading.Lock()
# The experiment runner is sequential, but an agent may leave a previous HTTP
# request alive while the next context begins.  Keep a process-wide gate as a
# second line of defence: only one router request may own an upstream TCP
# connection (including the L4 generate connection) at a time.
_ROUTER_REQUEST_SINGLEFLIGHT = asyncio.Lock()


def _key(context: ExperimentContext) -> str:
    return f"{context.run_id}:{context.context_id}"


def _lock_for(context: ExperimentContext) -> asyncio.Lock:
    key = _key(context)
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _LOCKS[key] = lock
        return lock


@asynccontextmanager
async def serialize_context_request(context: ExperimentContext) -> AsyncIterator[None]:
    """Do not advance a context's turn counter until its prior response is complete.

    A dynamic L4 call can take minutes. Holding this gate across the entire
    request lifecycle makes a concurrent follow-up request wait until the
    router has injected, defended, persisted, and returned the prior response.
    """

    await _ROUTER_REQUEST_SINGLEFLIGHT.acquire()
    lock = _lock_for(context)
    await lock.acquire()
    try:
        yield
    finally:
        lock.release()
        _ROUTER_REQUEST_SINGLEFLIGHT.release()


def reset_request_gates() -> None:
    """Test helper: discard all unlocked context gates."""

    global _ROUTER_REQUEST_SINGLEFLIGHT
    with _LOCKS_GUARD:
        if _ROUTER_REQUEST_SINGLEFLIGHT.locked() or any(lock.locked() for lock in _LOCKS.values()):
            raise RuntimeError("cannot reset request gates while a request is active")
        _LOCKS.clear()
        # IsolatedAsyncioTestCase creates one event loop per test.  Production
        # never calls this helper, but tests must not reuse a lock bound to a
        # prior loop.
        _ROUTER_REQUEST_SINGLEFLIGHT = asyncio.Lock()
