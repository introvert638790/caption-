"""
Job manager: orchestrates job lifecycle and enforces "only one active job
at a time" (per architecture: single-user bot, shared across Caption
Manager and Post Manager task types).

Owns the in-process asyncio.Task running the current job. Since Render's
free Web Service has no separate worker process, the task lives inside the
same process handling webhook requests -- this module is the single place
that creates/tracks/cancels that task, so bot/ handlers never touch asyncio
primitives directly.

Resume-after-restart: on startup, `recover_on_startup` checks for a job left
in RUNNING status (meaning the process died mid-run) and automatically
resumes it from its persisted cursor, regardless of task_type. A PAUSED job
is left paused; the user must explicitly resume it via the button-driven UI.
"""

from __future__ import annotations

import asyncio
import logging

import asyncpg
from aiogram import Bot

from core.job_runner import JobRunner, RunOutcome, StopReason
from db import queries
from db.models import Job, JobStatus, NewJob, TaskType

logger = logging.getLogger(__name__)


class JobManagerError(RuntimeError):
    """Raised for invalid job lifecycle operations (e.g. starting a second job)."""


class JobManager:
    """
    Single source of truth for "is a job running right now" within this
    process. Not safe for multi-process use -- fine here since Render free
    Web Service runs exactly one instance and there is no separate worker.
    """

    def __init__(self, pool: asyncpg.Pool, bot: Bot, scratch_chat_id: int) -> None:
        self._pool = pool
        self._bot = bot
        self._scratch_chat_id = scratch_chat_id
        self._current_runner: JobRunner | None = None
        self._current_task: asyncio.Task | None = None
        self._current_job_id: int | None = None

    @property
    def is_job_active(self) -> bool:
        """True if a job is currently running in-process (not just DB-persisted)."""
        return self._current_task is not None and not self._current_task.done()

    async def get_active_job(self) -> Job | None:
        """Fetch the currently active (RUNNING or PAUSED) job from the DB, if any, of either task_type."""
        return await queries.get_active_job(self._pool)

    async def _get_delay_seconds(self) -> float:
        settings = await queries.get_settings(self._pool)
        return settings.action_delay_seconds

    async def start_new_job(
        self,
        task_type: TaskType,
        channel_chat_id: int,
        range_start_message_id: int,
        range_end_message_id: int,
        find_word: str | None = None,
        replace_word: str | None = None,
        replace_word_entities: list[dict] | None = None,
        remove_links: bool = True,
        remove_urls: bool = False,
        inject_text: str | None = None,
        inject_text_entities: list[dict] | None = None,
        inject_position: str | None = None,
        quote_removal_enabled: bool = False,
        target_thread_id: int | None = None,
        progress_callback=None,
        completion_callback=None,
    ) -> Job:
        """
        Create and start a brand-new job (caption_edit or post_delete).

        Raises:
            JobManagerError: if another job is already active (RUNNING or
                PAUSED in the DB, or an in-process task is still running).
                Enforces the single-active-task invariant across BOTH task
                types; the DB has no constraint for this since "active"
                spans two status values, so it's checked here.
        """
        existing = await queries.get_active_job(self._pool)
        if existing is not None:
            raise JobManagerError(
                f"Cannot start a new job: job {existing.id} is already {existing.status.value}. "
                "Stop or resume it first."
            )
        if self.is_job_active:
            raise JobManagerError("Cannot start a new job: a job task is already running in-process.")

        total_count = range_end_message_id - range_start_message_id + 1
        new_job = NewJob(
            task_type=task_type,
            channel_chat_id=channel_chat_id,
            range_start_message_id=range_start_message_id,
            range_end_message_id=range_end_message_id,
            find_word=find_word,
            replace_word=replace_word,
            replace_word_entities=replace_word_entities,
            remove_links=remove_links,
            remove_urls=remove_urls,
            inject_text=inject_text,
            inject_text_entities=inject_text_entities,
            inject_position=inject_position,
            quote_removal_enabled=quote_removal_enabled,
            target_thread_id=target_thread_id,
        )
        job = await queries.create_job(self._pool, new_job, total_count)
        await self._launch(job, progress_callback, completion_callback)
        return job

    async def resume_job(self, job_id: int, progress_callback=None, completion_callback=None) -> Job:
        """
        Resume a job currently in PAUSED (or RUNNING, e.g. after a restart)
        status. Resumes from `job.cursor_message_id + 1`.

        Raises:
            JobManagerError: if the job doesn't exist, isn't resumable, or
                another job is already active in-process.
        """
        job = await queries.get_job(self._pool, job_id)
        if job is None:
            raise JobManagerError(f"Job {job_id} not found.")
        if job.status not in (JobStatus.PAUSED, JobStatus.RUNNING):
            raise JobManagerError(
                f"Job {job_id} is {job.status.value}; only PAUSED or RUNNING jobs can be resumed."
            )
        if self.is_job_active:
            raise JobManagerError("Cannot resume: a job task is already running in-process.")

        if job.status != JobStatus.RUNNING:
            await queries.update_job_status(self._pool, job_id, JobStatus.RUNNING)
            job = await queries.get_job(self._pool, job_id)

        await self._launch(job, progress_callback, completion_callback)
        return job

    async def pause_current_job(self) -> None:
        """
        Request the in-process runner to stop cooperatively, then relabel the
        resulting STOPPED status as PAUSED. Pausing and stopping are both
        cooperative-stop under the hood but mean different things to the UI:
        PAUSED is resumable via the same flow as an interrupted RUNNING job.
        """
        if self._current_runner is None or not self.is_job_active:
            raise JobManagerError("No job is currently active.")

        job_id = self._current_job_id
        self._current_runner.request_stop()
        await self._current_task  # wait for the loop to exit at the next safe point

        if job_id is not None:
            await queries.update_job_status(self._pool, job_id, JobStatus.PAUSED)

    async def stop_current_job(self) -> None:
        """
        Permanently stop the current job. Unlike pause, this leaves the job
        in STOPPED status (not resumable through the normal resume flow).

        A paused job has no live asyncio task (pausing already let the
        runner exit cleanly), so `is_job_active` is False for it even
        though it is still RUNNING/PAUSED in the DB. In that case there is
        nothing to cooperatively signal -- just persist STOPPED directly
        for whichever job the DB still shows as active, and clear any
        leftover in-memory references for it.
        """
        if self._current_runner is not None and self.is_job_active:
            self._current_runner.request_stop()
            await self._current_task
            # JobRunner.run already persists STOPPED status when stop_requested
            # causes early exit, so no further DB write needed here.
            return

        db_active = await queries.get_active_job(self._pool)
        if db_active is None:
            raise JobManagerError("No job is currently active.")

        await queries.update_job_status(self._pool, db_active.id, JobStatus.STOPPED)
        if self._current_job_id == db_active.id:
            self._current_runner = None
            self._current_task = None
            self._current_job_id = None

    async def recover_on_startup(self, progress_callback=None) -> Job | None:
        """
        Call once during application startup. If a job was left in RUNNING
        status (process died mid-run without a clean stop/pause), resume it
        automatically from its persisted cursor, regardless of task_type.

        A job left PAUSED is intentionally NOT auto-resumed -- pausing is a
        deliberate user action and should stay paused until the user
        explicitly resumes it via the button UI.
        """
        job = await queries.get_active_job(self._pool)
        if job is None:
            return None
        if job.status != JobStatus.RUNNING:
            logger.info(
                "Found job %s in status %s at startup; not auto-resuming.", job.id, job.status.value
            )
            return job

        logger.info(
            "Found job %s in RUNNING status at startup; auto-resuming from cursor %s.",
            job.id,
            job.cursor_message_id,
        )
        await self._launch(job, progress_callback)
        return job

    async def _launch(self, job: Job, progress_callback, completion_callback=None) -> None:
        """Create the JobRunner + asyncio.Task for the given job and track it."""
        delay_seconds = await self._get_delay_seconds()
        runner = JobRunner(
            pool=self._pool,
            bot=self._bot,
            scratch_chat_id=self._scratch_chat_id,
            delay_seconds=delay_seconds,
            progress_callback=progress_callback,
        )
        self._current_runner = runner
        self._current_job_id = job.id
        self._current_task = asyncio.create_task(self._run_and_cleanup(runner, job, completion_callback))

    async def _run_and_cleanup(self, runner: JobRunner, job: Job, completion_callback=None) -> RunOutcome:
        """
        Wraps runner.run() so unexpected exceptions are caught, logged, and
        recorded as a FAILED job status rather than crashing the process or
        leaving the job stuck in RUNNING forever.

        completion_callback, if provided, is invoked with the final
        RunOutcome only for COMPLETED or FAILED -- never for
        STOPPED_BY_USER, since the pause/stop handlers already own the
        final edit of the job message in that case.
        """
        outcome = None
        try:
            outcome = await runner.run(job)
            if outcome.stop_reason == StopReason.COMPLETED:
                logger.info("Job %s completed.", job.id)
                # Captions were modified by this run, so any cached preview
                # (even for the same channel/range/words fingerprint) is now
                # stale and must not be reused. Only relevant for caption_edit
                # jobs -- a post_delete job completing has no bearing on
                # Caption Manager's preview cache.
                if job.task_type == TaskType.CAPTION_EDIT:
                    await queries.clear_cached_preview(self._pool)
            elif outcome.stop_reason == StopReason.STOPPED_BY_USER:
                logger.info("Job %s stopped by user.", job.id)
            return outcome
        except Exception:
            logger.exception("Job %s crashed unexpectedly.", job.id)
            await queries.update_job_status(self._pool, job.id, JobStatus.FAILED)
            final_job = await queries.get_job(self._pool, job.id)
            outcome = RunOutcome(stop_reason=StopReason.FAILED, final_job=final_job)
            return outcome
        finally:
            if completion_callback is not None and outcome is not None and outcome.stop_reason in (
                StopReason.COMPLETED,
                StopReason.FAILED,
            ):
                try:
                    await completion_callback(outcome)
                except Exception:
                    logger.exception("completion_callback failed for job %s", job.id)
            self._current_runner = None
            self._current_task = None
            self._current_job_id = None
