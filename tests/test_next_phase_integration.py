"""Parent integration gates for the approved next phase."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import pytest
import main
from tracker_core.schema import NewsItem, SourceType


def tracker_at(tmp_path):
    return main.NewsTracker(data_dir=tmp_path / 'data', cache_dir=tmp_path / 'cache',
        reports_dir=tmp_path / 'reports', freellmapi_db_path=tmp_path / 'missing.db')


def test_legacy_disabled_aggregate_is_retryable_and_retains_dates(tmp_path):
    tracker = tracker_at(tmp_path)
    now = datetime.now(timezone.utc)
    tracker.sqlite.save_source_health('rss', {
        'is_disabled': True, 'consecutive_failures': 3, 'total_items': 7,
        'last_success': (now - timedelta(days=2)).isoformat(),
        'last_failure': (now - timedelta(hours=1)).isoformat(),
    })
    reloaded = tracker_at(tmp_path)
    record = reloaded.health.get_or_create('rss')
    assert reloaded.health.is_source_enabled('rss'), 'Legacy boolean cannot disable a source forever'
    assert record.last_success == now - timedelta(days=2)
    assert record.last_failure == now - timedelta(hours=1)
    assert record.total_items == 7


@pytest.mark.asyncio
async def test_fetch_wires_persisted_subfeed_health_and_surfaces_partial_success(tmp_path, monkeypatch):
    tracker = tracker_at(tmp_path)
    configs = []
    class Partial:
        name = 'rss'
        feed_results = {}
        async def fetch_all(self):
            self.feed_results = {
                'rss:good-hash': {'status': 'healthy', 'items': 1},
                'rss:bad-hash': {'status': 'failed', 'items': 0, 'error': 'http_404'},
            }
            self.feed_health.record_success('rss:good-hash', 1)
            self.feed_health.record_failure('rss:bad-hash', 'http_404')
            self.feed_health.save_state()
            return [NewsItem(title='Actual fixture news', url='https://example.org/news', source=SourceType.NEWS)]
    adapter = Partial()
    def factory(config=None):
        assert config and config['feed_health_state_path'] == str(tracker.data_dir / 'feed_health.json')
        configs.append(config)
        adapter.feed_health = config['feed_health']
        return [adapter]
    monkeypatch.setattr(main, 'get_all_adapters', factory)
    assert len(await tracker.fetch_all()) == 1
    assert len(configs) == 1
    assert tracker.get_health_table()[0]['status'] == 'partial'
    assert tracker.get_health_table()[0]['feed_status_counts'] == {'healthy': 1, 'failed': 1}
    assert {row['source'] for row in tracker.get_feed_health_table()} == {'rss:good-hash', 'rss:bad-hash'}
    reloaded = tracker_at(tmp_path)
    assert reloaded.feed_health.get_or_create('rss:bad-hash').consecutive_failures == 1
    assert reloaded.health.get_or_create('rss').last_success is not None


@pytest.mark.asyncio
async def test_optional_feed_state_write_failure_cannot_discard_success(tmp_path, monkeypatch, caplog):
    from news_tracker.adapters import RSSFeedAdapter
    adapter = RSSFeedAdapter({'feeds': []})
    async def success():
        # A row that cannot become a NewsItem (no http(s) url) is rejected by
        # per-row validation before persistence, so use a complete observed row.
        return [{'title': 'Observed item', 'url': 'https://example.org/observed-item'}]
    def unavailable_state():
        raise OSError('private-path-and-token-must-not-be-logged')
    monkeypatch.setattr(adapter.feed_health, 'save_state', unavailable_state)
    rows = await adapter._collect_feeds([('https://example.org/rss', success)])
    assert rows == [{'title': 'Observed item', 'url': 'https://example.org/observed-item'}]
    assert 'private-path-and-token' not in caplog.text
    assert 'persistence' in caplog.text.lower()


def _seed_discoveries(tracker):
    now = datetime.now(timezone.utc).isoformat()
    tracker.intel_store.update(
        [{'id': 'skills:obra/superpowers/test-driven-development', 'headline': 'TDD skill',
          'source': 'skills-sh', 'summary': 'advisory', 'url': 'https://skills.sh/obra/superpowers/test-driven-development',
          'project_types': ['devops'], 'fingerprint_data': {'summary': 'advisory'}}],
        [{'name': 'skills-sh', 'status': 'ok', 'checked_at': now, 'item_count': 1}])


def test_report_renders_evaluated_project_shortlists_with_coverage(tmp_path):
    tracker = tracker_at(tmp_path)
    _seed_discoveries(tracker)
    html = tracker.generate_report(include_news=False)
    assert 'id="project-shortlists"' in html
    assert 'test-driven-development' in html
    assert 'metadata-only' in html
    assert '1 reviewed / 1 discovery candidates' in html
    assert 'id="project-shortlists"' not in tracker.generate_report(include_news=False, include_tools=False)


def test_export_public_cli_writes_only_sanitized_index(tmp_path, monkeypatch, capsys):
    import sys
    tracker = tracker_at(tmp_path)
    _seed_discoveries(tracker)
    monkeypatch.setattr(main, 'NewsTracker', lambda **kw: tracker)
    # tracker_at() puts saved state under <tmp>/data, so the CLI must read there.
    monkeypatch.setattr(sys, 'argv', ['main.py', 'export-public', '--data-dir', str(tmp_path / 'data'),
                                   '--public-dir', str(tmp_path / 'public')])
    assert main.main() == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert Path(payload['artifact']).name == 'index.html'
    assert [entry.name for entry in (tmp_path / 'public').iterdir()] == ['index.html']
    body = Path(payload['artifact']).read_text(encoding='utf-8')
    assert 'Public sanitized export' in body
    assert str(tmp_path) not in body


def test_publish_cli_fails_closed_without_delivery_config(tmp_path, monkeypatch, capsys):
    import sys
    tracker = tracker_at(tmp_path)
    _seed_discoveries(tracker)
    monkeypatch.setattr(main, 'NewsTracker', lambda **kw: tracker)
    monkeypatch.setattr(sys, 'argv', ['main.py', 'publish', '--data-dir', str(tmp_path / 'data'),
                                   '--public-dir', str(tmp_path / 'public')])
    for name in ('NETLIFY_AUTH_TOKEN', 'NETLIFY_SITE_ID', 'TELEGRAM_BOT_TOKEN', 'TELEGRAM_CHAT_ID',
                 'DISCORD_BOT_TOKEN', 'DISCORD_CHANNEL_ID'):
        monkeypatch.delenv(name, raising=False)
    assert main.main() == 1
    output = capsys.readouterr()
    assert 'missing_config' in (output.out + output.err)
    # The export step legitimately reports its own artifact path on stdout; the
    # delivery failure must not echo paths or tokens on the failure channel.
    assert 'missing_config' in output.err
    assert str(tmp_path) not in output.err
