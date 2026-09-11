"""
Schema DDL and all SQL queries.

Tables:
  - bot_settings:   single-row table holding Caption Manager's configured
                    channel/range/words/preview cache, Post Manager's
                    independent target/range configuration, Caption
                    Injector text/position, and global settings (delay).
  - jobs:           one row per bulk run (caption edit or post delete);
                    enforced single active job by application logic in
                    job_manager, not by a DB constraint, since "active"
                    spans several JobStatus values.
  - message_logs:   one row per processed message, for edited/skipped/failed
                    history and debugging.

All functions take an explicit `conn` (asyncpg.Connection or Pool) so callers
control transaction boundaries where needed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import asyncpg

from db.models import (
    AuthorizedUser,
    CachedPreview,
    ChannelConfig,
    DEFAULT_PROMO_PHRASES,
    InjectorConfig,
    Job,
    JobConfig,
    JobStatus,
    MessageLogEntry,
    MessageLogStatus,
    NewJob,
    Settings,
    TaskType,
)

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS authorized_users (
    user_id         BIGINT PRIMARY KEY,
    display_name    TEXT,
    authorized_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS bot_settings (
    id                       SMALLINT PRIMARY KEY DEFAULT 1,
    channel_chat_id          BIGINT,
    channel_title            TEXT,
    configured_at            TIMESTAMPTZ,
    range_start_message_id   INTEGER,
    range_end_message_id     INTEGER,
    find_word                TEXT,
    replace_word             TEXT,
    preview_fingerprint      TEXT,
    preview_total_scanned    INTEGER,
    preview_would_edit       INTEGER,
    preview_would_skip       INTEGER,
    preview_would_fail       INTEGER,
    CONSTRAINT single_row CHECK (id = 1)
);

CREATE TABLE IF NOT EXISTS jobs (
    id                      SERIAL PRIMARY KEY,
    channel_chat_id         BIGINT NOT NULL,
    range_start_message_id  INTEGER NOT NULL,
    range_end_message_id    INTEGER NOT NULL,
    find_word               TEXT NOT NULL,
    replace_word            TEXT NOT NULL,
    remove_links            BOOLEAN NOT NULL DEFAULT TRUE,
    status                  TEXT NOT NULL,
    cursor_message_id       INTEGER NOT NULL,
    total_count             INTEGER NOT NULL DEFAULT 0,
    edited_count            INTEGER NOT NULL DEFAULT 0,
    skipped_count           INTEGER NOT NULL DEFAULT 0,
    failed_count            INTEGER NOT NULL DEFAULT 0,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS message_logs (
    id          SERIAL PRIMARY KEY,
    job_id      INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    message_id  INTEGER NOT NULL,
    status      TEXT NOT NULL,
    reason      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_message_logs_job_id ON message_logs(job_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

-- Migration guard: existing deployments created these tables before the
-- columns below existed. CREATE TABLE IF NOT EXISTS above is a no-op on an
-- existing table, so add the columns here idempotently.
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS range_start_message_id INTEGER;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS range_end_message_id INTEGER;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS find_word TEXT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS replace_word TEXT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS replace_word_entities JSONB;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS preview_fingerprint TEXT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS preview_total_scanned INTEGER;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS preview_would_edit INTEGER;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS preview_would_skip INTEGER;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS preview_would_fail INTEGER;

-- Global settings (shared delay for Caption Manager + Post Manager).
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS action_delay_seconds DOUBLE PRECISION NOT NULL DEFAULT 1.0;

-- Caption Manager feature enable/disable flags (Find & Replace, Remove
-- Hyperlinks, Caption Injector are independently toggleable; saving values
-- no longer implies enabled).
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS find_replace_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS remove_links_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS remove_urls_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS inject_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS promo_remover_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS add_hyperlink_enabled BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS add_hyperlink_url TEXT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS quote_removal_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- Caption Injector (independent of find/replace, applied as final step).
-- inject_text_entities preserves any Telegram formatting (hyperlinks etc.)
-- the user applied when sending the inject text -- e.g. only "JOIN ME"
-- linked, "on insta" left plain -- stored as a JSON array of
-- {type, offset, length, url} objects mirroring CaptionEntity.
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS inject_text TEXT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS inject_text_entities JSONB;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS inject_position TEXT;

-- Post Manager: fully separate target + range configuration, never shares
-- Caption Manager's channel_chat_id/range_start_message_id/etc. columns.
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS pm_target_chat_id BIGINT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS pm_target_type TEXT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS pm_target_title TEXT;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS pm_thread_id INTEGER;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS pm_range_start_message_id INTEGER;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS pm_range_end_message_id INTEGER;

-- Keep Alive: configurable background health-ping mode + interval.
-- mode is one of 'manual' | 'auto' | 'task_protection' (see
-- core/keep_alive_manager.py). No separate boolean flag is stored --
-- the mode value alone determines behavior.
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS keep_alive_mode TEXT NOT NULL DEFAULT 'manual';
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS keep_alive_interval_seconds INTEGER NOT NULL DEFAULT 540;

-- jobs: task_type discriminates caption_edit vs post_delete rows. Existing
-- rows default to caption_edit (their only prior meaning). find_word /
-- replace_word relaxed to nullable since post_delete jobs don't use them.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS task_type TEXT NOT NULL DEFAULT 'caption_edit';
ALTER TABLE jobs ALTER COLUMN find_word DROP NOT NULL;
ALTER TABLE jobs ALTER COLUMN replace_word DROP NOT NULL;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS inject_text TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS inject_text_entities JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS inject_position TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS target_thread_id INTEGER;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS processed_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS remove_urls BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS replace_word_entities JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS promo_phrases JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS add_hyperlink_url TEXT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS quote_removal_enabled BOOLEAN NOT NULL DEFAULT FALSE;

-- Promotional Line Remover: user-added custom trigger phrases, stored one
-- per row (independent of each other -- e.g. "Owner", "Network", "Updates"
-- added separately, not as one combined phrase). Default phrases are not
-- stored here; they're a fixed list in code.
CREATE TABLE IF NOT EXISTS promo_phrases (
    id          SERIAL PRIMARY KEY,
    phrase      TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


async def init_schema(conn: asyncpg.Pool) -> None:
    """Create tables/indexes if they don't already exist. Idempotent."""
    await conn.execute(SCHEMA_DDL)


