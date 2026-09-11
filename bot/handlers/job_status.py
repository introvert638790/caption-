"""
Job status display: current/most-recent job progress and recent log entries.
"""

from __future__ import annotations

from datetime import datetime, timezone

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot import keyboards
from db import queries
from db.connection import get_pool
from db.models import JobStatus, MessageLogStatus

router = Router(name="job_status")


def _status_emoji(status: JobStatus) -> str:
    return {
        JobStatus.RUNNING: "🚀",
        JobStatus.PAUSED: "⏸️",
        JobStatus.STOPPED: "⏹️",
        JobStatus.COMPLETED: "✅",
        JobStatus.FAILED: "❌",
    }.get(status, "❔")


@router.callback_query(F.data == "menu:status")
async def cb_job_status(callback: CallbackQuery) -> None:
    pool = get_pool()
    job_manager = callback.bot.job_manager
    job = await job_manager.get_active_job()

    if job is None:
        await callback.message.edit_text(
            "No active job. Use Preview & Run to start one.",
            reply_markup=keyboards.back_to_menu(),
        )
        await callback.answer()
        return

    operation_label = "Caption Processing"
    edited_label = "Edited"

    processed = job.edited_count + job.skipped_count + job.failed_count
    remaining = max(job.total_count - processed, 0)
    elapsed_seconds = int((datetime.now(timezone.utc) - job.created_at).total_seconds())
    progress_pct = int((processed / job.total_count) * 100) if job.total_count else 0

    text_lines = [
        f"{_status_emoji(job.status)} Job {job.id} -- {job.status.value.upper()}",
        "",
        f"Current Operation: {operation_label}",
        f"Progress: {processed}/{job.total_count} ({progress_pct}%)",
        f"✅ {edited_label}: {job.edited_count}",
        f"⏭️ Skipped: {job.skipped_count}",
        f"❌ Failed: {job.failed_count}",
        f"Remaining: {remaining}",
        f"Elapsed: {elapsed_seconds}s",
        "",
    ]

    text_lines.append(f"Find: {job.find_word} \u2192 Replace: {job.replace_word}")
    text_lines.append(f"Range: {job.range_start_message_id} \u2192 {job.range_end_message_id}")

    text = "\n".join(text_lines)

    if job.status == JobStatus.PAUSED:
        markup = keyboards.job_controls(is_paused=True)
    elif job.status == JobStatus.RUNNING:
        markup = keyboards.job_controls(is_paused=False)
    else:
        markup = keyboards.back_to_menu()

    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "menu:logs:failed")
async def cb_recent_failed_logs(callback: CallbackQuery) -> None:
    pool = get_pool()
    job_manager = callback.bot.job_manager
    job = await job_manager.get_active_job()

    if job is None:
        await callback.answer("No active job.", show_alert=True)
        return

    logs = await queries.get_job_logs(pool, job.id, status_filter=MessageLogStatus.FAILED, limit=20)
    if not logs:
        await callback.answer("No failed messages logged.", show_alert=True)
        return

    lines = [f"• {log.message_id}: {log.reason or 'unknown error'}" for log in logs]
    text = "❌ Recent failures:\n\n" + "\n".join(lines)
    await callback.message.edit_text(text, reply_markup=keyboards.back_to_menu())
    await callback.answer()
