"""Run synchronous database work without blocking the event loop or abandoning a worker."""

import asyncio
from collections.abc import Callable
from contextlib import suppress


async def db[**P, T](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Propagate context to a worker and finish its call before unwinding cancellation.

    Cancelling an executor await cannot stop the underlying thread. Wait for that
    thread before callers close its connection, release locks or start recovery.
    Cancellation still propagates, including when the worker also fails.
    """
    task = asyncio.create_task(asyncio.to_thread(call, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(Exception, asyncio.CancelledError):
            task.result()
        raise
