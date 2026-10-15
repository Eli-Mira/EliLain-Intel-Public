"""Scheduler module for running tracker jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .config import load_schedule, ScheduleConfig, get_trigger


logger = logging.getLogger(__name__)


class TrackerScheduler:
    """Manages scheduled runs for tracker tools."""
    
    def __init__(self, config_path: str = "schedule.yaml"):
        self.config = load_schedule(config_path)
        self.scheduler = AsyncIOScheduler()
        self._jobs: dict[str, Any] = {}
        self._run_count: dict[str, int] = {}
        self._consecutive_failures: dict[str, int] = {}
    
    def add_job(
        self,
        job_id: str,
        func: Callable,
        schedule: str | None = None,
        trigger=None,
        **kwargs
    ) -> None:
        """Add a job to the scheduler."""
        if job_id in self._jobs:
            self.remove_job(job_id)
        
        if trigger is None and schedule:
            trigger = self._parse_schedule(schedule)
        
        job = self.scheduler.add_job(
            func,
            trigger=trigger,
            id=job_id,
            kwargs=kwargs,
            replace_existing=True
        )
        
        self._jobs[job_id] = job
        self._run_count[job_id] = 0
        self._consecutive_failures[job_id] = 0
        logger.info(f"Added job: {job_id}")
    
    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID."""
        if job_id in self._jobs:
            self.scheduler.remove_job(job_id)
            del self._jobs[job_id]
            del self._run_count[job_id]
            del self._consecutive_failures[job_id]
            return True
        return False
    
    def start(self) -> None:
        """Start the scheduler."""
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started")
    
    def shutdown(self, wait: bool = True) -> None:
        """Shutdown the scheduler."""
        if self.scheduler.running:
            self.scheduler.shutdown(wait=wait)
            logger.info("Scheduler shutdown")
    
    def run_now(self, job_id: str) -> None:
        """Trigger a job immediately."""
        if job_id in self._jobs:
            self._jobs[job_id].trigger_job()
    
    def get_status(self) -> dict[str, Any]:
        """Get scheduler status."""
        return {
            "running": self.scheduler.running,
            "jobs": {
                job_id: {
                    "next_run": str(job.next_run_time) if job.next_run_time else None,
                    "run_count": self._run_count.get(job_id, 0),
                    "consecutive_failures": self._consecutive_failures.get(job_id, 0),
                }
                for job_id, job in self._jobs.items()
            }
        }
    
    def record_success(self, job_id: str) -> None:
        """Record successful job run."""
        self._run_count[job_id] = self._run_count.get(job_id, 0) + 1
        self._consecutive_failures[job_id] = 0
    
    def record_failure(self, job_id: str) -> None:
        """Record failed job run."""
        failures = self._consecutive_failures.get(job_id, 0) + 1
        self._consecutive_failures[job_id] = failures
        
        threshold = self.config.alerts.consecutive_failures
        if failures >= threshold:
            logger.warning(f"Job {job_id} failed {failures} times consecutively")
    
    def _parse_schedule(self, schedule: str) -> Any:
        """Parse schedule string to trigger."""
        # Format: "cron:hour=2,minute=0" or "interval:seconds=86400"
        if schedule.startswith("cron:"):
            parts = schedule[5:].split(",")
            kwargs = {}
            for part in parts:
                key, value = part.split("=")
                kwargs[key] = int(value)
            return CronTrigger(**kwargs)
        elif schedule.startswith("interval:"):
            seconds = int(schedule[9:])
            return IntervalTrigger(seconds=seconds)
        else:
            raise ValueError(f"Invalid schedule format: {schedule}")
    
    async def _wrapped_job(self, job_id: str, func: Callable, *args, **kwargs) -> None:
        """Wrapper to track job execution."""
        try:
            await func(*args, **kwargs)
            self.record_success(job_id)
        except Exception as e:
            self.record_failure(job_id)
            logger.error(f"Job {job_id} failed: {e}", exc_info=True)
            raise


def create_scheduler(config_path: str = "schedule.yaml") -> TrackerScheduler:
    """Factory function to create a scheduler from config."""
    return TrackerScheduler(config_path)
