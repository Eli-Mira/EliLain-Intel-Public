"""End-to-end CLI wiring tests; source adapters are tested independently."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import main
from freellmapi_tracker.models import FreeProvider, ProviderStatus, ProviderProbeResult


def tracker_at(tmp_path, **kwargs):
    return main.NewsTracker(data_dir=tmp_path / 'data', cache_dir=tmp_path / 'cache', reports_dir=tmp_path / 'reports', freellmapi_db_path=tmp_path / 'missing.db', **kwargs)


def test_generate_report_uses_persisted_discoveries_and_original_template(tmp_path):
    tracker = tracker_at(tmp_path)
    now = datetime.now(timezone.utc)
    tracker.intel_store.update([{'id': 'github:test/repo', 'headline': 'Useful game tool', 'source': 'github-trending', 'summary': 'Game engine', 'url': 'https://github.com/test/repo', 'project_types': ['game'], 'evidence': {'periods': {'weekly': {'rank': 1}}}, 'fingerprint_data': {'summary': 'Game engine'}}], [{'name': 'github-trending', 'status': 'ok', 'checked_at': now.isoformat(), 'item_count': 1, 'url': 'https://github.com/trending'}])
    html = tracker.generate_report(include_news=False)
    assert 'Useful game tool' in html
    assert 'github-trending' in html
    assert 'game' in html
    assert 'id="changes"' in html and 'id="discoveries"' in html
    assert 'lain // tracker report' in html
    assert Path(tracker.last_report_path).read_text(encoding='utf-8') == html
    without = tracker.generate_report(include_news=False, include_tools=False)
    assert 'Useful game tool' not in without


@pytest.mark.asyncio
async def test_discovery_wires_both_source_families_persists_and_suppresses_repeats(tmp_path, monkeypatch):
    tracker = tracker_at(tmp_path)
    now = datetime.now(timezone.utc).isoformat()
    calls = []
    async def catalogs(client, **kwargs):
        calls.append(('catalogs', kwargs))
        return {'items': [{'id': 'openrouter:model:a', 'headline': 'Model A', 'source': 'openrouter-models', 'summary': 'listed', 'url': 'https://openrouter.ai/a', 'fingerprint_data': {'context': 10}}], 'sources': [{'name': 'openrouter-models', 'status': 'ok', 'checked_at': now, 'item_count': 1}]}
    async def tools(client, **kwargs):
        calls.append(('tools', kwargs))
        return {'items': [{'id': 'skills:a/b/c', 'headline': 'Skill C', 'source': 'skills-sh', 'summary': 'advisory', 'url': 'https://skills.sh/a/b/c', 'fingerprint_data': {'summary': 'advisory'}}], 'sources': [{'name': 'skills-sh', 'status': 'partial', 'checked_at': now, 'item_count': 1, 'coverage': {'scope': 'Rendered pages only'}}]}
    async def legacy():
        return []
    monkeypatch.setattr(main, 'fetch_catalogs', catalogs, raising=False)
    monkeypatch.setattr(main, 'fetch_tools', tools, raising=False)
    monkeypatch.setattr(tracker.tool_discovery, 'discover_github_tools', legacy)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    items = await tracker.discover_tools()
    assert len(items) == 2
    assert {c[0] for c in calls} == {'catalogs', 'tools'}
    assert calls[0][1]['include_local'] is False
    assert tracker.intel_store.path.exists()
    assert 'Skill C' in tracker.generate_report(include_news=False)
    assert tracker.intel_store.read()['baseline'] is True
    await tracker.discover_tools()
    assert tracker.intel_store.read()['changes'] == []
    assert tracker.intel_store.read()['baseline'] is False


def test_provider_report_cannot_render_old_live_as_current(tmp_path):
    tracker = tracker_at(tmp_path)
    now = datetime.now(timezone.utc)
    tracker.provider_tracker.providers = {
        'groq': FreeProvider(name='Groq', platform='groq', status=ProviderStatus.LIVE, checked_at=now-timedelta(days=3), probe_source='local:freellmapi', freellmapi_action='No action needed'),
        'mistral': FreeProvider(name='Mistral', platform='mistral', status=ProviderStatus.LIVE, checked_at=now, probe_source='local:freellmapi', probe_evidence=[{'model': 'mistral/test', 'http_status': 200}]),
    }
    rows = {r['platform']: r for r in tracker.provider_status_rows()}
    assert rows['groq']['status'] == 'unknown'
    assert rows['groq']['actual_status'] == 'unknown'
    assert rows['groq']['last_known_status'] == 'live'
    assert rows['groq']['freshness'] == 'stale'
    assert 'No action needed' not in rows['groq']['description']
    assert rows['mistral']['actual_status'] == 'live'
    assert rows['mistral']['evidence'][0]['http_status'] == 200
    html = tracker.generate_report(include_news=False)
    assert 'Verify Groq' in html
    assert 'Freshness:' in html and 'stale' in html
    assert 'mistral/test' in html


@pytest.mark.asyncio
async def test_probe_preserves_observation_and_persists_change_for_report_reload(tmp_path, monkeypatch):
    tracker = tracker_at(tmp_path)
    checked = datetime.now(timezone.utc)-timedelta(minutes=5)
    tracker.provider_tracker.providers['groq'] = FreeProvider(name='Groq', platform='groq', status=ProviderStatus.BROKEN_KEY, checked_at=checked-timedelta(hours=1), probe_source='local:freellmapi')
    result = ProviderProbeResult('groq', True, 'groq/test', http_status=200, checked_at=checked, attempted_models=['missing', 'groq/test'], response_category='success', evidence=[{'model': 'groq/test', 'http_status': 200, 'category': 'success'}])
    class Prober:
        def __init__(self, **kwargs):
            pass
        async def probe_all(self, providers, **kwargs):
            return {'groq': result}
    monkeypatch.setattr(main, 'AutoProber', Prober)
    assert await tracker.probe_providers() == 1
    provider = tracker.provider_tracker.providers['groq']
    assert provider.checked_at == checked
    assert provider.last_verified == checked
    assert provider.probe_source == 'local:freellmapi'
    assert provider.probe_evidence[0]['model'] == 'groq/test'
    html = tracker.generate_report(include_news=False)
    assert 'recovered' in html and 'Groq: broken_key' in html
    reloaded = tracker_at(tmp_path)
    reloaded.load_providers()
    assert 'recovered' in reloaded.generate_report(include_news=False)
    assert reloaded.provider_tracker.providers['groq'].checked_at == checked
    await tracker.probe_providers()
    assert 'Groq: broken_key' not in tracker.generate_report(include_news=False)


@pytest.mark.asyncio
async def test_ci_audit_includes_news_and_discoveries_and_returns_file_path(tmp_path, monkeypatch, capsys):
    tracker = tracker_at(tmp_path)
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    monkeypatch.setattr(tracker.sqlite, 'get_items', lambda **kw: [{'title': 'Stored AI news', 'summary': 'A real stored row', 'source': 'news', 'url': 'https://example.org/story'}])
    async def discover():
        now = datetime.now(timezone.utc).isoformat()
        tracker.intel_store.update([{'id': 'github:demo/tool', 'headline': 'Discovered tool', 'source': 'github-trending', 'summary': 'advisory', 'url': 'https://github.com/demo/tool', 'evidence': {'demo': 'usable evidence value'}, 'fingerprint_data': {'description': 'advisory'}}], [{'name': 'github-trending', 'status': 'ok', 'checked_at': now, 'item_count': 1}])
        return tracker.intel_store.read()['items']
    async def forbidden_probe():
        pytest.fail('CI must not contact the local router')
    monkeypatch.setattr(tracker, 'discover_tools', discover)
    monkeypatch.setattr(tracker, 'probe_providers', forbidden_probe)
    result = await main.async_full_audit(tracker)
    path = Path(result['report'])
    assert path.is_file()
    html = path.read_text(encoding='utf-8')
    assert 'Stored AI news' in html and 'Discovered tool' in html
    assert 'usable evidence value' in html
    assert result['providers_probed'] == 0
    assert '<!DOCTYPE' not in capsys.readouterr().out


def test_providers_cli_does_not_print_stale_live_or_old_rotation_advice(tmp_path, monkeypatch, capsys):
    import sys
    tracker = tracker_at(tmp_path)
    tracker.provider_tracker.providers['groq'] = FreeProvider(name='Groq',platform='groq',status=ProviderStatus.LIVE,checked_at=datetime.now(timezone.utc)-timedelta(days=3),probe_source='local:freellmapi',freellmapi_action='No action needed')
    monkeypatch.setattr(main,'NewsTracker',lambda **kw: tracker)
    monkeypatch.setattr(sys,'argv',['main.py','providers'])
    main.main()
    output = capsys.readouterr().out
    assert '[live' not in output
    assert 'stale' in output and 'Verify Groq' in output
    assert 'No action needed' not in output


@pytest.mark.asyncio
async def test_slow_news_source_cannot_block_other_sources(tmp_path, monkeypatch):
    import asyncio
    from tracker_core.schema import NewsItem, SourceType
    tracker = tracker_at(tmp_path, source_timeout_seconds=0.02)
    class Slow:
        name = 'slow'
        async def fetch_all(self):
            await asyncio.Event().wait()
    class Fast:
        name = 'fast'
        async def fetch_all(self):
            return [NewsItem(title='Useful AI news',url='https://example.org/news',source=SourceType.GITHUB)]
    monkeypatch.setattr(main,'get_all_adapters',lambda config=None:[Slow(),Fast()])
    rows = await asyncio.wait_for(tracker.fetch_all(),timeout=1)
    assert len(rows) == 1 and rows[0].title == 'Useful AI news'
    assert tracker.health.get_or_create('slow').consecutive_failures == 1
    assert tracker.health.get_or_create('fast').total_items == 1
