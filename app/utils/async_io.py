"""Drain background work before propagating cancellation to its caller."""

from __future__ import annotations

import asyncio

from typing import Any, Callable, TypeVar

T = TypeVar("T")


async def finish_before_cancel(pending: asyncio.Future[T]) -> T:
    """Keep work alive through cancellation, including repeated shutdown requests."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(pending)
        except asyncio.CancelledError:
            if pending.cancelled():
                raise
            cancelled = True
            continue
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result


async def shielded_io(operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Finish a threaded operation before its resources can be closed by shutdown."""
    return await finish_before_cancel(asyncio.create_task(asyncio.to_thread(operation, *args, **kwargs)))
