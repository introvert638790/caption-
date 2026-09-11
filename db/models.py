"""
Data models for persisted entities.

These dataclasses mirror the database schema (see db/queries.py for the
actual DDL) and are the shared shape used by core/ and bot/ layers, so
callers never work with raw asyncpg Record objects directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


# Promotional Line Remover: always-active default trigger phrases (in
# addition to any user-added custom phrases). Deliberately excludes
# "Downloaded by" / "Extracted by" and single generic words that would
# cause false positives.
DEFAULT_PROMO_PHRASES: list[str] = [
    "Join channel",
    "Join our channel",
    "Join us",
    "Follow",
    "Follow us",
    "Follow our channel",
    "Subscribe",
    "Subscribe now",
    "Click here",
    "Click on",
    "Click below",
    "Link in bio",
    "For more updates",
]


class JobStatus(str, Enum):
    """Lifecycle states for a bulk job (caption edit or post delete).

    Note: dry-run previews are stateless and do NOT create a JobStatus row
    (see architecture decision: "stateless dry-run"). A row only exists once
    the user confirms and the real run begins.
    """

    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


class MessageLogStatus(str, Enum):
    """Outcome of processing a single message within a job."""

    EDITED = "edited"
    SKIPPED = "skipped"
    FAILED = "failed"


class TaskType(str, Enum):
    """Distinguishes what kind of job a `jobs` row represents."""

    CAPTION_EDIT = "caption_edit"


@dataclass(frozen=True)
class AuthorizedUser:
    """
    A non-owner user granted access via /addauth. Owner always has access
    regardless of this table -- this is purely an additional allow-list on
    top of the existing owner check.
    """

    user_id: int
    display_name: str | None
    authorized_at: datetime


@dataclass(frozen=True)
class ChannelConfig:
    """The single currently-configured target channel (Caption Manager only)."""

    chat_id: int
    title: str
    configured_at: datetime


@dataclass(frozen=True)
class JobConfig:
    """
    Persisted processing-range and find/replace word configuration for
    Caption Manager.

    Distinct from `Job` -- this is the *pending* setup the user configures
    via the button UI before starting a run, persisted so it survives menu
    navigation and process restarts. A `Job` row is only created once the
    user actually starts a run (see job_manager.start_new_job).
    """

    range_start_message_id: int | None
    range_end_message_id: int | None
    find_word: str | None
    replace_word: str | None
    replace_word_entities: list[dict] | None
    find_replace_enabled: bool
    remove_links_enabled: bool
    remove_urls_enabled: bool
    promo_remover_enabled: bool
    promo_custom_phrases: list[str]
    add_hyperlink_enabled: bool
    add_hyperlink_url: str | None
    quote_removal_enabled: bool


@dataclass(frozen=True)
class CachedPreview:
    """
    Cached result of the last dry-run preview scan, keyed by a fingerprint
    of (channel, range, find_word, replace_word). Reused by job_control to
    avoid rescanning when nothing has changed. Invalidated whenever the
    fingerprint no longer matches, or explicitly after a caption-edit job
    completes (since editing changes captions, making any cached counts
    stale even if channel/range/words are unchanged).
    """

    fingerprint: str
    total_scanned: int
    would_edit_count: int
    would_skip_count: int
    would_fail_count: int


@dataclass(frozen=True)
class InjectorConfig:
    """
    Persisted Caption Injector text + enabled state. Always appended at the
    bottom. `inject_text_entities` preserves any Telegram formatting
    (hyperlinks, bold, etc.) the user applied when sending the text, stored
    as a list of dicts mirroring core.caption_engine.CaptionEntity
    (type/offset/length/url), so a partial hyperlink within the injected
    text (e.g. only "JOIN ME" linked, "on insta" plain) is preserved
    exactly as the user formatted it.
    """

    inject_text: str | None
    inject_text_entities: list[dict] | None
    enabled: bool


@dataclass(frozen=True)
class Settings:
    """Global bot settings for Caption Manager."""

    action_delay_seconds: float


@dataclass(frozen=True)
class Job:
    """A single bulk job (caption edit or post delete), one at a time, enforced by job_manager."""

    id: int
    task_type: TaskType
    channel_chat_id: int
    range_start_message_id: int
    range_end_message_id: int
    find_word: str | None
    replace_word: str | None
    replace_word_entities: list[dict] | None
    remove_links: bool
    remove_urls: bool
    promo_phrases: list[str]
    inject_text: str | None
    inject_text_entities: list[dict] | None
    inject_position: str | None
    add_hyperlink_url: str | None
    quote_removal_enabled: bool
    target_thread_id: int | None
    status: JobStatus
    cursor_message_id: int  # last message_id fully processed; resume starts at cursor+1
    total_count: int
    edited_count: int
    skipped_count: int
    failed_count: int
    processed_count: int  # generic counter; unused by Caption Manager today
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class MessageLogEntry:
    """A single log row recording the outcome for one message in a job."""

    id: int
    job_id: int
    message_id: int
    status: MessageLogStatus
    reason: str | None
    created_at: datetime


@dataclass(frozen=True)
class NewJob:
    """Fields required to create a new job (id/timestamps assigned by DB)."""

    task_type: TaskType
    channel_chat_id: int
    range_start_message_id: int
    range_end_message_id: int
    find_word: str | None = None
    replace_word: str | None = None
    replace_word_entities: list[dict] | None = None
    remove_links: bool = True
    remove_urls: bool = False
    promo_phrases: list[str] = field(default_factory=list)
    inject_text: str | None = None
    inject_text_entities: list[dict] | None = None
    inject_position: str | None = None
    add_hyperlink_url: str | None = None
    quote_removal_enabled: bool = False
    target_thread_id: int | None = None


@dataclass
class JobProgress:
    """Mutable in-memory progress snapshot used during a running job.

    Persisted to the DB after every processed message so a restart can
    resume from `cursor_message_id`.
    """

    job_id: int
    cursor_message_id: int
    total_count: int
    edited_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    processed_count: int = 0
    recent_log: list[MessageLogEntry] = field(default_factory=list)
