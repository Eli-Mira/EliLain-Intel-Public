"""Tests for the daily automation module."""
import asyncio
from datetime import datetime, timezone, timedelta
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from intel_sources import settings
from intel_sources.daily_automation import run_daily, _intended_cairo_slot


def test_default_settings_load():
    s = settings.default_settings()
    assert s['schema_version'] == 1
    assert s['schedule']['timezone'] == 'Africa/Cairo'
    assert s['schedule']['hour'] == 10
    assert s['schedule']['minute'] == 0
    assert len(s['sources']) >= 1


def test_intended_slot_does_not_drop_late_job():
    sched = {'hour': 10, 'minute': 0}
    # 11:30 UTC approximates late run
    now = datetime(2026, 10, 7, 11, 30, tzinfo=timezone.utc)
    slot = _intended_cairo_slot(now, sched)
    # Slot should be today at 10:00, not tomorrow or dropped
    assert slot.hour == 7  # 10:00 Cairo = 07:00 UTC (DST +3)
    assert slot.minute == 0
    # Ensure we still return a slot even though it's late
    delta = now - slot
    assert delta.total_seconds() > 0


def test_intended_cairo_slot_uses_zoneinfo():
    from zoneinfo import ZoneInfo
    sched = {'hour': 10, 'minute': 0}
    # 2026-10-07 07:00 UTC is 10:00 Cairo (DST +3)
    now = datetime(2026, 10, 7, 7, 0, tzinfo=timezone.utc)
    slot = _intended_cairo_slot(now, sched)
    # At exact slot time, slot should equal now
    assert slot == now
    # Verify ZoneInfo is used by checking DST transition
    # Egypt DST ends last Friday of April, so in April 2026 DST still active
    # 2026-04-30 is DST start? We'll test a date in winter (UTC+2)
    now_winter = datetime(2026, 1, 15, 8, 0, tzinfo=timezone.utc)  # 10:00 Cairo = 08:00 UTC
    slot_winter = _intended_cairo_slot(now_winter, sched)
    assert slot_winter.hour == 8
    # DST summer
    now_summer = datetime(2026, 7, 15, 7, 0, tzinfo=timezone.utc)  # 10:00 Cairo = 07:00 UTC
    slot_summer = _intended_cairo_slot(now_summer, sched)
    assert slot_summer.hour == 7
    # Ensure zoneinfo aware
    assert slot.tzinfo is not None


def test_intended_slot_handles_dst_transition():
    from zoneinfo import ZoneInfo
    sched = {'hour': 10, 'minute': 0}
    tz = ZoneInfo('Africa/Cairo')
    # Pick a date around DST change (Egypt changes DST)
    # Test before slot, should return previous day
    now_before = datetime(2026, 1, 15, 5, 0, tzinfo=timezone.utc)  # 07:00 Cairo, before 10:00
    slot_before = _intended_cairo_slot(now_before, sched)
    cairo_now = now_before.astimezone(tz)
    cairo_slot = slot_before.astimezone(tz)
    assert cairo_slot.hour == 10
    assert cairo_slot <= cairo_now
    # Return in UTC
    assert slot_before.tzinfo == timezone.utc


def test_run_daily_creates_artifacts(tmp_path_factory):
    # Use a temporary data dir
    data_dir = Path(tmp_path_factory.mktemp('daily'))
    async def _run():
        res = await run_daily(settings_path=None, data_dir=data_dir)
        return res
    res = asyncio.run(_run())
    assert res['report_date'] is not None
    assert Path(res['public_path']).exists()
    assert Path(res['evidence_path']).exists()
    assert res['counts']['fetched'] >= 0
    # History file exists
    hist = data_dir / 'daily_state' / 'history.jsonl'
    assert hist.exists()
    # Delayed flag is bool
    assert isinstance(res['delayed_start'], bool)


def test_link_verification_shape():
    from intel_sources.daily_sources import fetch_candidates, verify_candidates
    cfg = settings.default_settings()
    fetched = asyncio.run(fetch_candidates(cfg))
    verified = asyncio.run(verify_candidates(fetched['candidates'], cfg))
    for cand in verified:
        lv = cand.get('link_verification', {})
        assert 'status' in lv
        assert 'checked_at' in lv
        assert 'url' in lv
        assert 'http_status' in lv
