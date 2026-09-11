"""
Dispatcher setup: registers the owner-only auth middleware and all handler
routers, in the order they should be checked.
"""

from __future__ import annotations

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage

from bot.handlers import (
    auth_commands,
    channel_setup,
    help,
    job_control,
    job_status,
    keep_alive,
    range_setup,
    start,
    words_setup,
)
from bot.middlewares.auth import OwnerOnlyMiddleware


def create_dispatcher(owner_id: int) -> Dispatcher:
    """
    Build and return a fully configured Dispatcher.

    FSM storage is in-memory: acceptable per architecture, since setup-flow
    state (waiting for a forwarded message, waiting for a word, etc.) is
    short-lived and only meaningful within a single live process -- it is
    NOT the same as job progress, which is persisted to Postgres and must
    survive restarts. Losing in-memory FSM state on restart just means the
    user has to redo the current setup step, which is an acceptable trade-off
    for simplicity per the finalized architecture.
    """
    dispatcher = Dispatcher(storage=MemoryStorage())

    # Make owner_id available for dependency injection into any handler that
    # declares an `owner_id: int` parameter (e.g. auth_commands.py's
    # /addauth, /removeauth, /listauth) -- aiogram auto-injects entries from
    # this workflow data by parameter name.
    dispatcher["owner_id"] = owner_id

    dispatcher.update.outer_middleware(OwnerOnlyMiddleware(owner_id=owner_id))

    dispatcher.include_router(start.router)
    dispatcher.include_router(auth_commands.router)
    dispatcher.include_router(channel_setup.router)
    dispatcher.include_router(range_setup.router)
    dispatcher.include_router(words_setup.router)
    dispatcher.include_router(job_control.router)
    dispatcher.include_router(job_status.router)
    dispatcher.include_router(keep_alive.router)
    dispatcher.include_router(help.router)

    return dispatcher
