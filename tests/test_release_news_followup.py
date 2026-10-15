"""Customer acceptance regressions; offline only, no inference or real state."""

import json
from datetime import datetime, timezone

import httpx
import pytest

from news_tracker.summarizer import Summarizer, summarize_news
from news_tracker.adapters import HackerNewsAdapter, GitHubAdapter, RedditAdapter
from news_tracker.health import SourceHealthTracker
from tracker_core.pipeline.fetcher import FetchResult
from tracker_core.schema import NewsItem, SourceType


def news(title="Observed AI announcement", summary=None):
    return NewsItem(source=SourceType.NEWS, title=title,
                    url="https://example.org/news/" + title.replace(" ", "-"), summary=summary)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [
    {"summary": "Fabricated result", "dedup_key": "fake"},
    "Fabricated result",
    [{"summary": "Fabricated result", "dedup_key": "fake"}],
    [{"summary": 123, "dedup_key": "fake"}, {"summary": "Other", "dedup_key": "other"}],
    [{"summary": "Fabricated result", "dedup_key": "fake"}, {}],
    [{"summary": "Fabricated result", "dedup_key": []}, {"summary": "Other", "dedup_key": "other"}],
    [{"summary": "   ", "dedup_key": "fake"}, {"summary": "Other", "dedup_key": "other"}],
])
async def test_invalid_configured_batch_preserves_all_observations(monkeypatch, payload):
    async def response(self, prompt):
        return json.dumps(payload)
    monkeypatch.setattr(Summarizer, "_call_llm", response)
    rows = [news(summary="Observed publisher excerpt"), news("Second AI announcement")]
    result = await summarize_news(rows, {"api_key": "fixture"})
    assert result is rows
    assert rows[0].summary == "Observed publisher excerpt"
    assert rows[1].summary == "Title excerpt (not AI summary): Second AI announcement"
    assert all(row.dedup_key is None for row in rows)


@pytest.mark.asyncio
async def test_raw_lines_never_become_unlabelled_summaries(monkeypatch, caplog):
    async def response(self, prompt):
        return "private-canary-untrusted-line\nThis is not validated JSON"
    monkeypatch.setattr(Summarizer, "_call_llm", response)
    rows = [news(summary="Observed publisher excerpt"), news("Second AI announcement")]
    await summarize_news(rows, {"api_key": "fixture"})
    assert rows[0].summary == "Observed publisher excerpt"
    assert rows[1].summary.startswith("Title excerpt (not AI summary): ")
    assert "private-canary" not in caplog.text


@pytest.mark.asyncio
async def test_configured_failure_logs_no_upstream_message(monkeypatch, caplog):
    async def response(self, prompt):
        raise RuntimeError("https://private-host/?token=private-canary")
    monkeypatch.setattr(Summarizer, "_call_llm", response)
    await summarize_news([news()], {"api_key": "fixture"})
    assert "private-canary" not in caplog.text
    assert "private-host" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [None, 123, {}, [], "   "])
async def test_api_nontext_content_rejected_safely(monkeypatch, content):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "choices": [{"message": {"content": content}}]}))
    monkeypatch.setattr("news_tracker.summarizer.httpx.AsyncClient",
                        lambda **kw: original(transport=transport, **kw))
    with pytest.raises(RuntimeError, match="AI summarization unavailable"):
        await Summarizer({"base_url": "https://example.org/v1", "api_key": "fixture"})._call_llm("fixture")


@pytest.mark.asyncio
async def test_complete_valid_batch_uses_only_validated_text(monkeypatch):
    async def response(self, prompt):
        return json.dumps([{"summary": "Validated first", "dedup_key": "first"},
                           {"summary": "Validated second", "dedup_key": "second"}])
    monkeypatch.setattr(Summarizer, "_call_llm", response)
    rows = [news(), news("Second AI announcement")]
    await summarize_news(rows, {"api_key": "fixture"})
    assert [(row.summary, row.dedup_key) for row in rows] == [
        ("Validated first", "first"), ("Validated second", "second")]


@pytest.mark.asyncio
async def test_invalid_batch_stops_before_more_requests(monkeypatch):
    calls = []
    async def response(self, prompt):
        calls.append(prompt)
        return "[]"
    monkeypatch.setattr(Summarizer, "_call_llm", response)
    rows = [news(f"AI observed title {i}") for i in range(11)]
    await summarize_news(rows, {"api_key": "fixture"})
    assert len(calls) == 1
    assert all(row.summary.startswith("Title excerpt (not AI summary): ") for row in rows)


@pytest.mark.asyncio
async def test_hn_requests_chronological_news_and_keeps_publication_time(monkeypatch):
    adapter = HackerNewsAdapter({"feed_health": SourceHealthTracker()})
    urls = []
    published = 1704067200
    async def fetch(url, **kw):
        urls.append(url)
        return FetchResult(json.dumps({"hits": [{"title": "Observed AI story", "url": "https://example.org/story",
                           "created_at_i": published}]}), {}, 200, url, 0, True)
    monkeypatch.setattr(adapter.fetcher, "fetch", fetch)
    rows = await adapter.fetch_all()
    assert len(urls) == len(adapter.KEYWORDS)
    assert all("/api/v1/search_by_date?" in url for url in urls)
    assert all("tags=story" in url and "hitsPerPage=10" in url for url in urls)
    assert rows[0].published_date == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert rows[0].fetched_date.replace(tzinfo=timezone.utc) > rows[0].published_date