# ---------------------------------------------------------------------------
# authorized_users -- allow-list of non-owner users granted access via
# /addauth. Independent of every other table; the owner always has access
# regardless of this list (checked separately in bot/middlewares/auth.py).
# ---------------------------------------------------------------------------

async def add_authorized_user(conn: asyncpg.Pool, user_id: int, display_name: str | None) -> AuthorizedUser:
    row = await conn.fetchrow(
        """
        INSERT INTO authorized_users (user_id, display_name)
        VALUES ($1, $2)
        ON CONFLICT (user_id) DO UPDATE
            SET display_name = COALESCE(EXCLUDED.display_name, authorized_users.display_name)
        RETURNING user_id, display_name, authorized_at
        """,
        user_id,
        display_name,
    )
    return AuthorizedUser(
        user_id=row["user_id"],
        display_name=row["display_name"],
        authorized_at=row["authorized_at"],
    )


async def remove_authorized_user(conn: asyncpg.Pool, user_id: int) -> bool:
    """Returns True if a row was actually removed (user was authorized)."""
    result = await conn.execute("DELETE FROM authorized_users WHERE user_id = $1", user_id)
    return result != "DELETE 0"


async def is_authorized(conn: asyncpg.Pool, user_id: int) -> bool:
    row = await conn.fetchrow("SELECT 1 FROM authorized_users WHERE user_id = $1", user_id)
    return row is not None


async def list_authorized_users(conn: asyncpg.Pool) -> list[AuthorizedUser]:
    rows = await conn.fetch(
        "SELECT user_id, display_name, authorized_at FROM authorized_users ORDER BY authorized_at ASC"
    )
    return [
        AuthorizedUser(user_id=r["user_id"], display_name=r["display_name"], authorized_at=r["authorized_at"])
        for r in rows
    ]


async def update_authorized_display_name(conn: asyncpg.Pool, user_id: int, display_name: str) -> None:
    """
    Best-effort: fills in a display name for an authorized user once they
    actually send a message the bot can read their name from (e.g. if they
    were /addauth'd by ID before ever interacting with the bot).
    """
    await conn.execute(
        "UPDATE authorized_users SET display_name = $1 WHERE user_id = $2 AND display_name IS NULL",
        display_name,
        user_id,
    )


