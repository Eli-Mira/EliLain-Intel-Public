"""Deterministic individual-feed regressions; no network or real state writes."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import httpx

from news_tracker.health import SourceHealthTracker
from news_tracker.adapters import RSSFeedAdapter, RedditAdapter, HackerNewsAdapter, ArXivAdapter, HuggingFaceAdapter, YouTubeAdapter, GitHubAdapter
from tracker_core.pipeline.fetcher import FetchResult

RSS = '<rss version="2.0"><channel><title>Fixture</title><item><title>Survivor</title><link>https://example.org/story</link></item></channel></rss>'


def response(url, content=RSS, status=200):
    return FetchResult(content, {}, status, url, 0, status == 200)


@pytest.mark.asyncio
async def test_rss_timeout_isolated_and_success_survives():
    health = SourceHealthTracker()
    adapter = RSSFeedAdapter({'feeds': [('blocked', 'https://example.org/blocked'), ('good', 'https://example.org/good')], 'feed_health': health, 'feed_timeout_seconds': 0.02, 'feed_budget_seconds': 0.1})
    cancelled = asyncio.Event()

    async def fetch(url, **kwargs):
        if url.endswith('/blocked'):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        return response(url)

    adapter.fetcher.fetch = fetch
    items = await asyncio.wait_for(adapter.fetch(), 0.2)
    assert [item['title'] for item in items] == ['Survivor']
    assert cancelled.is_set()
    rows = {row['source']: row for row in health.get_health_table()}
    assert rows[adapter._feed_key('https://example.org/blocked')]['last_error'] == 'timeout'
    assert rows[adapter._feed_key('https://example.org/good')]['status'] == 'healthy'


@pytest.mark.asyncio
@pytest.mark.parametrize('status,content,error', [(403, RSS, 'http_403'), (429, RSS, 'http_429'), (200, '<html>Blocked</html>', 'invalid_feed'), (200, '<rss><channel>', 'invalid_feed')])
async def test_rss_failure_is_truthful_and_sibling_survives(status, content, error):
    health = SourceHealthTracker()
    adapter = RSSFeedAdapter({'feed_health': health, 'feeds': [('bad', 'https://example.org/bad'), ('good', 'https://example.org/good')]})

    async def fetch(url, **kwargs):
        return response(url, content, status) if url.endswith('/bad') else response(url)

    adapter.fetcher.fetch = fetch
    items = await adapter.fetch()
    assert len(items) == 1
    bad = health.get_or_create(adapter._feed_key('https://example.org/bad'))
    assert bad.consecutive_failures == 1
    assert bad.last_error == error
    assert bad.last_success is None


@pytest.mark.asyncio
async def test_actual_http_429_is_preserved_and_retry_after_cools_only_feed(monkeypatch):
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    health = SourceHealthTracker()
    health._now = lambda: now
    adapter = RSSFeedAdapter({'feed_health': health, 'feeds': [('limited', 'https://example.org/limited'), ('good', 'https://example.org/good')], 'rate_limit_delay': 0, 'feed_timeout_seconds': 0.02})
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(429, headers={'Retry-After': '120'}, text='limited') if request.url.path == '/limited' else httpx.Response(200, text=RSS)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=httpx.MockTransport(handle), **kw))
    assert len(await adapter.fetch()) == 1
    limited = health.get_or_create(adapter._feed_key('https://example.org/limited'))
    assert limited.last_error == 'http_429'
    assert limited.cooldown_until == now + timedelta(seconds=120)
    assert not health.is_source_enabled(limited.name)
    assert len(await adapter.fetch()) == 1
    assert calls.count('https://example.org/limited') == 1
    assert calls.count('https://example.org/good') == 2


@pytest.mark.asyncio
async def test_reddit_rss_subreddit_timeout_isolated():
    adapter = RedditAdapter({'feed_health': SourceHealthTracker(), 'feed_timeout_seconds': 0.02, 'feed_budget_seconds': 0.1})
    adapter.SUBREDDITS = ['blocked', 'good']

    async def fetch(url, **kwargs):
        if '/blocked/' in url:
            await asyncio.Event().wait()
        return response(url)

    adapter.fetcher.fetch = fetch
    assert len(await asyncio.wait_for(adapter.fetch(), 0.2)) == 1
    assert sorted(row['status'] for row in adapter.feed_results.values()) == ['failed', 'healthy']


@pytest.mark.asyncio
async def test_praw_subreddit_timeout_isolated():
    import threading
    from types import SimpleNamespace
    release = threading.Event()
    adapter = RedditAdapter({'feed_health': SourceHealthTracker(), 'feed_timeout_seconds': 0.02, 'feed_budget_seconds': 0.1})
    adapter.SUBREDDITS = ['blocked', 'good']
    post = SimpleNamespace(title='Survivor', permalink='/r/good/1', created_utc=1, selftext='', author=None, score=1, num_comments=0, id='1')

    class Sub:
        def __init__(self, name):
            self.name = name

        def hot(self, limit):
            if self.name == 'blocked':
                release.wait(1)
            return [post]

    adapter._use_praw = True
    adapter._praw_instance = SimpleNamespace(subreddit=Sub)
    try:
        items = await asyncio.wait_for(adapter.fetch(), 0.2)
        assert [item['subreddit'] for item in items] == ['good']
        assert sorted(row['status'] for row in adapter.feed_results.values()) == ['failed', 'healthy']
    finally:
        release.set()


@pytest.mark.asyncio
@pytest.mark.parametrize('kind', ['hackernews', 'arxiv', 'huggingface', 'youtube', 'github'])
async def test_other_adapters_preserve_sibling_on_timeout(kind):
    cls = {'hackernews': HackerNewsAdapter, 'arxiv': ArXivAdapter, 'huggingface': HuggingFaceAdapter, 'youtube': YouTubeAdapter, 'github': GitHubAdapter}[kind]
    adapter = cls({'feed_health': SourceHealthTracker(), 'feed_timeout_seconds': 0.02, 'feed_budget_seconds': 0.1, 'orgs': ['good']})
    adapter.KEYWORDS = ['blocked', 'good']
    adapter.CATEGORIES = ['blocked', 'good']
    adapter.CHANNELS = [('blocked', 'blocked'), ('good', 'good')]

    async def fetch(url, **kwargs):
        if 'blocked' in url or (kind == 'huggingface' and '/models?' in url) or (kind == 'github' and '/search/' in url):
            await asyncio.Event().wait()
        if kind == 'hackernews':
            return response(url, '{"hits": [{"title": "Survivor", "url": "https://example.org/story"}]}')
        if kind == 'github':
            if '/orgs/' in url:
                return response(url, '[{"full_name":"good/repo"}]')
            return response(url, '{"name":"Survivor", "html_url":"https://example.org/story"}')
        if kind == 'huggingface':
            return response(url, '<a href="/papers/2601.12345">Paper</a>')
        return response(url)

    adapter.fetcher.fetch = fetch
    items = await asyncio.wait_for(adapter.fetch(), 0.2)
    assert len(items) == 1
    assert any(row['status'] == 'failed' for row in adapter.feed_results.values())
    assert any(row['status'] == 'healthy' for row in adapter.feed_results.values())


@pytest.mark.asyncio
async def test_youtube_http_failure_is_not_reported_healthy_or_timeout():
    adapter = YouTubeAdapter({'feed_health': SourceHealthTracker(), 'feed_timeout_seconds': 0.02})
    adapter.CHANNELS = [('blocked', 'blocked'), ('good', 'good')]

    async def fetch(url, **kwargs):
        return response(url, RSS, 403 if 'blocked' in url else 200)

    adapter.fetcher.fetch = fetch
    assert len(await adapter.fetch()) == 1
    record = adapter.feed_health.get_or_create(adapter._feed_key('channel:blocked'))
    assert record.last_error == 'http_403'
    assert record.last_success is None


@pytest.mark.parametrize('name,url', [('OpenAI Blog', 'https://openai.com/news/rss.xml'), ('Google AI Blog', 'https://research.google/blog/rss/'), ('Mistral Blog', 'https://mistral.ai/news/rss')])
def test_official_rss_canonical_endpoints(name, url):
    assert dict(RSSFeedAdapter.FEEDS)[name] == url


@pytest.mark.parametrize('name,channel', [('AI Explained', 'UCNJ1Ymd5yFuUPtn21xtRbbw'), ('Two Minute Papers', 'UCbfYPyITQ-7l4upoX8nvctg'), ('Yannic Kilcher', 'UCZHmQk67mSJgfCCTn7xBfew')])
def test_official_youtube_channel_ids(name, channel):
    assert dict(YouTubeAdapter.CHANNELS)[name] == channel


@pytest.mark.asyncio
async def test_feed_cooldown_persists_across_new_adapter_instances(tmp_path):
    state_path = str(tmp_path / 'feeds.json')
    config = {'feed_health_state_path': state_path, 'feeds': [('private', 'https://example.org/private?key=not-a-real-secret')]}
    adapter = RSSFeedAdapter(config)
    adapter.feed_health.failure_threshold = 1

    async def fail(url, **kwargs):
        return response(url, RSS, 403)

    adapter.fetcher.fetch = fail
    await adapter.fetch()
    next_adapter = RSSFeedAdapter(config)
    calls = []

    async def forbidden(url, **kwargs):
        calls.append(url)
        return response(url)

    next_adapter.fetcher.fetch = forbidden
    assert await next_adapter.fetch() == []
    assert calls == []
    assert list(next_adapter.feed_results.values())[0]['status'] == 'cooldown'
    text = (tmp_path / 'feeds.json').read_text()
    assert 'not-a-real-secret' not in text
    assert 'example.org' not in text
    assert 'http_403' in text


@pytest.mark.asyncio
async def test_youtube_api_failure_is_truthful_without_key_in_diagnostics(caplog):
    adapter = YouTubeAdapter({'feed_health': SourceHealthTracker(), 'youtube_api_key': 'not-a-real-secret', 'feed_timeout_seconds': 0.1})
    adapter.CHANNELS = [('bad', 'bad')]

    async def fetch(url, **kwargs):
        return response(url, 'denied', 401 if 'googleapis.com' in url else 403)

    adapter.fetcher.fetch = fetch
    assert await adapter.fetch() == []
    record = adapter.feed_health.get_or_create(adapter._feed_key('channel:bad'))
    assert record.last_error == 'http_401'
    assert 'not-a-real-secret' not in caplog.text
    assert 'not-a-real-secret' not in str(adapter.feed_results)


@pytest.mark.asyncio
async def test_praw_workers_use_separate_bounded_clients(monkeypatch):
    import sys
    import threading
    from types import SimpleNamespace
    barrier = threading.Barrier(2)
    used = []
    options = []

    class Client:
        def __init__(self, **kwargs):
            options.append(kwargs)

        def subreddit(self, name):
            return SimpleNamespace(hot=lambda limit: self.hot(name))

        def hot(self, name):
            used.append(id(self))
            barrier.wait(timeout=0.5)
            return []

        def close(self):
            pass

    monkeypatch.setitem(sys.modules, 'praw', SimpleNamespace(Reddit=Client))
    adapter = RedditAdapter({'feed_health': SourceHealthTracker(), 'reddit_client_id': 'fixture-id', 'reddit_client_secret': 'fixture-secret', 'feed_timeout_seconds': 0.2})
    adapter.SUBREDDITS = ['one', 'two']
    assert await adapter.fetch() == []
    assert len(set(used)) == 2
    assert all(option['requestor_kwargs']['timeout'] <= 0.2 for option in options)


@pytest.mark.asyncio
async def test_http_response_size_is_bounded_without_hiding_sibling(monkeypatch):
    adapter = RSSFeedAdapter({'feed_health': SourceHealthTracker(), 'feeds': [('large', 'https://example.org/large'), ('small', 'https://example.org/small')], 'rate_limit_delay': 0})
    adapter.fetcher.max_response_bytes = 64
    large = RSS.replace('Survivor', 'x' * 200)
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=large if request.url.path == '/large' else '<rss version="2.0"><channel/></rss>'))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    assert await adapter.fetch() == []
    bad = adapter.feed_health.get_or_create(adapter._feed_key('https://example.org/large'))
    good = adapter.feed_health.get_or_create(adapter._feed_key('https://example.org/small'))
    assert bad.last_error == 'response_too_large'
    assert good.is_healthy
    assert good.total_items == 0


@pytest.mark.asyncio
async def test_concurrent_http_requests_serialize_host_throttle(monkeypatch):
    adapter = RSSFeedAdapter({'feed_health': SourceHealthTracker(), 'feeds': [(str(i), f'https://example.org/{i}') for i in range(4)], 'rate_limit_delay': 0})
    active = 0
    peak = 0

    async def throttle(domain):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1

    adapter.fetcher._maybe_throttle = throttle
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=RSS))
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kw: original(transport=transport, **kw))
    assert len(await adapter.fetch()) == 4
    assert peak == 1


@pytest.mark.asyncio
async def test_factory_adapters_share_persisted_feed_health(tmp_path):
    from news_tracker.adapters import get_all_adapters
    adapters = get_all_adapters({'feed_health_state_path': str(tmp_path / 'feeds.json')})
    assert all(adapter.feed_health is adapters[0].feed_health for adapter in adapters)
    adapters[0].feed_health.record_failure('github:fixture', 'http_403')
    adapters[1].feed_health.record_success('reddit:fixture', 1)
    adapters[1].feed_health.save_state()
    restored = SourceHealthTracker(state_path=str(tmp_path / 'feeds.json'))
    restored.load_state()
    assert {row['source'] for row in restored.get_health_table()} == {'github:fixture', 'reddit:fixture'}


@pytest.mark.asyncio
async def test_youtube_api_key_not_in_request_urls_or_health():
    adapter = YouTubeAdapter({'feed_health': SourceHealthTracker(), 'youtube_api_key': 'fixture-key'})
    adapter.CHANNELS = [('good', 'good')]
    requests = []

    async def fetch(url, **kwargs):
        requests.append((url, kwargs))
        if 'youtube.com/feeds' in url:
            return response(url, '', 403)
        if '/channels?' in url:
            return response(url, '{"items":[{"contentDetails":{"relatedPlaylists":{"uploads":"fixture-list"}}}]}')
        return response(url, '{"items":[{"snippet":{"title":"Survivor","resourceId":{"videoId":"fixture-video"}}}]}')

    adapter.fetcher.fetch = fetch
    assert len(await adapter.fetch()) == 1
    api = [(url, kw) for url, kw in requests if 'googleapis.com' in url]
    assert len(api) == 2
    assert all('fixture-key' not in url for url, _ in api)
    assert all(kw['headers']['X-Goog-Api-Key'] == 'fixture-key' for _, kw in api)
    assert 'fixture-key' not in str(adapter.feed_results)


@pytest.mark.asyncio
async def test_exhausted_collection_budget_never_starts_feed_or_counts_failure():
    adapter = RSSFeedAdapter({'feed_health': SourceHealthTracker(), 'feeds': [('good', 'https://example.org/good')], 'feed_budget_seconds': 0})
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return response(url)

    adapter.fetcher.fetch = fetch
    assert await adapter.fetch() == []
    assert calls == []
    assert list(adapter.feed_results.values()) == [{'status': 'not_attempted', 'items': 0}]
    assert all(row['consecutive_failures'] == 0 for row in adapter.feed_health.get_health_table())


@pytest.mark.asyncio
async def test_item_parse_diagnostics_do_not_expose_private_values(caplog):
    adapter = RSSFeedAdapter({'feed_health': SourceHealthTracker(), 'feeds': [('fixture', 'https://example.org/fixture')]})

    async def fetch(url, **kwargs):
        return response(url)

    def parse(raw):
        raise ValueError('private-destination-fixture')

    adapter.fetcher.fetch = fetch
    adapter.parse_item = parse
    assert await adapter.fetch_all() == []
    assert 'private-destination-fixture' not in caplog.text
    assert 'ValueError' in caplog.text


def test_health_failure_is_not_healthy_and_cooldown_expires():
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    health = SourceHealthTracker(failure_threshold=2)
    health._now = lambda: now
    health.cooldown_seconds = 60
    health.record_failure('rss:test', 'http_404')
    row = health.get_health_table()[0]
    assert row['status'] == 'failed'
    assert row['last_error'] == 'http_404'
    assert health.is_source_enabled('rss:test')
    health.record_failure('rss:test', 'timeout')
    assert not health.is_source_enabled('rss:test')
    assert health.get_health_table()[0]['cooldown_until'] == (now + timedelta(seconds=60)).isoformat()
    now += timedelta(seconds=61)
    assert health.is_source_enabled('rss:test')
    assert health.get_health_table()[0]['status'] == 'failed'
    health.record_success('rss:test', 0)
    row = health.get_health_table()[0]
    assert row['status'] == 'healthy'
    assert row['last_error'] is None
    assert row['cooldown_until'] is None
