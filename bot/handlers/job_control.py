"""
Caption Manager menu, feature config screens (Caption Cleanup, Caption
Injector), the single unified Preview & Run flow, job pause/resume/stop,
and Settings (Delay).

Caption Manager is configuration-only: Find & Replace, Caption Cleanup
(Remove Direct URLs + Remove Hyperlink Formatting), and Caption Injector
each have their own config screen (Enable/Disable + values), with NO
scanning/preview/job-start on those screens. Preview & Run is the only
execution point -- it reads all Caption Manager settings, detects which
features are enabled, performs ONE scan, and shows a single Start/Cancel
confirmation.

Pipeline order (see core/caption_engine.transform_caption): Remove Direct
URLs -> Remove Hyperlink Formatting -> Find & Replace -> Caption Injector
-> Edit Caption.
"""

from __future__ import annotations

import hashlib
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton

from aiogram.exceptions import TelegramBadRequest

from bot import keyboards, progress_ui
from core.job_manager import JobManagerError
from core.job_runner import ProgressSnapshot, RunOutcome, StopReason, run_dry_run_preview
from db import queries
from db.connection import get_pool
from db.models import CachedPreview, DEFAULT_PROMO_PHRASES, JobStatus, TaskType

router = Router(name="job_control")


def _dicts_to_entities(raw):
    """Converts entity dicts (as stored in the DB/JSONB) back to CaptionEntity objects for core.caption_engine."""
    if not raw:
        return None
    from core.caption_engine import CaptionEntity
    return [
        CaptionEntity(type=d["type"], offset=d["offset"], length=d["length"], url=d.get("url"))
        for d in raw
    ]


class InjectorSetupStates(StatesGroup):
    waiting_for_text = State()


class PromoSetupStates(StatesGroup):
    waiting_for_add_phrase = State()
    waiting_for_remove_phrase = State()


class AddHyperlinkSetupStates(StatesGroup):
    waiting_for_url = State()


class DelaySetupStates(StatesGroup):
    waiting_for_delay = State()


def _format_progress(snapshot: ProgressSnapshot) -> str:
    return (
        f"⏳ Progress: {snapshot.processed_count}/{snapshot.total_count}\n"
        f"✅ Edited: {snapshot.edited_count}\n"
        f"⏭️ Skipped: {snapshot.skipped_count}\n"
        f"❌ Failed: {snapshot.failed_count}"
    )


async def _edit_job_message(bot, chat_id: int, message_id: int, text: str, reply_markup=None) -> None:
    """
    Edits the single job/progress message in place. Swallows edit failures
    (e.g. Telegram's "message is not modified" when a snapshot repeats, or
    the message having been deleted) -- a failed progress/completion edit
    must never crash the job loop or the calling handler.
    """
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            reply_markup=reply_markup,
        )
    except TelegramBadRequest:
        pass
    except Exception:
        pass


def _format_caption_status_card(snapshot: ProgressSnapshot, job_start_monotonic: float, footer: str = "ᴘʀᴏɢʀᴇssɪɴɢ") -> str:
    if snapshot.status == "sleeping":
        status_text = f"😴 Sleeping {snapshot.sleeping_seconds}s"
    else:
        status_text = "✏️ Editing"

    total = snapshot.total_count
    current = snapshot.processed_count
    percentage = min(100, round(current * 100 / total)) if total else 0

    eta_seconds = None
    if snapshot.status != "sleeping" and current > 0:
        elapsed = time.monotonic() - job_start_monotonic
        if elapsed > 0:
            speed = current / elapsed
            remaining = total - current
            if speed > 0 and remaining > 0:
                eta_seconds = round(remaining / speed)

    return progress_ui.format_status_card(
        current=current,
        total=total,
        edited=snapshot.edited_count,
        skipped=snapshot.skipped_count,
        failed=snapshot.failed_count,
        status=status_text,
        percentage=percentage,
        eta_seconds=eta_seconds,
        footer=footer,
    )


def _format_caption_complete_card(outcome: RunOutcome) -> str:
    job = outcome.final_job
    return progress_ui.format_status_card(
        current=job.total_count,
        total=job.total_count,
        edited=job.edited_count,
        skipped=job.skipped_count,
        failed=job.failed_count,
        status="✅ Completed",
        percentage=100,
        eta_seconds=None,
        footer="ᴄᴏᴍᴘʟᴇᴛᴇᴅ",
    )


def _format_caption_failed_card(outcome: RunOutcome) -> str:
    job = outcome.final_job
    processed = job.edited_count + job.skipped_count + job.failed_count
    percentage = min(100, round(processed * 100 / job.total_count)) if job.total_count else 0
    return progress_ui.format_status_card(
        current=processed,
        total=job.total_count,
        edited=job.edited_count,
        skipped=job.skipped_count,
        failed=job.failed_count,
        status="❌ Failed",
        percentage=percentage,
        eta_seconds=None,
        footer="ꜰᴀɪʟᴇᴅ",
    )