# ---------------------------------------------------------------------------
# bot_settings / channel config (Caption Manager)
# ---------------------------------------------------------------------------

async def get_channel_config(conn: asyncpg.Pool) -> ChannelConfig | None:
    row = await conn.fetchrow(
        "SELECT channel_chat_id, channel_title, configured_at FROM bot_settings WHERE id = 1"
    )
    if row is None or row["channel_chat_id"] is None:
        return None
    return ChannelConfig(
        chat_id=row["channel_chat_id"],
        title=row["channel_title"],
        configured_at=row["configured_at"],
    )


async def set_channel_config(conn: asyncpg.Pool, chat_id: int, title: str) -> ChannelConfig:
    now = datetime.now(timezone.utc)
    await conn.execute(
        """
        INSERT INTO bot_settings (id, channel_chat_id, channel_title, configured_at)
        VALUES (1, $1, $2, $3)
        ON CONFLICT (id) DO UPDATE
            SET channel_chat_id = EXCLUDED.channel_chat_id,
                channel_title = EXCLUDED.channel_title,
                configured_at = EXCLUDED.configured_at
        """,
        chat_id,
        title,
        now,
    )
    return ChannelConfig(chat_id=chat_id, title=title, configured_at=now)


async def get_job_config(conn: asyncpg.Pool) -> JobConfig:
    """
    Fetch the persisted range + find/replace word configuration, plus
    Find & Replace / Remove Hyperlinks / Remove Direct URLs / Promotional
    Line Remover enabled state.

    Always returns a JobConfig (never None); individual fields may be None
    if not yet configured. Saving values does NOT imply enabled -- enabled
    state is tracked separately.
    """
    row = await conn.fetchrow(
        """
        SELECT range_start_message_id, range_end_message_id, find_word, replace_word,
               replace_word_entities, find_replace_enabled, remove_links_enabled, remove_urls_enabled,
               promo_remover_enabled, add_hyperlink_enabled, add_hyperlink_url, quote_removal_enabled
        FROM bot_settings WHERE id = 1
        """
    )
    custom_phrases = await get_promo_custom_phrases(conn)
    if row is None:
        return JobConfig(
            range_start_message_id=None,
            range_end_message_id=None,
            find_word=None,
            replace_word=None,
            replace_word_entities=None,
            find_replace_enabled=False,
            remove_links_enabled=False,
            remove_urls_enabled=False,
            promo_remover_enabled=False,
            promo_custom_phrases=custom_phrases,
            add_hyperlink_enabled=False,
            add_hyperlink_url=None,
            quote_removal_enabled=False,
        )
    entities_raw = row["replace_word_entities"]
    entities = json.loads(entities_raw) if isinstance(entities_raw, str) else entities_raw
    return JobConfig(
        range_start_message_id=row["range_start_message_id"],
        range_end_message_id=row["range_end_message_id"],
        find_word=row["find_word"],
        replace_word=row["replace_word"],
        replace_word_entities=entities,
        find_replace_enabled=row["find_replace_enabled"],
        remove_links_enabled=row["remove_links_enabled"],
        remove_urls_enabled=row["remove_urls_enabled"],
        promo_remover_enabled=row["promo_remover_enabled"],
        promo_custom_phrases=custom_phrases,
        add_hyperlink_enabled=row["add_hyperlink_enabled"],
        add_hyperlink_url=row["add_hyperlink_url"],
        quote_removal_enabled=row["quote_removal_enabled"],
    )


async def set_job_range(conn: asyncpg.Pool, range_start_message_id: int, range_end_message_id: int) -> None:
    """Persist the processing range, independent of channel/words fields."""
    await conn.execute(
        """
        INSERT INTO bot_settings (id, range_start_message_id, range_end_message_id)
        VALUES (1, $1, $2)
        ON CONFLICT (id) DO UPDATE
            SET range_start_message_id = EXCLUDED.range_start_message_id,
                range_end_message_id = EXCLUDED.range_end_message_id
        """,
        range_start_message_id,
        range_end_message_id,
    )


