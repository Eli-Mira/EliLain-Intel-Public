"""Generic example scope excludes inactive examples from active recommendations."""
from datetime import datetime, timezone

from intel_sources.recommendations import evaluate_projects
from intel_sources.public_report import build_public_payload
from tracker_core.reporter_html import LainReporter


def test_inactive_arcade_example_never_receives_active_report_recommendations():
    now = datetime(2026, 10, 1, 5, tzinfo=timezone.utc)
    item = {
        'id': 'skills:obra/superpowers/test-driven-development',
        'headline': 'test-driven-development',
        'source': 'skills-sh',
        'url': 'https://skills.sh/obra/superpowers/test-driven-development',
        'checked_at': now.isoformat(),
        'evidence': {},
    }
    evaluated = evaluate_projects([item], now=now)
    assert set(evaluated['groups']) == {'desktop-app', 'vision-app', 'data-ai'}
    assert 'arcade-game' not in evaluated['groups']
    state = {'items': [item], 'sources': [
        {'name': 'skills-sh', 'status': 'ok', 'checked_at': now.isoformat()}
    ]}
    public = build_public_payload(state, now=now)
    assert 'recommendation_groups' not in public
    html = LainReporter().render(**public, public_export=True)
    assert 'desktop-app' not in html and 'vision-app' not in html
    assert 'data-project="arcade-game"' not in html
    assert 'Model fruit lifecycle' not in html


def test_report_persists_the_exact_current_evaluation_for_restart(tmp_path, monkeypatch):
    import json
    import main
    monkeypatch.setenv('NOTIFICATION_CHANNEL', 'none')
    tracker = main.NewsTracker(data_dir=tmp_path / 'data', cache_dir=tmp_path / 'cache',
        reports_dir=tmp_path / 'reports', freellmapi_db_path=tmp_path / 'missing.db')
    now = datetime.now(timezone.utc).isoformat()
    tracker.intel_store.update([
        {'id': 'skills:obra/superpowers/test-driven-development', 'headline': 'TDD skill',
         'source': 'skills-sh', 'url': 'https://skills.sh/obra/superpowers/test-driven-development',
         'evidence': {}, 'summary': 'advisory', 'fingerprint_data': {'summary': 'advisory'}}
    ], [{'name': 'skills-sh', 'status': 'ok', 'checked_at': now, 'item_count': 1}])
    html = tracker.generate_report(include_news=False, include_providers=False)
    artifact_path = tracker.data_dir / 'project_evaluations.json'
    assert artifact_path.exists(), 'Report evaluation is discarded instead of updating its saved JSON'
    artifact = json.loads(artifact_path.read_text(encoding='utf-8'))
    snapshot = artifact['discovery_snapshot']
    evaluated_at = datetime.fromisoformat(artifact['result']['evaluated_at'])
    assert evaluate_projects(snapshot, now=evaluated_at) == artifact['result']
    assert set(artifact['result']['groups']) == {'desktop-app', 'vision-app', 'data-ai'}
    assert snapshot == tracker.intel_store.read()['items']
    assert artifact['provenance']['source_inventory_count'] == len(snapshot)
    assert len(artifact['provenance']['input_state_sha256']) == 64
    assert 'Decimal totals' in html
    assert not list(tracker.data_dir.glob('*.tmp'))


def test_first_report_without_prior_discovery_saves_an_explicit_empty_evaluation(tmp_path, monkeypatch):
    import json
    import main
    monkeypatch.setenv('NOTIFICATION_CHANNEL', 'none')
    tracker = main.NewsTracker(data_dir=tmp_path / 'data', cache_dir=tmp_path / 'cache',
        reports_dir=tmp_path / 'reports', freellmapi_db_path=tmp_path / 'missing.db')
    html = tracker.generate_report(include_news=False, include_providers=False)
    artifact = json.loads((tracker.data_dir / 'project_evaluations.json').read_text(encoding='utf-8'))
    assert artifact['discovery_snapshot'] == []
    assert artifact['result']['inventory_count'] == 0
    assert len(artifact['provenance']['input_state_sha256']) == 64
    assert tracker.last_report_path.exists()
    assert 'arcade-game' not in html