def _format_caption_stopped_card(job) -> str:
    processed = job.edited_count + job.skipped_count + job.failed_count
    percentage = min(100, round(processed * 100 / job.total_count)) if job.total_count else 0
    return progress_ui.format_status_card(
        current=processed,
        total=job.total_count,
        edited=job.edited_count,
        skipped=job.skipped_count,
        failed=job.failed_count,
        status="⏹️ Stopped",
        percentage=percentage,
        eta_seconds=None,
        footer="sᴛᴏᴘᴘᴇᴅ",
    )


def _format_caption_paused_card(job) -> str:
    processed = job.edited_count + job.skipped_count + job.failed_count
    percentage = min(100, round(processed * 100 / job.total_count)) if job.total_count else 0
    return progress_ui.format_status_card(
        current=processed,
        total=job.total_count,
        edited=job.edited_count,
        skipped=job.skipped_count,
        failed=job.failed_count,
        status="⏸️ Paused",
        percentage=percentage,
        eta_seconds=None,
        footer="ᴘᴀᴜsᴇᴅ",
    )


def _make_fingerprint(
    channel_chat_id: int,
    range_start: int,
    range_end: int,
    find_word: str,
    replace_word: str,
    find_replace_enabled: bool,
    remove_links_enabled: bool,
    remove_urls_enabled: bool,
    inject_text: str,
    inject_enabled: bool,
    promo_remover_enabled: bool = False,
    promo_custom_phrases: tuple = (),
    add_hyperlink_enabled: bool = False,
    add_hyperlink_url: str = "",
    quote_removal_enabled: bool = False,
) -> str:
    """
    Fingerprint identifying a specific Caption Manager configuration
    (channel, range, and all feature states/values), used to decide
    whether a cached preview is still valid. Cache is also explicitly
    cleared after a job completes, since editing changes captions even
    when the fingerprint is unchanged.
    """
    raw = (
        f"{channel_chat_id}|{range_start}|{range_end}|"
        f"{find_word}|{replace_word}|{find_replace_enabled}|"
        f"{remove_links_enabled}|{remove_urls_enabled}|{inject_text}|{inject_enabled}|"
        f"{promo_remover_enabled}|{'|'.join(promo_custom_phrases)}|"
        f"{add_hyperlink_enabled}|{add_hyperlink_url}|{quote_removal_enabled}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def _get_job_manager(callback: CallbackQuery):
    """
    Retrieves the shared JobManager instance attached to the Bot/Dispatcher
    at startup. Stored on `callback.bot` as a plain attribute to avoid a
    global singleton.
    """
    return callback.bot.job_manager


async def _check_no_active_task(callback: CallbackQuery) -> bool:
    """Returns True and shows the blocking alert if a task is already active."""
    job_manager = await _get_job_manager(callback)
    active = await job_manager.get_active_job()
    if active is not None:
        await callback.answer(
            "A task is already running. Please wait until it finishes or cancel it from Job Status.",
            show_alert=True,
        )
        return True
    return False


# ---------------------------------------------------------------------------
# Caption Manager submenu
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:caption_manager")
async def cb_open_caption_manager(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "📂 Caption Manager\n\nConfigure each feature below, then use Preview & Run from the main menu.",
        reply_markup=keyboards.caption_manager_menu(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Caption Cleanup (config screen only -- no scan/preview/job here).
# Two independent toggles: Remove Direct URLs, Remove Hyperlink Formatting.
# ---------------------------------------------------------------------------

async def _render_cleanup_screen(callback: CallbackQuery) -> None:
    pool = get_pool()
    job_config = await queries.get_job_config(pool)

    urls_status = "✅ Remove Direct URLs" if job_config.remove_urls_enabled else "❌ Remove Direct URLs"
    links_status = "✅ Remove Hyperlink Formatting" if job_config.remove_links_enabled else "❌ Remove Hyperlink Formatting"
    quotes_status = "✅ Quote Removal" if job_config.quote_removal_enabled else "❌ Quote Removal"

    url_toggle_button = (
        InlineKeyboardButton(text="Direct URLs: Disable", callback_data="cleanup:urls:disable")
        if job_config.remove_urls_enabled
        else InlineKeyboardButton(text="Direct URLs: Enable", callback_data="cleanup:urls:enable")
    )
    link_toggle_button = (
        InlineKeyboardButton(text="Hyperlinks: Disable", callback_data="cleanup:links:disable")
        if job_config.remove_links_enabled
        else InlineKeyboardButton(text="Hyperlinks: Enable", callback_data="cleanup:links:enable")
    )
    quote_toggle_button = (
        InlineKeyboardButton(text="Quote Removal: Disable", callback_data="quote_removal:disable")
        if job_config.quote_removal_enabled
        else InlineKeyboardButton(text="Quote Removal: Enable", callback_data="quote_removal:enable")
    )

    from aiogram.types import InlineKeyboardMarkup
    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [url_toggle_button],
            [link_toggle_button],
            [quote_toggle_button],
            [InlineKeyboardButton(text="⬅️ Back to Caption Manager", callback_data="menu:caption_manager")],
        ]
    )

    await callback.message.edit_text(
        f"Caption Cleanup\n\nCurrent Rules\n\n{urls_status}\n{links_status}\n{quotes_status}",
        reply_markup=markup,
    )


@router.callback_query(F.data == "cm:cleanup")
async def cb_open_cleanup(callback: CallbackQuery) -> None:
    if await _check_no_active_task(callback):
        return
    await _render_cleanup_screen(callback)
    await callback.answer()


@router.callback_query(F.data == "cleanup:urls:enable")
async def cb_cleanup_urls_enable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_remove_urls_enabled(pool, True)
    await _render_cleanup_screen(callback)
    await callback.answer("Remove Direct URLs enabled.")


@router.callback_query(F.data == "cleanup:urls:disable")
async def cb_cleanup_urls_disable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_remove_urls_enabled(pool, False)
    await _render_cleanup_screen(callback)
    await callback.answer("Remove Direct URLs disabled.")


@router.callback_query(F.data == "cleanup:links:enable")
async def cb_cleanup_links_enable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_remove_links_enabled(pool, True)
    await _render_cleanup_screen(callback)
    await callback.answer("Remove Hyperlink Formatting enabled.")


@router.callback_query(F.data == "cleanup:links:disable")
async def cb_cleanup_links_disable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_remove_links_enabled(pool, False)
    await _render_cleanup_screen(callback)
    await callback.answer("Remove Hyperlink Formatting disabled.")


# ---------------------------------------------------------------------------
# Quote Removal: strips blockquote/expandable_blockquote FORMATTING only
# (underlying text always preserved). Lives inside the Caption Cleanup
# screen (_render_cleanup_screen above), alongside Remove Direct URLs and
# Remove Hyperlinks -- no separate top-level menu entry / screen. Simple
# Enable/Disable toggle only -- no user-supplied config, so no FSM/
# text-capture is needed. Actual transform logic lives in
# core.caption_engine.remove_quotes.
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "quote_removal:enable")
async def cb_quote_removal_enable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_quote_removal_enabled(pool, True)
    await _render_cleanup_screen(callback)
    await callback.answer("Quote Removal enabled.")