async def set_job_words(
    conn: asyncpg.Pool, find_word: str, replace_word: str, replace_word_entities: list[dict] | None = None
) -> None:
    """
    Persist the find/replace words, independent of channel/range fields.
    Does NOT change find_replace_enabled -- saving words is separate from
    enabling. `replace_word_entities` preserves any Telegram formatting
    (hyperlinks etc.) applied to the replacement text when it was sent.
    """
    await conn.execute(
        """
        INSERT INTO bot_settings (id, find_word, replace_word, replace_word_entities)
        VALUES (1, $1, $2, $3::jsonb)
        ON CONFLICT (id) DO UPDATE
            SET find_word = EXCLUDED.find_word,
                replace_word = EXCLUDED.replace_word,
                replace_word_entities = EXCLUDED.replace_word_entities
        """,
        find_word,
        replace_word,
        json.dumps(replace_word_entities) if replace_word_entities else None,
    )


async def set_find_replace_enabled(conn: asyncpg.Pool, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, find_replace_enabled)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET find_replace_enabled = EXCLUDED.find_replace_enabled
        """,
        enabled,
    )


async def set_remove_links_enabled(conn: asyncpg.Pool, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, remove_links_enabled)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET remove_links_enabled = EXCLUDED.remove_links_enabled
        """,
        enabled,
    )


async def set_remove_urls_enabled(conn: asyncpg.Pool, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, remove_urls_enabled)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET remove_urls_enabled = EXCLUDED.remove_urls_enabled
        """,
        enabled,
    )


# ---------------------------------------------------------------------------
# Promotional Line Remover
# ---------------------------------------------------------------------------

async def set_promo_remover_enabled(conn: asyncpg.Pool, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, promo_remover_enabled)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET promo_remover_enabled = EXCLUDED.promo_remover_enabled
        """,
        enabled,
    )


async def get_promo_custom_phrases(conn: asyncpg.Pool) -> list[str]:
    """Fetch all user-added custom promotional trigger phrases, oldest first."""
    rows = await conn.fetch("SELECT phrase FROM promo_phrases ORDER BY id ASC")
    return [row["phrase"] for row in rows]


async def add_promo_custom_phrase(conn: asyncpg.Pool, phrase: str) -> None:
    """Add one custom trigger phrase. Each phrase is stored independently."""
    await conn.execute("INSERT INTO promo_phrases (phrase) VALUES ($1)", phrase)


async def remove_promo_custom_phrase(conn: asyncpg.Pool, phrase: str) -> bool:
    """Remove one custom trigger phrase by exact (case-insensitive) text match."""
    result = await conn.execute(
        "DELETE FROM promo_phrases WHERE lower(phrase) = lower($1)", phrase
    )
    return result != "DELETE 0"


async def clear_promo_custom_phrases(conn: asyncpg.Pool) -> None:
    await conn.execute("DELETE FROM promo_phrases")


# ---------------------------------------------------------------------------
# Add Hyperlink
# ---------------------------------------------------------------------------

async def set_add_hyperlink_enabled(conn: asyncpg.Pool, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, add_hyperlink_enabled)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET add_hyperlink_enabled = EXCLUDED.add_hyperlink_enabled
        """,
        enabled,
    )


async def set_add_hyperlink_url(conn: asyncpg.Pool, url: str) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, add_hyperlink_url)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET add_hyperlink_url = EXCLUDED.add_hyperlink_url
        """,
        url,
    )


# ---------------------------------------------------------------------------
# Quote Removal
# ---------------------------------------------------------------------------

