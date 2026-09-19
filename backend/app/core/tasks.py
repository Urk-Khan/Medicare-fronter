"""
Background tasks that can't silently disappear.

asyncio only keeps a weak reference to tasks, so a fire-and-forget task can be garbage-collected
mid-flight, and an exception inside one is only printed when Python notices ("Task exception was
never retrieved"). spawn() keeps a reference until the task is done, logs any error with context,
and can retry a step a few times (useful when the database has a short hiccup).
"""

import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

_RUNNING: set[asyncio.Task] = set()


def spawn(fn: Callable[..., Awaitable], *args, label: str = "", retries: int = 0, delay: float = 2.0) -> asyncio.Task:
    async def _runner():
        for attempt in range(retries + 1):
            try:
                return await fn(*args)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                if attempt < retries:
                    logger.warning(f"{label or fn.__name__} failed ({e}); retrying in {delay:.0f}s")
                    await asyncio.sleep(delay)
                else:
                    logger.exception(f"{label or fn.__name__} failed: {e}")

    task = asyncio.create_task(_runner())
    _RUNNING.add(task)
    task.add_done_callback(_RUNNING.discard)
    return task