@router.callback_query(F.data == "quote_removal:disable")
async def cb_quote_removal_disable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_quote_removal_enabled(pool, False)
    await _render_cleanup_screen(callback)
    await callback.answer("Quote Removal disabled.")


# ---------------------------------------------------------------------------
# Caption Injector (config screen only -- always appends at bottom)
# ---------------------------------------------------------------------------

async def _render_injector_screen(callback: CallbackQuery) -> None:
    pool = get_pool()
    injector_config = await queries.get_injector_config(pool)
    status_text = "Enabled ✅" if injector_config.enabled else "Disabled ❌"

    if injector_config.inject_text:
        current_text = f"Current text:\n{injector_config.inject_text}\n\n"
    else:
        current_text = "No inject text configured yet.\n\n"

    extra_buttons = [
        [InlineKeyboardButton(text="✏️ Set Text", callback_data="inj:set_text")],
        [InlineKeyboardButton(text="🗑️ Clear", callback_data="inj:clear")],
    ]

    await callback.message.edit_text(
        f"💉 Caption Injector\n\nStatus: {status_text}\n\n{current_text}"
        "Appends text to the bottom of captions as the final step.",
        reply_markup=keyboards.feature_toggle_menu(
            "inj", injector_config.enabled, extra_buttons, back_to="caption_manager"
        ),
    )


@router.callback_query(F.data == "cm:injector")
async def cb_open_injector(callback: CallbackQuery) -> None:
    if await _check_no_active_task(callback):
        return
    await _render_injector_screen(callback)
    await callback.answer()


@router.callback_query(F.data == "inj:enable")
async def cb_injector_enable(callback: CallbackQuery) -> None:
    pool = get_pool()
    injector_config = await queries.get_injector_config(pool)
    if not injector_config.inject_text:
        await callback.answer("Please set inject text first.", show_alert=True)
        return

    await queries.set_injector_enabled(pool, True)
    await _render_injector_screen(callback)
    await callback.answer("Caption Injector enabled.")


@router.callback_query(F.data == "inj:disable")
async def cb_injector_disable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_injector_enabled(pool, False)
    await _render_injector_screen(callback)
    await callback.answer("Caption Injector disabled.")