async def set_quote_removal_enabled(conn: asyncpg.Pool, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, quote_removal_enabled)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET quote_removal_enabled = EXCLUDED.quote_removal_enabled
        """,
        enabled,
    )


async def get_cached_preview(conn: asyncpg.Pool) -> CachedPreview | None:
    """Fetch the cached preview result, if any is stored."""
    row = await conn.fetchrow(
        """
        SELECT preview_fingerprint, preview_total_scanned, preview_would_edit,
               preview_would_skip, preview_would_fail
        FROM bot_settings WHERE id = 1
        """
    )
    if row is None or row["preview_fingerprint"] is None:
        return None
    return CachedPreview(
        fingerprint=row["preview_fingerprint"],
        total_scanned=row["preview_total_scanned"],
        would_edit_count=row["preview_would_edit"],
        would_skip_count=row["preview_would_skip"],
        would_fail_count=row["preview_would_fail"],
    )


async def set_cached_preview(conn: asyncpg.Pool, preview: CachedPreview) -> None:
    """Store a fresh preview result, keyed by its fingerprint."""
    await conn.execute(
        """
        INSERT INTO bot_settings (
            id, preview_fingerprint, preview_total_scanned,
            preview_would_edit, preview_would_skip, preview_would_fail
        )
        VALUES (1, $1, $2, $3, $4, $5)
        ON CONFLICT (id) DO UPDATE
            SET preview_fingerprint = EXCLUDED.preview_fingerprint,
                preview_total_scanned = EXCLUDED.preview_total_scanned,
                preview_would_edit = EXCLUDED.preview_would_edit,
                preview_would_skip = EXCLUDED.preview_would_skip,
                preview_would_fail = EXCLUDED.preview_would_fail
        """,
        preview.fingerprint,
        preview.total_scanned,
        preview.would_edit_count,
        preview.would_skip_count,
        preview.would_fail_count,
    )


async def clear_cached_preview(conn: asyncpg.Pool) -> None:
    """
    Invalidate the cached preview. Called after a caption-edit job completes
    (captions have changed, so any cached counts -- even for the same
    fingerprint -- are now stale), in addition to natural invalidation when
    the fingerprint changes due to different channel/range/words.
    """
    await conn.execute(
        "UPDATE bot_settings SET preview_fingerprint = NULL WHERE id = 1"
    )


# ---------------------------------------------------------------------------
# Caption Injector config
# ---------------------------------------------------------------------------

async def get_injector_config(conn: asyncpg.Pool) -> InjectorConfig:
    row = await conn.fetchrow(
        "SELECT inject_text, inject_text_entities, inject_enabled FROM bot_settings WHERE id = 1"
    )
    if row is None:
        return InjectorConfig(inject_text=None, inject_text_entities=None, enabled=False)
    entities_raw = row["inject_text_entities"]
    entities = json.loads(entities_raw) if isinstance(entities_raw, str) else entities_raw
    return InjectorConfig(
        inject_text=row["inject_text"],
        inject_text_entities=entities,
        enabled=row["inject_enabled"],
    )


async def set_injector_text(conn: asyncpg.Pool, inject_text: str, inject_text_entities: list[dict] | None = None) -> None:
    """
    Persist inject text. Does NOT change inject_enabled -- saving text is
    separate from enabling. `inject_text_entities` preserves any Telegram
    formatting (hyperlinks etc.) applied to the text when it was sent.
    """
    await conn.execute(
        """
        INSERT INTO bot_settings (id, inject_text, inject_text_entities)
        VALUES (1, $1, $2::jsonb)
        ON CONFLICT (id) DO UPDATE
            SET inject_text = EXCLUDED.inject_text,
                inject_text_entities = EXCLUDED.inject_text_entities
        """,
        inject_text,
        json.dumps(inject_text_entities) if inject_text_entities else None,
    )


async def set_injector_enabled(conn: asyncpg.Pool, enabled: bool) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, inject_enabled)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE SET inject_enabled = EXCLUDED.inject_enabled
        """,
        enabled,
    )


async def clear_injector_config(conn: asyncpg.Pool) -> None:
    await conn.execute(
        "UPDATE bot_settings SET inject_text = NULL, inject_text_entities = NULL, inject_enabled = FALSE WHERE id = 1"
    )


# ---------------------------------------------------------------------------
# Global settings
# ---------------------------------------------------------------------------

async def get_settings(conn: asyncpg.Pool) -> Settings:
    row = await conn.fetchrow("SELECT action_delay_seconds FROM bot_settings WHERE id = 1")
    if row is None:
        return Settings(action_delay_seconds=1.0)
    return Settings(action_delay_seconds=row["action_delay_seconds"])


