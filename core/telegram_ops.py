"""
Thin wrappers around Telegram Bot API calls used for the caption-edit workflow.

This is the only module that talks to aiogram/Bot directly for the
forward-read-edit-cleanup cycle. It converts between aiogram's MessageEntity
and core.caption_engine's CaptionEntity, and centralizes retry/backoff so
job_runner (Phase 4) can stay focused on orchestration logic.

Read step uses forwardMessage (not copyMessage): forwardMessage returns the
full Message object synchronously, including caption/caption_entities, while
copyMessage returns only a MessageId with no content -- so copyMessage
cannot be used to inspect a message's caption without a separate
update-correlation mechanism. forwardMessage avoids that complexity entirely.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from enum import Enum

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import MessageEntity

from core.caption_engine import CaptionEntity

logger = logging.getLogger(__name__)

# Conservative defaults; Telegram doesn't publish an exact per-chat edit rate
# limit, so we throttle proactively rather than only reacting to 429s.
DEFAULT_MAX_RETRIES = 5
DEFAULT_BASE_BACKOFF_SECONDS = 1.0
DEFAULT_MAX_BACKOFF_SECONDS = 30.0
DEFAULT_INTER_MESSAGE_DELAY_SECONDS = 0.15  # ~6-7 ops/sec ceiling, safely under limits


class ReadOutcome(str, Enum):
    """Result of attempting to read a message's caption via forwardMessage."""

    OK = "ok"
    NOT_FOUND = "not_found"  # message doesn't exist / was deleted / can't be forwarded
    PERMISSION_ERROR = "permission_error"
    OTHER_ERROR = "other_error"


class WriteOutcome(str, Enum):
    """Result of attempting to edit a message's caption."""

    OK = "ok"
    NOT_MODIFIED = "not_modified"  # Telegram rejects no-op edits; treat as success
    NOT_FOUND = "not_found"
    PERMISSION_ERROR = "permission_error"
    OTHER_ERROR = "other_error"


@dataclass(frozen=True)
class ReadResult:
    outcome: ReadOutcome
    caption: str | None = None
    entities: list[CaptionEntity] | None = None
    error_detail: str | None = None
    is_text_message: bool = False


@dataclass(frozen=True)
class WriteResult:
    outcome: WriteOutcome
    error_detail: str | None = None


def _to_caption_entities(entities: list[MessageEntity] | None) -> list[CaptionEntity]:
    """Convert aiogram MessageEntity list to engine-native CaptionEntity list."""
    if not entities:
        return []
    return [
        CaptionEntity(type=e.type, offset=e.offset, length=e.length, url=e.url)
        for e in entities
    ]


def _to_aiogram_entities(entities: list[CaptionEntity]) -> list[MessageEntity]:
    """Convert engine-native CaptionEntity list back to aiogram MessageEntity."""
    result = []
    for e in entities:
        kwargs = {"type": e.type, "offset": e.offset, "length": e.length}
        if e.url is not None:
            kwargs["url"] = e.url
        result.append(MessageEntity(**kwargs))
    return result


def _classify_api_error(exc: TelegramAPIError) -> ReadOutcome:
    """
    Map a Telegram API error to a Read/Write-style classification.

    Distinguishes "message not found / can't be copied" (-> Skipped upstream)
    from permission errors (-> Failed upstream) from everything else
    (-> Failed upstream), per the finalized Skipped-vs-Failed contract.
    """
    message = str(exc).lower()
    if any(
        phrase in message
        for phrase in (
            "message to copy not found",
            "message to forward not found",
            "message identifier is not specified",
            "message not found",
            "message to edit not found",
        )
    ):
        return ReadOutcome.NOT_FOUND
    if any(
        phrase in message
        for phrase in (
            "not enough rights",
            "chat_admin_required",
            "have no rights",
            "not a member",
            "bot was kicked",
        )
    ):
        return ReadOutcome.PERMISSION_ERROR
    return ReadOutcome.OTHER_ERROR


async def _retry_with_backoff(coro_factory, max_retries: int = DEFAULT_MAX_RETRIES):
    """
    Execute an async operation with exponential backoff on TelegramRetryAfter
    (explicit 429) and transient errors. Raises the final exception if all
    retries are exhausted.

    `coro_factory` is a zero-arg callable returning a fresh coroutine each
    call, since coroutines can't be re-awaited after failing.
    """
    attempt = 0
    backoff = DEFAULT_BASE_BACKOFF_SECONDS
    while True:
        try:
            return await coro_factory()
        except TelegramRetryAfter as exc:
            attempt += 1
            if attempt > max_retries:
                raise
            wait_time = max(exc.retry_after, backoff)
            logger.warning("Rate limited by Telegram, waiting %.1fs (attempt %d)", wait_time, attempt)
            await asyncio.sleep(wait_time)
            backoff = min(backoff * 2, DEFAULT_MAX_BACKOFF_SECONDS)
        except TelegramBadRequest:
            # Bad requests (not found, permission errors) are not transient --
            # don't retry, let the caller classify and handle immediately.
            raise


