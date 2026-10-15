"""Daily automation orchestration.

This module owns the daily end-to-end flow: load settings, fetch and verify
candidates, build the digest, export a public HTML artifact and persist a
private raw evidence snapshot with an immutable bounded history.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

from . import settings as settings_module
from .daily_sources import fetch_candidates, verify_candidates
from .daily_digest import build_digest
from .daily_public import export_digest, export_state

logger = logging.getLogger(__name__)


def _ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def _atomic_write(target: Path, content: str) -> None:
    temporary = target.with_name(target.name + '.tmp')
    try:
        temporary.write_text(content, encoding='utf-8')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_json(target: Path, obj: Any) -> None:
    _atomic_write(target, json.dumps(obj, ensure_ascii=False, sort_keys=True) + '\n')


def _intended_cairo_slot(now: datetime, schedule: dict[str, Any]) -> datetime:
    """Return the most recent 10:00 Africa/Cairo slot on or before now.

    Used to decide whether a delayed run should still process the intended
    day's digest without silently dropping late jobs.
    """
    from zoneinfo import ZoneInfo
    cairo_tz = ZoneInfo('Africa/Cairo')
    # Ensure now is timezone-aware UTC
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now_utc = now.astimezone(timezone.utc)
    now_cairo = now_utc.astimezone(cairo_tz)
    hour = int(schedule.get('hour', 10))
    minute = int(schedule.get('minute', 0))
    # Most recent slot at configured time in Cairo
    slot_cairo = now_cairo.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now_cairo < slot_cairo:
        slot_cairo = slot_cairo - timedelta(days=1)
    # Return slot as timezone-aware UTC
    slot_utc = slot_cairo.astimezone(timezone.utc)
    return slot_utc


def _persist_history(state_dir: Path, digest: dict[str, Any], metadata: dict[str, Any]) -> None:
    """Persist an immutable bounded history entry."""
    history_file = state_dir / 'history.jsonl'
    entry = {
        'report_date': digest.get('report_date'),
        'generated_at': digest.get('generated_at'),
        'counts': digest.get('counts', {}),
        'metadata': metadata,
    }
    line = json.dumps(entry, ensure_ascii=False, sort_keys=True)
    # Append atomically via temp file rename to avoid interleaving (simplified)
    tmp = history_file.with_name(history_file.name + '.tmp')
    if history_file.exists():
        existing = history_file.read_text(encoding='utf-8')
        tmp.write_text(existing + line + '\n', encoding='utf-8')
    else:
        tmp.write_text(line + '\n', encoding='utf-8')
    os.replace(tmp, history_file)


def _enforce_history_limit(state_dir: Path, limit: int = 90) -> None:
    history_file = state_dir / 'history.jsonl'
    if not history_file.exists():
        return
    lines = history_file.read_text(encoding='utf-8').strip().splitlines()
    if len(lines) <= limit:
        return
    kept = lines[-limit:]
    tmp = history_file.with_name(history_file.name + '.tmp')
    tmp.write_text('\n'.join(kept) + '\n', encoding='utf-8')
    os.replace(tmp, history_file)


async def run_daily(settings_path: str | Path | None = None, data_dir: str | Path = 'data', *, public_dir: str | Path | None = None) -> dict[str, Any]:
    """Run the daily pipeline end-to-end.

    Returns a summary dict with paths and counts for verification.

    ``public_dir`` is keyword-only; when omitted defaults to ``data_dir/public``.
    The returned summary records ``settings_hash`` (deterministic) rather than
    raw settings JSON under ``settings_sha``.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    state_dir = data_dir / 'daily_state'
    _ensure_dir(state_dir)

    # Load settings
    cfg = settings_module.load_settings(settings_path)
    schedule = cfg.get('schedule', {})
    now = datetime.now(timezone.utc)

    # Intended slot check (record only; do not drop late jobs)
    intended_slot = _intended_cairo_slot(now, schedule)
    delayed = (now - intended_slot).total_seconds() > 60 * 30  # more than 30 min late

    # Fetch & verify
    fetched = await fetch_candidates(cfg, now)
    verified = await verify_candidates(fetched['candidates'], cfg, now)

    # Build digest
    collection = {'candidates': verified, 'sources': fetched['sources']}
    digest = build_digest(collection, cfg, now)

    # Public export — caller-selected public_dir is the exact producer output
    export_dir = Path(public_dir) if public_dir is not None else data_dir / 'public'
    public_path = export_digest(digest, export_dir)
    # Persist the exact artifact SHA so the nonempty gate can bind the
    # history entry to this run's HTML (stale/tampered histories fail).
    _artifact_sha256 = hashlib.sha256(public_path.read_bytes()).hexdigest()

    # Private raw evidence
    raw_evidence = {
        'candidates': verified,
        'sources': fetched['sources'],
        'settings': cfg,
        'generated_at': digest.get('generated_at'),
    }
    evidence_path = state_dir / 'raw_evidence.json'
    _atomic_json(evidence_path, raw_evidence)

    # Persist delivery continuity artifacts (only required current-state files;
    # no legacy provider/project/DB paths). delivery_receipts.json is per-run
    # confirmation state written by report_delivery; cloudflare_publication.json
    # records the last Pages verification result. These survive cache restores
    # so a restart after channel failure can resume without re-announcing.
    receipts_path = data_dir / 'delivery_receipts.json'
    if not receipts_path.exists():
        _atomic_json(receipts_path, {"artifacts": {}})
    cf_path = data_dir / 'cloudflare_publication.json'
    _atomic_json(cf_path, {"publication": "cloudflare-pages", "status": "pending",
                           "checked_at": now.isoformat(), "url": "", "sha256": _artifact_sha256})

    # History — bounded real history with actual report counts, no false receipts
    settings_hash = hashlib.sha256(json.dumps(cfg, sort_keys=True).encode('utf-8')).hexdigest()
    metadata = {
        'delayed_start': delayed,
        'intended_slot': intended_slot.isoformat(),
        'settings_hash': settings_hash,
        'artifact_sha256': _artifact_sha256,
        'public_path': str(public_path),
        'rendered_at': digest.get('generated_at'),
    }
    _persist_history(state_dir, digest, metadata)
    _enforce_history_limit(state_dir, limit=90)

    summary = {
        'report_date': digest.get('report_date'),
        'generated_at': digest.get('generated_at'),
        'public_path': str(public_path),
        'evidence_path': str(evidence_path),
        'counts': digest.get('counts', {}),
        'delayed_start': delayed,
        'intended_slot': intended_slot.isoformat(),
        'settings_hash': settings_hash,
    }
    logger.info(f"Daily run complete: {summary}")
    return summary
