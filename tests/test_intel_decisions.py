from datetime import datetime, timezone
import importlib

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def test_stale_broken_key_requires_verification_before_key_rotation():
    decisions = importlib.import_module('intel_sources.decisions')
    row = {'platform': 'groq', 'name': 'Groq', 'status': 'broken_key', 'checked_at': '2026-09-28T12:00:00+00:00', 'probe_source': 'local:freellmapi', 'probe_enabled': True}
    actions = decisions.provider_actions([row], now=NOW)
    assert len(actions) == 1
    assert actions[0]['title'] == 'Verify Groq'
    assert 're-probe' in actions[0]['action'].lower()
    assert 'fresh key' not in actions[0]['action'].lower()


def test_fresh_auth_failure_is_first_and_only_fresh_alternative_is_suggested():
    from intel_sources.decisions import provider_actions
    rows = [
        {'platform': 'groq', 'name': 'Groq', 'status': 'broken_key', 'checked_at': NOW.isoformat(), 'probe_source': 'local:freellmapi'},
        {'platform': 'mistral', 'name': 'Mistral', 'status': 'live', 'checked_at': NOW.isoformat(), 'probe_source': 'local:freellmapi'},
        {'platform': 'google', 'name': 'Google', 'status': 'live', 'checked_at': '2026-09-20T12:00:00+00:00', 'probe_source': 'local:freellmapi'},
        {'platform': 'cohere', 'name': 'Cohere', 'status': 'exhausted', 'checked_at': NOW.isoformat(), 'probe_source': 'local:freellmapi', 'reset_schedule': 'Monthly'},
    ]
    actions = provider_actions(rows, now=NOW)
    assert actions[0]['title'] == 'Check Groq credentials'
    assert actions[0]['priority'] == 1
    assert 'Mistral' in actions[0]['action']
    assert 'Google' not in actions[0]['action']
    quota = next(a for a in actions if a['title'] == 'Check Cohere quota')
    assert 'unverified' in quota['action'].lower()
    assert all(a['title'] != 'Verify Mistral' for a in actions)


def test_provider_changes_only_report_new_fresh_probe_verdicts():
    from intel_sources import decisions
    old = {'platform': 'groq', 'name': 'Groq', 'status': 'broken_key', 'checked_at': '2026-09-29T12:00:00+00:00', 'probe_source': 'local:freellmapi'}
    new = dict(old, status='live', checked_at=NOW.isoformat())
    changes = decisions.provider_changes([old], [new], now=NOW)
    assert changes[0]['change_type'] == 'recovered'
    assert changes[0]['headline'] == 'Groq: broken_key → live'
    assert decisions.provider_changes([old], [old], now=NOW) == []
    assert decisions.provider_changes([], [new], now=NOW) == []
    assert decisions.provider_changes([old], [dict(new, checked_at='2026-09-20T12:00:00+00:00')], now=NOW) == []
    exhausted = dict(new, status='exhausted')
    assert decisions.provider_changes([new], [exhausted], now=NOW)[0]['change_type'] == 'quota_exhausted'


def test_unknown_or_unverified_history_is_not_a_provider_recovery():
    from intel_sources.decisions import provider_changes
    new = {'platform':'groq','name':'Groq','status':'live','checked_at':NOW.isoformat(),'probe_source':'local:freellmapi'}
    for old in [dict(new,status='unknown'), {'platform':'groq','status':'broken_key','checked_at':None,'probe_source':''}]:
        assert provider_changes([old],[new],now=NOW) == []