async def set_action_delay(conn: asyncpg.Pool, delay_seconds: float) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, action_delay_seconds)
        VALUES (1, $1)
        ON CONFLICT (id) DO UPDATE
            SET action_delay_seconds = EXCLUDED.action_delay_seconds
        """,
        delay_seconds,
    )


async def get_keep_alive_config(conn: asyncpg.Pool) -> tuple[str, int]:
    """Returns (mode, interval_seconds). Defaults to ('manual', 540) if unset."""
    row = await conn.fetchrow(
        "SELECT keep_alive_mode, keep_alive_interval_seconds FROM bot_settings WHERE id = 1"
    )
    if row is None:
        return ("manual", 540)
    return (row["keep_alive_mode"], row["keep_alive_interval_seconds"])


async def set_keep_alive_config(conn: asyncpg.Pool, mode: str, interval_seconds: int) -> None:
    await conn.execute(
        """
        INSERT INTO bot_settings (id, keep_alive_mode, keep_alive_interval_seconds)
        VALUES (1, $1, $2)
        ON CONFLICT (id) DO UPDATE
            SET keep_alive_mode = EXCLUDED.keep_alive_mode,
                keep_alive_interval_seconds = EXCLUDED.keep_alive_interval_seconds
        """,
        mode,
        interval_seconds,
    )


# ---------------------------------------------------------------------------
# jobs
# ---------------------------------------------------------------------------

_JOB_COLUMNS = (
    "id, task_type, channel_chat_id, range_start_message_id, range_end_message_id, "
    "find_word, replace_word, replace_word_entities, remove_links, remove_urls, "
    "inject_text, inject_text_entities, inject_position, promo_phrases, add_hyperlink_url, "
    "quote_removal_enabled, "
    "target_thread_id, status, cursor_message_id, total_count, edited_count, "
    "skipped_count, failed_count, processed_count, created_at, updated_at"
)


def _row_to_job(row: asyncpg.Record) -> Job:
    replace_entities_raw = row["replace_word_entities"]
    replace_entities = (
        json.loads(replace_entities_raw) if isinstance(replace_entities_raw, str) else replace_entities_raw
    )
    inject_entities_raw = row["inject_text_entities"]
    inject_entities = (
        json.loads(inject_entities_raw) if isinstance(inject_entities_raw, str) else inject_entities_raw
    )
    promo_phrases_raw = row["promo_phrases"]
    promo_phrases = (
        json.loads(promo_phrases_raw) if isinstance(promo_phrases_raw, str) else promo_phrases_raw
    ) or []
    return Job(
        id=row["id"],
        task_type=TaskType(row["task_type"]),
        channel_chat_id=row["channel_chat_id"],
        range_start_message_id=row["range_start_message_id"],
        range_end_message_id=row["range_end_message_id"],
        find_word=row["find_word"],
        replace_word=row["replace_word"],
        replace_word_entities=replace_entities,
        remove_links=row["remove_links"],
        remove_urls=row["remove_urls"],
        inject_text=row["inject_text"],
        inject_text_entities=inject_entities,
        inject_position=row["inject_position"],
        promo_phrases=promo_phrases,
        add_hyperlink_url=row["add_hyperlink_url"],
        quote_removal_enabled=row["quote_removal_enabled"],
        target_thread_id=row["target_thread_id"],
        status=JobStatus(row["status"]),
        cursor_message_id=row["cursor_message_id"],
        total_count=row["total_count"],
        edited_count=row["edited_count"],
        skipped_count=row["skipped_count"],
        failed_count=row["failed_count"],
        processed_count=row["processed_count"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


async def create_job(conn: asyncpg.Pool, new_job: NewJob, total_count: int) -> Job:
    """
    Create a new job row in RUNNING status.

    `cursor_message_id` is initialized to `range_start_message_id - 1` so the
    runner's "start at cursor + 1" logic works uniformly for both fresh
    starts and resumes, regardless of task_type.
    """
    row = await conn.fetchrow(
        f"""
        INSERT INTO jobs (
            task_type, channel_chat_id, range_start_message_id, range_end_message_id,
            find_word, replace_word, replace_word_entities, remove_links, remove_urls,
            inject_text, inject_text_entities, inject_position, promo_phrases, add_hyperlink_url,
            quote_removal_enabled,
            target_thread_id, status, cursor_message_id, total_count
        )
        VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9, $10, $11::jsonb, $12, $13::jsonb, $14, $15, $16, $17, $18, $19)
        RETURNING {_JOB_COLUMNS}
        """,
        new_job.task_type.value,
        new_job.channel_chat_id,
        new_job.range_start_message_id,
        new_job.range_end_message_id,
        new_job.find_word,
        new_job.replace_word,
        json.dumps(new_job.replace_word_entities) if new_job.replace_word_entities else None,
        new_job.remove_links,
        new_job.remove_urls,
        new_job.inject_text,
        json.dumps(new_job.inject_text_entities) if new_job.inject_text_entities else None,
        new_job.inject_position,
        json.dumps(new_job.promo_phrases) if new_job.promo_phrases else None,
        new_job.add_hyperlink_url,
        new_job.quote_removal_enabled,
        new_job.target_thread_id,
        JobStatus.RUNNING.value,
        new_job.range_start_message_id - 1,
        total_count,
    )
    return _row_to_job(row)


async def get_job(conn: asyncpg.Pool, job_id: int) -> Job | None:
    row = await conn.fetchrow(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = $1", job_id)
    return _row_to_job(row) if row else None


async def get_active_job(conn: asyncpg.Pool) -> Job | None:
    """
    Return the single job currently in RUNNING or PAUSED state, if any,
    regardless of task_type (caption edit or post delete) -- single active
    task is enforced across both.
    """
    row = await conn.fetchrow(
        f"""
        SELECT {_JOB_COLUMNS} FROM jobs
        WHERE status IN ($1, $2)
        ORDER BY created_at DESC
        LIMIT 1
        """,
        JobStatus.RUNNING.value,
        JobStatus.PAUSED.value,
    )
    return _row_to_job(row) if row else None


async def update_job_status(conn: asyncpg.Pool, job_id: int, status: JobStatus) -> None:
    await conn.execute(
        "UPDATE jobs SET status = $1, updated_at = now() WHERE id = $2",
        status.value,
        job_id,
    )


async def update_job_progress(
    conn: asyncpg.Pool,
    job_id: int,
    cursor_message_id: int,
    edited_delta: int = 0,
    skipped_delta: int = 0,
    failed_delta: int = 0,
    processed_delta: int = 0,
) -> None:
    """
    Persist progress after processing a single message.

    Called after every message (not batched) per the architecture decision
    on exact resume — this keeps `cursor_message_id` always accurate even if
    the process crashes immediately after. `processed_delta` is the generic
    counter used by Post Manager (e.g. deleted); Caption Manager jobs leave
    it at 0 and use edited/skipped/failed as before.
    """
    await conn.execute(
        """
        UPDATE jobs
        SET cursor_message_id = $1,
            edited_count = edited_count + $2,
            skipped_count = skipped_count + $3,
            failed_count = failed_count + $4,
            processed_count = processed_count + $5,
            updated_at = now()
        WHERE id = $6
        """,
        cursor_message_id,
        edited_delta,
        skipped_delta,
        failed_delta,
        processed_delta,
        job_id,
    )


# ---------------------------------------------------------------------------
# message_logs
# ---------------------------------------------------------------------------

async def add_message_log(
    conn: asyncpg.Pool,
    job_id: int,
    message_id: int,
    status: MessageLogStatus,
    reason: str | None = None,
) -> MessageLogEntry:
    row = await conn.fetchrow(
        """
        INSERT INTO message_logs (job_id, message_id, status, reason)
        VALUES ($1, $2, $3, $4)
        RETURNING id, job_id, message_id, status, reason, created_at
        """,
        job_id,
        message_id,
        status.value,
        reason,
    )
    return MessageLogEntry(
        id=row["id"],
        job_id=row["job_id"],
        message_id=row["message_id"],
        status=MessageLogStatus(row["status"]),
        reason=row["reason"],
        created_at=row["created_at"],
    )


async def get_job_logs(
    conn: asyncpg.Pool,
    job_id: int,
    status_filter: MessageLogStatus | None = None,
    limit: int = 50,
) -> list[MessageLogEntry]:
    """Fetch recent log entries for a job, optionally filtered by status."""
    if status_filter is not None:
        rows = await conn.fetch(
            """
            SELECT id, job_id, message_id, status, reason, created_at
            FROM message_logs
            WHERE job_id = $1 AND status = $2
            ORDER BY id DESC
            LIMIT $3
            """,
            job_id,
            status_filter.value,
            limit,
        )
    else:
        rows = await conn.fetch(
            """
            SELECT id, job_id, message_id, status, reason, created_at
            FROM message_logs
            WHERE job_id = $1
            ORDER BY id DESC
            LIMIT $2
            """,
            job_id,
            limit,
        )
    return [
        MessageLogEntry(
            id=r["id"],
            job_id=r["job_id"],
            message_id=r["message_id"],
            status=MessageLogStatus(r["status"]),
            reason=r["reason"],
            created_at=r["created_at"],
        )
        for r in rows
    ]