@router.callback_query(F.data == "inj:clear")
async def cb_injector_clear(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.clear_injector_config(pool)
    await _render_injector_screen(callback)
    await callback.answer("Cleared.")


@router.callback_query(F.data == "inj:set_text")
async def cb_injector_set_text(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(InjectorSetupStates.waiting_for_text)
    await callback.message.edit_text(
        "Send the text you want to inject into captions.",
        reply_markup=keyboards.back_to_caption_manager(),
    )
    await callback.answer()


@router.message(InjectorSetupStates.waiting_for_text)
async def handle_injector_text(message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Please send some text.", reply_markup=keyboards.back_to_caption_manager())
        return

    inject_text_entities = None
    if message.entities and (message.text or "") == text:
        inject_text_entities = [
            {"type": e.type, "offset": e.offset, "length": e.length, "url": e.url}
            for e in message.entities
            if e.type in ("text_link", "url")
        ]
        if not inject_text_entities:
            inject_text_entities = None

    pool = get_pool()
    await queries.set_injector_text(pool, text, inject_text_entities)
    await state.set_state(None)

    await message.answer(
        f"✅ Inject text set:\n{text}\n\nUse Enable on the Caption Injector screen to activate this feature.",
        reply_markup=keyboards.back_to_caption_manager(),
    )


# ---------------------------------------------------------------------------
# Add Hyperlink: wraps the ENTIRE final caption in one configured URL, as
# the very last pipeline step. Actual transform logic lives in
# core.caption_engine.add_full_caption_hyperlink; this section is only
# config/UI, following the same pattern as Caption Injector.
# ---------------------------------------------------------------------------

MAX_ADD_HYPERLINK_URL_LENGTH = 512


async def _render_add_hyperlink_screen(callback: CallbackQuery) -> None:
    pool = get_pool()
    job_config = await queries.get_job_config(pool)
    status_text = "Enabled ✅" if job_config.add_hyperlink_enabled else "Disabled ❌"

    if job_config.add_hyperlink_url:
        current_url = f"Current URL:\n{job_config.add_hyperlink_url}\n\n"
    else:
        current_url = "No URL configured yet.\n\n"

    extra_buttons = [
        [InlineKeyboardButton(text="✏️ Set URL", callback_data="addlink:set_url")],
    ]

    await callback.message.edit_text(
        f"🔗 Add Hyperlink\n\nStatus: {status_text}\n\n{current_url}"
        "Makes the entire final caption clickable, opening the configured URL. "
        "Always runs as the LAST step, after every other feature -- any existing "
        "hyperlinks in the caption are replaced by this one.",
        reply_markup=keyboards.feature_toggle_menu(
            "addlink", job_config.add_hyperlink_enabled, extra_buttons, back_to="caption_manager"
        ),
    )


@router.callback_query(F.data == "cm:add_hyperlink")
async def cb_open_add_hyperlink(callback: CallbackQuery) -> None:
    if await _check_no_active_task(callback):
        return
    await _render_add_hyperlink_screen(callback)
    await callback.answer()


@router.callback_query(F.data == "addlink:enable")
async def cb_add_hyperlink_enable(callback: CallbackQuery) -> None:
    pool = get_pool()
    job_config = await queries.get_job_config(pool)
    if not job_config.add_hyperlink_url:
        await callback.answer("Please set a URL first.", show_alert=True)
        return

    await queries.set_add_hyperlink_enabled(pool, True)
    await _render_add_hyperlink_screen(callback)
    await callback.answer("Add Hyperlink enabled.")


@router.callback_query(F.data == "addlink:disable")
async def cb_add_hyperlink_disable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_add_hyperlink_enabled(pool, False)
    await _render_add_hyperlink_screen(callback)
    await callback.answer("Add Hyperlink disabled.")


@router.callback_query(F.data == "addlink:set_url")
async def cb_add_hyperlink_set_url(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(AddHyperlinkSetupStates.waiting_for_url)
    await callback.message.edit_text(
        "Send the URL. It must start with http:// or https://",
        reply_markup=keyboards.back_to_caption_manager(),
    )
    await callback.answer()


@router.message(AddHyperlinkSetupStates.waiting_for_url)
async def handle_add_hyperlink_url(message, state: FSMContext) -> None:
    url = (message.text or "").strip()
    if not url:
        await message.answer("Please send some text.", reply_markup=keyboards.back_to_caption_manager())
        return
    if not (url.startswith("http://") or url.startswith("https://")):
        await message.answer(
            "That doesn't look like a valid URL. It must start with http:// or https://\n\n"
            "Please send a valid URL.",
            reply_markup=keyboards.back_to_caption_manager(),
        )
        return
    if len(url) > MAX_ADD_HYPERLINK_URL_LENGTH:
        await message.answer(
            f"URL is too long (max {MAX_ADD_HYPERLINK_URL_LENGTH} characters). "
            "Please send a shorter URL.",
            reply_markup=keyboards.back_to_caption_manager(),
        )
        return

    pool = get_pool()
    await queries.set_add_hyperlink_url(pool, url)
    await state.set_state(None)

    await message.answer(
        f"✅ Hyperlink URL set:\n{url}\n\nUse Enable on the Add Hyperlink screen to activate this feature.",
        reply_markup=keyboards.back_to_caption_manager(),
    )


# ---------------------------------------------------------------------------
# Promotional Line Remover (config screen only). Default trigger phrases are
# always active; custom phrases are stored independently (one row each) and
# added on top of the defaults. Matching/removal itself is entity-safe and
# lives in core.caption_engine.remove_promotional_lines.
# ---------------------------------------------------------------------------

MAX_PROMO_PHRASE_LENGTH = 200  # same limit as Find & Replace's find/replace words


async def _render_promo_screen(callback: CallbackQuery) -> None:
    pool = get_pool()
    job_config = await queries.get_job_config(pool)
    status_text = "Enabled ✅" if job_config.promo_remover_enabled else "Disabled ❌"

    defaults_list = "\n".join(f"• {p}" for p in DEFAULT_PROMO_PHRASES)
    if job_config.promo_custom_phrases:
        custom_list = "\n".join(f"• {p}" for p in job_config.promo_custom_phrases)
    else:
        custom_list = "(none)"

    extra_buttons = [
        [InlineKeyboardButton(text="➕ Add Custom Phrase", callback_data="promo:add")],
        [InlineKeyboardButton(text="🗑️ Remove Custom Phrase", callback_data="promo:remove")],
        [InlineKeyboardButton(text="👁️ View All Phrases", callback_data="promo:view_all")],
    ]

    await callback.message.edit_text(
        f"🚫 Promotional Line Remover\n\n"
        f"Status: {status_text}\n\n"
        f"Default Triggers:\n{defaults_list}\n\n"
        f"Custom Triggers:\n{custom_list}\n\n"
        "If any trigger appears anywhere in a line (case-insensitive, whole-word/phrase), "
        "that entire line is removed.",
        reply_markup=keyboards.feature_toggle_menu(
            "promo", job_config.promo_remover_enabled, extra_buttons, back_to="caption_manager"
        ),
    )


@router.callback_query(F.data == "cm:promo_remover")
async def cb_open_promo(callback: CallbackQuery) -> None:
    if await _check_no_active_task(callback):
        return
    await _render_promo_screen(callback)
    await callback.answer()


@router.callback_query(F.data == "promo:enable")
async def cb_promo_enable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_promo_remover_enabled(pool, True)
    await _render_promo_screen(callback)
    await callback.answer("Promotional Line Remover enabled.")


@router.callback_query(F.data == "promo:disable")
async def cb_promo_disable(callback: CallbackQuery) -> None:
    pool = get_pool()
    await queries.set_promo_remover_enabled(pool, False)
    await _render_promo_screen(callback)
    await callback.answer("Promotional Line Remover disabled.")


@router.callback_query(F.data == "promo:add")
async def cb_promo_add(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoSetupStates.waiting_for_add_phrase)
    await callback.message.edit_text(
        "Send the trigger phrase. Matching is case-insensitive and if it appears "
        "anywhere in a line, the entire line will be removed.",
        reply_markup=keyboards.back_to_caption_manager(),
    )
    await callback.answer()


@router.message(PromoSetupStates.waiting_for_add_phrase)
async def handle_promo_add_phrase(message, state: FSMContext) -> None:
    phrase = (message.text or "").strip()
    if not phrase:
        await message.answer("Please send some text.", reply_markup=keyboards.back_to_caption_manager())
        return
    if len(phrase) > MAX_PROMO_PHRASE_LENGTH:
        await message.answer(
            f"Trigger phrase is too long (max {MAX_PROMO_PHRASE_LENGTH} characters).",
            reply_markup=keyboards.back_to_caption_manager(),
        )
        return

    pool = get_pool()
    await queries.add_promo_custom_phrase(pool, phrase)
    await state.set_state(None)

    await message.answer(
        f"✅ Custom trigger added:\n{phrase}\n\nUse Enable on the Promotional Line Remover screen to activate this feature.",
        reply_markup=keyboards.back_to_caption_manager(),
    )


@router.callback_query(F.data == "promo:remove")
async def cb_promo_remove(callback: CallbackQuery, state: FSMContext) -> None:
    pool = get_pool()
    custom_phrases = await queries.get_promo_custom_phrases(pool)
    if not custom_phrases:
        await callback.answer("No custom phrases to remove.", show_alert=True)
        return
    await state.set_state(PromoSetupStates.waiting_for_remove_phrase)
    current_list = "\n".join(f"• {p}" for p in custom_phrases)
    await callback.message.edit_text(
        f"Send the exact custom trigger phrase to remove:\n\n{current_list}",
        reply_markup=keyboards.back_to_caption_manager(),
    )
    await callback.answer()


@router.message(PromoSetupStates.waiting_for_remove_phrase)
async def handle_promo_remove_phrase(message, state: FSMContext) -> None:
    phrase = (message.text or "").strip()
    if not phrase:
        await message.answer("Please send some text.", reply_markup=keyboards.back_to_caption_manager())
        return

    pool = get_pool()
    removed = await queries.remove_promo_custom_phrase(pool, phrase)
    await state.set_state(None)

    if removed:
        await message.answer(
            f"✅ Removed custom trigger:\n{phrase}",
            reply_markup=keyboards.back_to_caption_manager(),
        )
    else:
        await message.answer(
            f"No custom trigger matching \"{phrase}\" was found.",
            reply_markup=keyboards.back_to_caption_manager(),
        )


@router.callback_query(F.data == "promo:view_all")
async def cb_promo_view_all(callback: CallbackQuery) -> None:
    pool = get_pool()
    custom_phrases = await queries.get_promo_custom_phrases(pool)

    defaults_list = "\n".join(f"• {p}" for p in DEFAULT_PROMO_PHRASES)
    if custom_phrases:
        custom_list = "\n".join(f"• {p}" for p in custom_phrases)
    else:
        custom_list = "(none)"

    await callback.message.edit_text(
        f"👁️ All Promotional Line Remover Triggers\n\n"
        f"Default Triggers (always active when enabled):\n{defaults_list}\n\n"
        f"Custom Triggers:\n{custom_list}",
        reply_markup=keyboards.back_to_caption_manager(),
    )
    await callback.answer()


# ---------------------------------------------------------------------------
# Preview & Run -- the ONLY execution point for Caption Manager
# ---------------------------------------------------------------------------

async def _run_preview_scan(callback: CallbackQuery, state: FSMContext) -> None:
    """
    Performs ONE fresh dry-run scan reflecting all currently enabled
    Caption Manager features, and caches the result. Shared by both the
    initial "Preview & Run" tap and the "Rescan" button.
    """
    pool = get_pool()
    channel = await queries.get_channel_config(pool)
    job_config = await queries.get_job_config(pool)
    injector_config = await queries.get_injector_config(pool)

    range_start = job_config.range_start_message_id
    range_end = job_config.range_end_message_id
    find_word = job_config.find_word if job_config.find_replace_enabled else ""
    replace_word = job_config.replace_word if job_config.find_replace_enabled else ""
    remove_links = job_config.remove_links_enabled
    remove_urls = job_config.remove_urls_enabled
    inject_text_value = injector_config.inject_text if injector_config.enabled else None
    promo_phrases = (
        DEFAULT_PROMO_PHRASES + job_config.promo_custom_phrases
        if job_config.promo_remover_enabled
        else []
    )
    add_hyperlink_url = job_config.add_hyperlink_url if job_config.add_hyperlink_enabled else None
    quote_removal_enabled = job_config.quote_removal_enabled

    replace_word_entities = _dicts_to_entities(
        job_config.replace_word_entities if job_config.find_replace_enabled else None
    )
    inject_text_entities = _dicts_to_entities(
        injector_config.inject_text_entities if injector_config.enabled else None
    )

    await callback.message.edit_text(
        "🔍 Scanning range, please wait...",
        reply_markup=keyboards.main_menu_button(),
    )

    scratch_chat_id = callback.bot.scratch_chat_id
    preview = await run_dry_run_preview(
        bot=callback.bot,
        scratch_chat_id=scratch_chat_id,
        channel_chat_id=channel.chat_id,
        range_start_message_id=range_start,
        range_end_message_id=range_end,
        find_word=find_word or "",
        replace_word=replace_word or "",
        remove_links=remove_links,
        inject_text_value=inject_text_value,
        remove_urls=remove_urls,
        replace_word_entities=replace_word_entities,
        inject_text_entities=inject_text_entities,
        promo_phrases=promo_phrases,
        add_hyperlink_url=add_hyperlink_url,
        quote_removal_enabled=quote_removal_enabled,
    )

    fingerprint = _make_fingerprint(
        channel.chat_id, range_start, range_end,
        find_word or "", replace_word or "", job_config.find_replace_enabled,
        remove_links, remove_urls, injector_config.inject_text or "", injector_config.enabled,
        job_config.promo_remover_enabled, tuple(job_config.promo_custom_phrases),
        job_config.add_hyperlink_enabled, add_hyperlink_url or "",
        quote_removal_enabled,
    )
    await queries.set_cached_preview(
        pool,
        CachedPreview(
            fingerprint=fingerprint,
            total_scanned=preview.total_scanned,
            would_edit_count=preview.would_edit_count,
            would_skip_count=preview.would_skip_count,
            would_fail_count=preview.would_fail_count,
        ),
    )

    active_features = (
        f"{'✅' if remove_urls else '❌'} Remove Direct URLs\n"
        f"{'✅' if remove_links else '❌'} Remove Hyperlinks\n"
        f"{'✅' if quote_removal_enabled else '❌'} Quote Removal\n"
        f"{'✅' if job_config.promo_remover_enabled else '❌'} Promotional Line Remover\n"
        f"{'✅' if job_config.find_replace_enabled else '❌'} Find & Replace\n"
        f"{'✅' if injector_config.enabled else '❌'} Caption Injector\n"
        f"{'✅' if job_config.add_hyperlink_enabled else '❌'} Add Hyperlink"
    )

    await callback.message.edit_text(
        f"📋 Preview complete\n\n"
        f"Range: {range_start} \u2192 {range_end}\n"
        f"Would edit: {preview.would_edit_count}\n"
        f"Would skip: {preview.would_skip_count}\n"
        f"Would fail: {preview.would_fail_count}\n\n"
        f"Active Features\n{active_features}\n\n"
        "Start the real run?",
        reply_markup=keyboards.confirm_run(preview.would_edit_count),
    )


@router.callback_query(F.data == "menu:preview")
async def cb_start_preview(callback: CallbackQuery, state: FSMContext) -> None:
    pool = get_pool()

    channel = await queries.get_channel_config(pool)
    if channel is None:
        await callback.answer("Please configure a channel first.", show_alert=True)
        return

    job_config = await queries.get_job_config(pool)
    injector_config = await queries.get_injector_config(pool)

    if job_config.range_start_message_id is None or job_config.range_end_message_id is None:
        await callback.answer("Please set a processing range first.", show_alert=True)
        return

    if not (
        job_config.find_replace_enabled
        or job_config.remove_links_enabled
        or job_config.remove_urls_enabled
        or job_config.promo_remover_enabled
        or job_config.add_hyperlink_enabled
        or job_config.quote_removal_enabled
        or injector_config.enabled
    ):
        await callback.answer(
            "No features enabled. Enable at least one in Caption Manager first.", show_alert=True
        )
        return

    if await _check_no_active_task(callback):
        return

    range_start = job_config.range_start_message_id
    range_end = job_config.range_end_message_id
    find_word = job_config.find_word if job_config.find_replace_enabled else ""
    replace_word = job_config.replace_word if job_config.find_replace_enabled else ""

    fingerprint = _make_fingerprint(
        channel.chat_id, range_start, range_end,
        find_word or "", replace_word or "", job_config.find_replace_enabled,
        job_config.remove_links_enabled, job_config.remove_urls_enabled,
        injector_config.inject_text or "", injector_config.enabled,
    )
    cached = await queries.get_cached_preview(pool)

    if cached is not None and cached.fingerprint == fingerprint:
        active_features = (
            f"{'✅' if job_config.remove_urls_enabled else '❌'} Remove Direct URLs\n"
            f"{'✅' if job_config.remove_links_enabled else '❌'} Remove Hyperlinks\n"
            f"{'✅' if job_config.find_replace_enabled else '❌'} Find & Replace\n"
            f"{'✅' if injector_config.enabled else '❌'} Caption Injector"
        )
        await callback.message.edit_text(
            f"📋 Using cached preview (nothing changed since last scan)\n\n"
            f"Range: {range_start} \u2192 {range_end}\n"
            f"Would edit: {cached.would_edit_count}\n"
            f"Would skip: {cached.would_skip_count}\n"
            f"Would fail: {cached.would_fail_count}\n\n"
            f"Active Features\n{active_features}",
            reply_markup=keyboards.cached_preview_actions(cached.would_edit_count),
        )
        await callback.answer()
        return

    await callback.answer()
    await _run_preview_scan(callback, state)


@router.callback_query(F.data == "menu:preview:rescan")
async def cb_rescan_preview(callback: CallbackQuery, state: FSMContext) -> None:
    await callback.answer()
    await _run_preview_scan(callback, state)


@router.callback_query(F.data == "job:start")
async def cb_start_job(callback: CallbackQuery, state: FSMContext) -> None:
    pool = get_pool()
    channel = await queries.get_channel_config(pool)
    job_config = await queries.get_job_config(pool)
    injector_config = await queries.get_injector_config(pool)

    range_start = job_config.range_start_message_id
    range_end = job_config.range_end_message_id

    if channel is None or range_start is None or range_end is None:
        await callback.answer("Setup incomplete, please start again.", show_alert=True)
        return

    find_word = job_config.find_word if job_config.find_replace_enabled else ""
    replace_word = job_config.replace_word if job_config.find_replace_enabled else ""
    inject_text_value = injector_config.inject_text if injector_config.enabled else None
    replace_word_entities = job_config.replace_word_entities if job_config.find_replace_enabled else None
    inject_text_entities = injector_config.inject_text_entities if injector_config.enabled else None
    promo_phrases = (
        DEFAULT_PROMO_PHRASES + job_config.promo_custom_phrases
        if job_config.promo_remover_enabled
        else []
    )
    add_hyperlink_url = job_config.add_hyperlink_url if job_config.add_hyperlink_enabled else None

    job_manager = await _get_job_manager(callback)

    progress_chat_id = callback.from_user.id
    progress_message_id = callback.message.message_id
    job_start_monotonic = time.monotonic()

    async def progress_callback(snapshot: ProgressSnapshot) -> None:
        await _edit_job_message(
            callback.bot,
            progress_chat_id,
            progress_message_id,
            _format_caption_status_card(snapshot, job_start_monotonic),
            reply_markup=keyboards.job_controls(is_paused=False),
        )

    async def completion_callback(outcome: RunOutcome) -> None:
        if outcome.stop_reason == StopReason.COMPLETED:
            text = _format_caption_complete_card(outcome)
        elif outcome.stop_reason == StopReason.FAILED:
            text = _format_caption_failed_card(outcome)
        else:
            return  # STOPPED_BY_USER is handled by the pause/stop handlers themselves
        await _edit_job_message(
            callback.bot,
            progress_chat_id,
            progress_message_id,
            text,
            reply_markup=keyboards.back_to_menu(),
        )

    try:
        job = await job_manager.start_new_job(
            task_type=TaskType.CAPTION_EDIT,
            channel_chat_id=channel.chat_id,
            range_start_message_id=range_start,
            range_end_message_id=range_end,
            find_word=find_word or "",
            replace_word=replace_word or "",
            replace_word_entities=replace_word_entities,
            remove_links=job_config.remove_links_enabled,
            remove_urls=job_config.remove_urls_enabled,
            inject_text=inject_text_value,
            inject_text_entities=inject_text_entities,
            promo_phrases=promo_phrases,
            add_hyperlink_url=add_hyperlink_url,
            quote_removal_enabled=job_config.quote_removal_enabled,
            progress_callback=progress_callback,
            completion_callback=completion_callback,
        )
    except JobManagerError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await state.clear()
    await callback.answer()


# ---------------------------------------------------------------------------
# Pause / Resume / Stop
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "job:pause")
async def cb_pause_job(callback: CallbackQuery) -> None:
    job_manager = await _get_job_manager(callback)

    active = await job_manager.get_active_job()
    task_type = active.task_type if active is not None else None

    await callback.answer("⏸️ Pause signal sent. Pausing shortly...")

    try:
        await job_manager.pause_current_job()
    except JobManagerError as exc:
        await callback.message.edit_text(str(exc), reply_markup=keyboards.back_to_menu())
        return

    if task_type == TaskType.CAPTION_EDIT and active is not None:
        job = await queries.get_job(get_pool(), active.id)
        await callback.message.edit_text(
            _format_caption_paused_card(job),
            reply_markup=keyboards.job_controls(is_paused=True),
        )
    else:
        await callback.message.edit_text(
            "⏸️ Job paused. You can resume it anytime from Job Status.",
            reply_markup=keyboards.job_controls(is_paused=True),
        )


@router.callback_query(F.data == "job:resume")
async def cb_resume_job(callback: CallbackQuery) -> None:
    pool = get_pool()
    job_manager = await _get_job_manager(callback)
    active = await job_manager.get_active_job()

    if active is None or active.status != JobStatus.PAUSED:
        await callback.answer("No paused job to resume.", show_alert=True)
        return

    progress_chat_id = callback.from_user.id
    progress_message_id = callback.message.message_id
    job_start_monotonic = time.monotonic()

    async def progress_callback(snapshot: ProgressSnapshot) -> None:
        await _edit_job_message(
            callback.bot,
            progress_chat_id,
            progress_message_id,
            _format_caption_status_card(snapshot, job_start_monotonic),
            reply_markup=keyboards.job_controls(is_paused=False),
        )

    async def completion_callback(outcome: RunOutcome) -> None:
        if outcome.stop_reason == StopReason.COMPLETED:
            text = _format_caption_complete_card(outcome)
        elif outcome.stop_reason == StopReason.FAILED:
            text = _format_caption_failed_card(outcome)
        else:
            return
        await _edit_job_message(
            callback.bot,
            progress_chat_id,
            progress_message_id,
            text,
            reply_markup=keyboards.back_to_menu(),
        )

    try:
        job = await job_manager.resume_job(
            active.id,
            progress_callback=progress_callback,
            completion_callback=completion_callback,
        )
    except JobManagerError as exc:
        await callback.answer(str(exc), show_alert=True)
        return

    await callback.answer()


@router.callback_query(F.data == "job:stop")
async def cb_stop_job_prompt(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "⚠️ Stopping is permanent -- the job cannot be resumed afterward. Continue?",
        reply_markup=keyboards.confirm_stop(),
    )
    await callback.answer()


@router.callback_query(F.data == "job:stop:confirmed")
async def cb_stop_job_confirmed(callback: CallbackQuery) -> None:
    job_manager = await _get_job_manager(callback)

    active = await job_manager.get_active_job()
    task_type = active.task_type if active is not None else None

    await callback.answer("⏹️ Stop signal sent. The current task will stop shortly...")

    try:
        await job_manager.stop_current_job()
    except JobManagerError as exc:
        await callback.message.edit_text(str(exc), reply_markup=keyboards.back_to_menu())
        return

    if task_type == TaskType.CAPTION_EDIT and active is not None:
        job = await queries.get_job(get_pool(), active.id)
        await callback.message.edit_text(
            _format_caption_stopped_card(job),
            reply_markup=keyboards.back_to_menu(),
        )
    else:
        await callback.message.edit_text(
            "⏹️ Job stopped.",
            reply_markup=keyboards.back_to_menu(),
        )


# ---------------------------------------------------------------------------
# Settings (Delay)
# ---------------------------------------------------------------------------

@router.callback_query(F.data == "menu:settings")
async def cb_open_settings(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        "⚙️ Settings",
        reply_markup=keyboards.settings_menu(),
    )
    await callback.answer()


@router.callback_query(F.data == "settings:delay")
async def cb_settings_delay(callback: CallbackQuery, state: FSMContext) -> None:
    pool = get_pool()

    if await _check_no_active_task(callback):
        return

    settings = await queries.get_settings(pool)

    await state.set_state(DelaySetupStates.waiting_for_delay)
    await callback.message.edit_text(
        f"⏱️ Current delay: {settings.action_delay_seconds}s\n\n"
        "Send a new delay value between 1.0 and 3.0 seconds.",
        reply_markup=keyboards.main_menu_button(),
    )
    await callback.answer()


@router.message(DelaySetupStates.waiting_for_delay)
async def handle_delay_input(message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    try:
        value = float(raw)
    except ValueError:
        await message.answer("Please send a number between 1.0 and 3.0.", reply_markup=keyboards.main_menu_button())
        return

    if value < 1.0 or value > 3.0:
        await message.answer("Delay must be between 1.0 and 3.0 seconds.", reply_markup=keyboards.main_menu_button())
        return

    pool = get_pool()
    await queries.set_action_delay(pool, value)
    await state.set_state(None)

    await message.answer(
        f"✅ Delay set to {value}s.",
        reply_markup=keyboards.settings_menu(),
    )
