"""Schedule config for news tracker — with Cairo timezone awareness."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "tracker-core" / "src"))

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from tracker_core.scheduler import TrackerScheduler
from tracker_core.config import load_schedule


CAIRO_TZ = ZoneInfo("Africa/Cairo")


def is_cairo_morning(hour: int) -> bool:
    """Check if current Cairo time is around morning (7am ±1hr)."""
    cairo_now = datetime.now(CAIRO_TZ)
    return cairo_now.hour in (hour - 1, hour, hour + 1)


def should_run_news_tracker(config_path: str | None = None) -> bool:
    """Determine if news tracker should run now (Cairo morning check)."""
    if config_path is None:
        config_path = str(Path(__file__).parent.parent / "tracker-core" / "schedule.yaml")
    try:
        config = load_schedule(config_path)
        target_hour = config.news_tracker.cron.hour if config.news_tracker.cron else 7
        return is_cairo_morning(target_hour)
    except Exception:
        return False


def create_news_scheduler(config_path: str | None = None):
    """Create scheduler for news tracker with Cairo timezone awareness."""
    if config_path is None:
        config_path = str(Path(__file__).parent.parent / "tracker-core" / "schedule.yaml")

    config = load_schedule(config_path)
    scheduler = TrackerScheduler(config_path)

    cairo_now = datetime.now(CAIRO_TZ)
    print(f"News tracker scheduler created (Cairo: {cairo_now.strftime('%Y-%m-%d %H:%M %Z')})")

    return scheduler, config.news_tracker


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)

    scheduler, schedule = create_news_scheduler()
    print(f"News tracker schedule: {schedule.mode}")
    if schedule.mode == "cron":
        print(f"  Cron: {schedule.cron.hour}:{schedule.cron.minute} daily")
    elif schedule.mode == "interval":
        print(f"  Interval: every {schedule.seconds} seconds")

    print(f"\nCurrent Cairo time: {datetime.now(CAIRO_TZ).strftime('%Y-%m-%d %H:%M %Z')}")
    print(f"Should run now: {should_run_news_tracker()}")
