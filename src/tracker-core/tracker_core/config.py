from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel


class CronConfig(BaseModel):
    hour: int = 0
    minute: int = 0
    day_of_week: str = "*"
    day_of_month: str = "*"
    month: str = "*"


class TrackerSchedule(BaseModel):
    mode: str = "cron"  # "cron" or "interval"
    cron: CronConfig | None = None
    seconds: int | None = None  # For interval mode


class AlertThresholds(BaseModel):
    consecutive_failures: int = 3
    check_interval_minutes: int = 10


class ScheduleConfig(BaseModel):
    api_tracker: TrackerSchedule
    news_tracker: TrackerSchedule
    alerts: AlertThresholds = AlertThresholds()


def load_schedule(path: Path | str = "schedule.yaml") -> ScheduleConfig:
    """Load schedule config from YAML file."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Schedule file not found: {path}")
    
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    
    return ScheduleConfig(**data)


def get_trigger(schedule: TrackerSchedule):
    """Return APScheduler trigger based on schedule config."""
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger
    
    if schedule.mode == "interval" and schedule.seconds:
        return IntervalTrigger(seconds=schedule.seconds)
    
    if schedule.mode == "cron" and schedule.cron:
        return CronTrigger(
            hour=schedule.cron.hour,
            minute=schedule.cron.minute,
            day_of_week=schedule.cron.day_of_week,
        )
    
    raise ValueError(f"Invalid schedule configuration: {schedule}")
