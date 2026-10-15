"""Evidence-first provider decisions: stale checks are never remediation proof."""
from __future__ import annotations

from datetime import datetime, timezone
from .store import parse_time


def verified_fresh(row: dict, now: datetime, max_age_hours: float = 24) -> bool:
    checked = parse_time(row.get('checked_at'))
    return bool(checked and row.get('probe_source') and 0 <= (now - checked).total_seconds() <= max_age_hours * 3600)


def provider_actions(providers: list[dict], now: datetime | None = None, max_age_hours: float = 24) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    actions = []
    alternatives = [p.get('name', p['platform']) for p in providers if p.get('status') == 'live' and p.get('probe_enabled', True) and verified_fresh(p, now, max_age_hours)]
    for row in providers:
        if row.get('status') == 'disabled' or not row.get('probe_enabled', True):
            continue
        name = row.get('name', row['platform'])
        if not verified_fresh(row, now, max_age_hours):
            actions.append({'title': f'Verify {name}', 'priority': 2, 'reason': 'No recent verified completion; last-known status is not current proof.', 'action': 'Re-probe locally with python main.py probe before changing this provider key.', 'url': None})
            continue
        status = row.get('status', 'unknown')
        titles = {'broken_key': f'Check {name} credentials', 'exhausted': f'Check {name} quota', 'paywalled': f'Check {name} billing', 'degraded': f'Retry {name}', 'unknown': f'Investigate {name}'}
        if status not in titles:
            continue
        advice = {
            'broken_key': 'Validate the provider-specific credential in its official dashboard and update it in FreeLLMAPI only if confirmed invalid.',
            'exhausted': 'Inspect current quota/reset headers or the official dashboard. Registry reset schedule is unverified: ' + (row.get('reset_schedule') or 'unknown') + '.',
            'paywalled': 'Inspect account/model billing requirements. Do not infer that the whole provider lost its free tier from one model.',
            'degraded': 'Wait/back off according to retry headers, then re-probe this route; do not rotate keys for throttling or server errors.',
            'unknown': 'Inspect sanitized attempt evidence and local router health/auth; a router-wide failure is not proof that every upstream key failed.',
        }[status]
        if alternatives:
            advice += ' Recently verified alternative routes: ' + ', '.join(alternatives) + '. Check model/capability and entitlement suitability before switching.'
        actions.append({'title': titles[status], 'priority': 1 if status == 'broken_key' else (2 if status in {'exhausted', 'paywalled'} else 3), 'reason': row.get('status_reason') or f'Fresh probe: {status}.', 'action': advice, 'url': None})
    return sorted(actions, key=lambda a: (a['priority'], a['title'].lower()))


def provider_changes(previous: list[dict], current: list[dict], now: datetime | None = None, max_age_hours: float = 24) -> list[dict]:
    """Only compare real probe verdicts; a DB ping/first observation isn't a recovery."""
    now = now or datetime.now(timezone.utc)
    old_by_platform = {p['platform']: p for p in previous}
    changes = []
    for row in current:
        old = old_by_platform.get(row['platform'])
        if not old or old.get('status', 'unknown') in {'unknown', 'disabled'} or not old.get('probe_source') or not parse_time(old.get('checked_at')) or not verified_fresh(row, now, max_age_hours) or old.get('status') == row.get('status'):
            continue
        status = row.get('status', 'unknown')
        if status == 'disabled':
            continue
        label = 'recovered' if status == 'live' else ('quota_exhausted' if status == 'exhausted' else ('newly_broken' if status in {'broken_key', 'paywalled', 'degraded'} else 'updated'))
        name = row.get('name', row['platform'])
        changes.append({'id': f"provider:{row['platform']}", 'headline': f"{name}: {old.get('status', 'unknown')} → {status}", 'source': 'provider-probes', 'summary': row.get('status_reason') or 'Fresh provider-specific probe verdict; inspect evidence below.', 'url': None, 'change_type': label, 'checked_at': row.get('checked_at')})
    return changes