async def read_caption_via_forward(
    bot: Bot,
    source_chat_id: int,
    scratch_chat_id: int,
    message_id: int,
) -> ReadResult:
    """
    Read a message's caption + entities by forwarding it into a private
    scratch chat, then deleting the forwarded copy immediately.

    forwardMessage (unlike copyMessage) returns the full Message object
    synchronously on success -- including `caption` and `caption_entities`
    -- so no update-correlation or webhook wait is needed. This is the
    simplest Bot-API-only way to inspect message content without having
    received it via update (no getMessage endpoint exists).

    The forwarded copy carries `forward_origin` metadata (a "Forwarded
    from" tag), which is irrelevant here since the scratch chat is private
    and the copy is deleted immediately after reading.

    Returns a ReadResult classifying the outcome so the caller can apply the
    Skipped-vs-Failed distinction correctly.
    """
    forwarded_message_id: int | None = None
    try:
        async def _forward():
            return await bot.forward_message(
                chat_id=scratch_chat_id,
                from_chat_id=source_chat_id,
                message_id=message_id,
                disable_notification=True,
            )

        forwarded = await _retry_with_backoff(_forward)
        forwarded_message_id = forwarded.message_id

        caption = forwarded.caption
        entities = _to_caption_entities(forwarded.caption_entities)
        if caption is not None:
            return ReadResult(outcome=ReadOutcome.OK, caption=caption, entities=entities, is_text_message=False)

        # No media caption present -- fall back to plain text (text-only
        # message). Media messages with no caption at all still correctly
        # fall through to caption=None here (is_text_message stays False),
        # preserving the existing "no caption" Skipped behavior for media.
        if forwarded.text is not None:
            text_entities = _to_caption_entities(forwarded.entities)
            return ReadResult(outcome=ReadOutcome.OK, caption=forwarded.text, entities=text_entities, is_text_message=True)

        return ReadResult(outcome=ReadOutcome.OK, caption=None, entities=None, is_text_message=False)

    except TelegramBadRequest as exc:
        classification = _classify_api_error(exc)
        return ReadResult(outcome=classification, error_detail=str(exc))
    except TelegramAPIError as exc:
        return ReadResult(outcome=ReadOutcome.OTHER_ERROR, error_detail=str(exc))
    finally:
        # Always clean up the forwarded copy, even if something above failed
        # after the forward succeeded but before we returned.
        if forwarded_message_id is not None:
            try:
                await bot.delete_message(chat_id=scratch_chat_id, message_id=forwarded_message_id)
            except TelegramAPIError as cleanup_exc:
                logger.warning(
                    "Failed to delete scratch forwarded copy (chat=%s, message_id=%s): %s",
                    scratch_chat_id,
                    forwarded_message_id,
                    cleanup_exc,
                )


async def write_caption(
    bot: Bot,
    chat_id: int,
    message_id: int,
    new_caption: str,
    new_entities: list[CaptionEntity],
    is_text_message: bool = False,
) -> WriteResult:
    """
    Apply a transformed caption back to the original channel message.

    Media messages (is_text_message=False, existing/default behavior) use
    editMessageCaption. Text-only messages use editMessageText instead --
    Telegram rejects editMessageCaption on a message with no media.
    """
    try:
        async def _edit():
            if is_text_message:
                return await bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=message_id,
                    text=new_caption,
                    entities=_to_aiogram_entities(new_entities),
                    parse_mode=None,
                )
            return await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=message_id,
                caption=new_caption,
                caption_entities=_to_aiogram_entities(new_entities),
                # The bot is constructed with a default parse_mode=HTML
                # (see main.py). Telegram treats parse_mode and
                # caption_entities as mutually exclusive on a single call --
                # when both are effectively present, parse_mode wins and
                # caption_entities is silently dropped (no error, caption
                # just renders as plain text). Explicitly override the
                # default to None here so our manually-built entities are
                # actually applied.
                parse_mode=None,
            )

        await _retry_with_backoff(_edit)
        return WriteResult(outcome=WriteOutcome.OK)

    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if "message is not modified" in message:
            # Telegram rejects edits that don't change anything; this is a
            # benign no-op case, not a failure (e.g. re-running an already
            # idempotently-cleaned caption on resume).
            return WriteResult(outcome=WriteOutcome.NOT_MODIFIED)

        classification = _classify_api_error(exc)
        write_outcome = {
            ReadOutcome.NOT_FOUND: WriteOutcome.NOT_FOUND,
            ReadOutcome.PERMISSION_ERROR: WriteOutcome.PERMISSION_ERROR,
            ReadOutcome.OTHER_ERROR: WriteOutcome.OTHER_ERROR,
        }[classification]
        return WriteResult(outcome=write_outcome, error_detail=str(exc))

    except TelegramAPIError as exc:
        return WriteResult(outcome=WriteOutcome.OTHER_ERROR, error_detail=str(exc))


async def throttle(delay_seconds: float | None = None) -> None:
    """
    Delay between per-message operations to proactively stay under
    Telegram's undocumented rate limits, on top of reactive retry-on-429
    handling above.

    If `delay_seconds` is omitted, falls back to the original fixed default
    (used by any caller that hasn't been updated to pass the configurable
    Settings.action_delay_seconds value) -- preserves prior behavior exactly
    for any call site not explicitly updated.
    """
    await asyncio.sleep(delay_seconds if delay_seconds is not None else DEFAULT_INTER_MESSAGE_DELAY_SECONDS)