@pytest.mark.asyncio
async def test_reddit_authenticated_timestamp_and_excerpt_survive_parse():
    item = RedditAdapter({"feed_health": SourceHealthTracker()}).parse_item({
        "title": "Observed AI discussion", "url": "https://reddit.com/r/MachineLearning/fixture",
        "published_date": datetime(2024, 1, 1, tzinfo=timezone.utc),
        "body": "Observed author excerpt", "subreddit": "MachineLearning"})
    assert item.published_date == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert item.summary == "Observed author excerpt"


@pytest.mark.asyncio
async def test_github_release_rate_limit_enforces_feed_cooldown(monkeypatch):
    adapter = GitHubAdapter({"feed_health": SourceHealthTracker(), "orgs": ["fixture-org"]})
    release_calls = []
    async def fetch(url, **kw):
        if "/releases/latest" in url:
            release_calls.append(url)
            return FetchResult("", {"Retry-After": "120"}, 429, url, 0, False)
        body = [{"full_name": "fixture-org/repo"}] if "/orgs/" in url else {"items": []}
        return FetchResult(json.dumps(body), {}, 200, url, 0, True)
    monkeypatch.setattr(adapter.fetcher, "fetch", fetch)
    await adapter.fetch_all()
    release_key = adapter._feed_key("https://api.github.com/repos/fixture-org/repo/releases/latest")
    assert adapter.feed_results[release_key]["error"] == "http_429"
    await adapter.fetch_all()
    assert len(release_calls) == 1
    assert adapter.feed_results[release_key]["status"] == "cooldown"


def test_github_keeps_publisher_release_excerpt_with_old_publication():
    item = GitHubAdapter({"feed_health": SourceHealthTracker()}).parse_item({
        "title": "Observed release", "url": "https://github.com/fixture/repo/releases/tag/old",
        "published_date": "2021-01-01T00:00:00Z", "body": "Observed publisher release notes"})
    assert item.summary == "Observed publisher release notes"
    assert item.published_date == datetime(2021, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
@pytest.mark.parametrize("credentials", [
    {"reddit_client_id": "fixture"},
    {"reddit_client_secret": "private-canary"},
    {"reddit_client_id": "fixture", "reddit_client_secret": "private-canary"},
])
async def test_configured_reddit_never_silently_downgrades_to_anonymous(monkeypatch, credentials, caplog):
    import sys
    monkeypatch.setitem(sys.modules, "praw", None)
    adapter = RedditAdapter({"feed_health": SourceHealthTracker(), **credentials})
    calls = []
    async def fetch(url, **kw):
        calls.append(url)
        return FetchResult('<rss version="2.0"><channel/></rss>', {}, 200, url, 0, True)
    monkeypatch.setattr(adapter.fetcher, "fetch", fetch)
    assert await adapter.fetch_all() == []
    assert calls == []
    assert len(adapter.feed_results) == len(adapter.SUBREDDITS)
    assert all(row["status"] == "failed" for row in adapter.feed_results.values())
    assert "private-canary" not in caplog.text + str(adapter.feed_results)


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_sibling", [False, True])
async def test_invalid_feed_items_do_not_claim_complete_healthy_coverage(monkeypatch, valid_sibling):
    from news_tracker.adapters import RSSFeedAdapter
    adapter = RSSFeedAdapter({"feed_health": SourceHealthTracker(), "feeds": [("fixture", "https://example.org/feed")]})
    invalid = '<item><title>private-canary-title</title><link>not-a-url</link></item>'
    valid = '<item><title>Observed story</title><link>https://example.org/story</link></item>' if valid_sibling else ""
    async def fetch(url, **kw):
        return FetchResult('<rss version="2.0"><channel>' + invalid + valid + '</channel></rss>', {}, 200, url, 0, True)
    monkeypatch.setattr(adapter.fetcher, "fetch", fetch)
    rows = await adapter.fetch_all()
    assert len(rows) == int(valid_sibling)
    result = next(iter(adapter.feed_results.values()))
    assert result["status"] == ("partial" if valid_sibling else "failed")
    assert result["items"] == int(valid_sibling)
    assert "private-canary" not in str(adapter.feed_results)


@pytest.mark.asyncio
async def test_repository_update_is_not_claimed_as_publication(monkeypatch):
    adapter = GitHubAdapter({"feed_health": SourceHealthTracker(), "orgs": []})
    async def fetch(url, **kw):
        return FetchResult(json.dumps({"items": [{"full_name": "fixture/repo", "description": "Observed repository description",
                         "html_url": "https://github.com/fixture/repo", "updated_at": "2026-01-01T00:00:00Z"}]}), {}, 200, url, 0, True)
    monkeypatch.setattr(adapter.fetcher, "fetch", fetch)
    rows = await adapter.fetch_all()
    assert rows[0].published_date is None
    assert rows[0].raw_data["item"]["raw"]["updated_at"] == "2026-01-01T00:00:00Z"
    assert rows[0].summary == "Observed repository description"


@pytest.mark.asyncio
@pytest.mark.parametrize("choice", [
    {"finish_reason": "length", "message": {"content": '[]'}},
    {"finish_reason": "content_filter", "message": {"content": '[]'}},
    {"finish_reason": "stop", "message": {"refusal": "private-canary", "content": '[]'}},
])
async def test_incomplete_or_refused_api_response_is_not_accepted(monkeypatch, choice):
    original = httpx.AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"choices": [choice]}))
    monkeypatch.setattr("news_tracker.summarizer.httpx.AsyncClient",
                        lambda **kw: original(transport=transport, **kw))
    with pytest.raises(RuntimeError, match="AI summarization unavailable"):
        await Summarizer({"base_url": "https://example.org/v1", "api_key": "fixture"})._call_llm("fixture")
