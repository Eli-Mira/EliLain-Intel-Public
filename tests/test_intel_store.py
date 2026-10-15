from datetime import datetime, timezone
import importlib

NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc)


def test_first_snapshot_is_baseline_and_retains_full_inventory(tmp_path):
    module = importlib.import_module('intel_sources.store')
    store = module.IntelStore(tmp_path / 'intel.json')
    item = {'id': 'catalog:a', 'source': 'catalog', 'headline': 'Model A', 'summary': 'observed', 'url': 'https://example.com/a', 'fingerprint_data': {'context': 100}}
    result = store.update([item], [{'name': 'catalog', 'status': 'ok', 'checked_at': NOW.isoformat(), 'item_count': 1}], now=NOW)
    assert result['baseline'] is True
    assert result['changes'] == []
    assert result['items'][0]['first_seen'] == NOW.isoformat()
    assert result['items'][0]['freshness'] == 'fresh'
    assert module.IntelStore(tmp_path / 'intel.json').read(now=NOW)['items'] == result['items']


def test_repeated_items_are_not_new_and_meaningful_updates_are_reported(tmp_path):
    from intel_sources.store import IntelStore
    store = IntelStore(tmp_path / 'intel.json')
    item = {'id': 'catalog:a', 'source': 'catalog', 'headline': 'A', 'summary': 'text', 'url': 'https://example.com', 'fingerprint_data': {'context': 100}}
    sources = [{'name': 'catalog', 'status': 'ok', 'checked_at': NOW.isoformat()}]
    store.update([item], sources, now=NOW)
    repeated = store.update([dict(item, stars=999)], sources, now=NOW)
    assert repeated['baseline'] is False
    assert repeated['changes'] == []
    updated = store.update([dict(item, fingerprint_data={'context': 200}), dict(item, id='catalog:b')], sources, now=NOW)
    assert {c['change_type'] for c in updated['changes']} == {'updated', 'newly_observed'}
    assert updated['items'][0]['first_seen'] == NOW.isoformat()


def test_source_failure_keeps_previous_inventory_but_not_freshness(tmp_path):
    from intel_sources.store import IntelStore
    store = IntelStore(tmp_path / 'intel.json')
    item = {'id': 'catalog:a', 'source': 'catalog', 'headline': 'A', 'fingerprint_data': {'id': 'a'}}
    store.update([item], [{'name': 'catalog', 'status': 'ok', 'checked_at': NOW.isoformat()}], now=NOW)
    failed = store.update([], [{'name': 'catalog', 'status': 'error', 'checked_at': NOW.isoformat(), 'error': '429'}], now=NOW)
    assert len(failed['items']) == 1
    assert failed['items'][0]['freshness'] == 'stale'
    assert failed['changes'] == []


def test_duplicate_sources_are_merged_without_losing_period_evidence(tmp_path):
    from intel_sources.store import IntelStore
    store = IntelStore(tmp_path / 'intel.json')
    base = {'id': 'repo:a', 'source': 'github', 'headline': 'A', 'fingerprint_data': {'description': 'tool'}}
    result = store.update([dict(base, project_types=['web'], evidence={'daily': 1}), dict(base, project_types=['data'], evidence={'weekly': 2})], [{'name': 'github', 'status': 'ok', 'checked_at': NOW.isoformat()}], now=NOW)
    assert len(result['items']) == 1
    assert set(result['items'][0]['project_types']) == {'web', 'data'}
    assert result['items'][0]['evidence'] == {'daily': 1, 'weekly': 2}


def test_unobserved_cached_item_is_stale_and_corrupt_state_is_not_overwritten(tmp_path):
    import pytest
    from intel_sources.store import IntelStore
    path = tmp_path / 'intel.json'
    store = IntelStore(path)
    a = {'id': 'catalog:a', 'source': 'catalog', 'headline': 'A', 'fingerprint_data': {'id': 'a'}}
    sources = [{'name': 'catalog', 'status': 'ok', 'checked_at': NOW.isoformat()}]
    store.update([a], sources, now=NOW)
    result = store.update([], sources, now=NOW)
    assert result['items'][0]['freshness'] == 'stale'
    path.write_text('{bad json', encoding='utf-8')
    with pytest.raises(ValueError):
        store.update([a], sources, now=NOW)
    assert path.read_text(encoding='utf-8') == '{bad json'


def test_failed_atomic_replace_preserves_last_good_snapshot(tmp_path, monkeypatch):
    import os
    import pytest
    from intel_sources.store import IntelStore
    path = tmp_path / 'intel.json'
    store = IntelStore(path)
    sources = [{'name': 'catalog', 'status': 'ok', 'checked_at': NOW.isoformat()}]
    a = {'id': 'catalog:a', 'source': 'catalog', 'headline': 'A', 'fingerprint_data': {'id': 'a'}}
    store.update([a], sources, now=NOW)
    before = path.read_bytes()
    def failed_replace(*args):
        raise OSError('simulated locked target')
    monkeypatch.setattr(os, 'replace', failed_replace)
    with pytest.raises(OSError):
        store.update([dict(a, headline='B')], sources, now=NOW)
    assert path.read_bytes() == before
