"""Shared in-process locks for bot and web operations."""

import asyncio
from functools import wraps


def action_lock(application, name):
    locks = application.bot_data.setdefault("action_locks", {})
    if name not in locks:
        locks[name] = asyncio.Lock()
    return locks[name]


def serialized(name):
    def decorate(handler):
        @wraps(handler)
        async def wrapped(update, context):
            async with action_lock(context.application, name):
                return await handler(update, context)
        return wrapped
    return decorate
