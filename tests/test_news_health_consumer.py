"""BUG9 vertical consumer regressions: real pipeline, isolated state, offline HTTP."""
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from bs4 import BeautifulSoup

import main
from news_tracker.adapters import RSSFeedAdapter


@pytest.mark.asyncio
async def test_run_report_exposes_failed_feed_beside_healthy_empty_feed(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    tracker = main.NewsTracker(
        data_dir=tmp_path / "data", cache_dir=tmp_path / "cache",
        reports_dir=tmp_path / "reports", summarizer_config={},
        freellmapi_db_path=tmp_path / "absent-provider.db",
    )
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
    tracker.health._now = tracker.feed_health._now = lambda: now
    adapter = RSSFeedAdapter({
        "feed_health": tracker.feed_health, "rate_limit_delay": 0,
        "feeds": [("empty", "https://example.org/empty"),
                  ("blocked", "https://example.org/blocked")],
    })
    monkeypatch.setattr(main, "get_all_adapters", lambda config: [adapter])
    original_client = httpx.AsyncClient
    calls = []

    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/blocked":
            return httpx.Response(403, text="fixture-private-error-never-render")
        assert request.url.path == "/empty"
        return httpx.Response(200, text='<rss version="2.0"><channel><title>Empty</title></channel></rss>')

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(
        transport=httpx.MockTransport(respond), **kw))
    try:
        result = await tracker.run(date=now, push_breaking=False)
        assert result["total_fetched"] == 0
        assert sorted(calls) == ["/blocked", "/empty"]
        empty_key = adapter._feed_key("https://example.org/empty")
        blocked_key = adapter._feed_key("https://example.org/blocked")
        assert tracker.feed_results["rss"][empty_key] == {"status": "healthy", "items": 0}
        assert tracker.feed_results["rss"][blocked_key]["status"] == "failed"
        assert tracker.feed_health.get_or_create(empty_key).last_success == now
        assert tracker.feed_health.get_or_create(blocked_key).last_failure == now
        output = tmp_path / "reports" / "actual.html"
        html = tracker.generate_report(output_path=output, include_providers=False, include_tools=False)
        assert output.read_text(encoding="utf-8") == html
        soup = BeautifulSoup(html, "html.parser")
        section = soup.select_one("#news-feed-health")
        assert section is not None, "Actual run outcomes never reach the internal news-health consumer"
        text = section.get_text(" ", strip=True)
        assert blocked_key in text and empty_key in text
        assert "failed" in text.lower() and "healthy" in text.lower()
        assert "fixture-private-error-never-render" not in html
        assert "https://example.org/blocked" not in html
    finally:
        tracker.sqlite.close()


NOW = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)
EMPTY_RSS = '<rss version="2.0"><channel><title>Fixture</title></channel></rss>'
ONE_RSS = '<rss version="2.0"><channel><title>Fixture</title><item><title>OpenAI local LLM model release</title><link>https://example.org/story</link></item></channel></rss>'


@pytest.fixture
def trackers(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    opened = []

    def make(theme="lain", **kwargs):
        tracker = main.NewsTracker(
            data_dir=tmp_path / "data", cache_dir=tmp_path / "cache",
            reports_dir=tmp_path / "reports", summarizer_config={}, theme=theme,
            freellmapi_db_path=tmp_path / "absent-provider.db", **kwargs,
        )
        tracker.health._now = tracker.feed_health._now = lambda: NOW
        opened.append(tracker)
        return tracker

    yield make
    for tracker in opened:
        tracker.sqlite.close()


def install_rss(monkeypatch, tracker, responses, **kwargs):
    adapter = RSSFeedAdapter({
        "feed_health": tracker.feed_health, "rate_limit_delay": 0,
        "feeds": [(path, "https://example.org" + path) for path in responses], **kwargs,
    })
    monkeypatch.setattr(main, "get_all_adapters", lambda config: [adapter])
    original_client = httpx.AsyncClient
    calls = []

    def respond(request):
        calls.append(request.url.path)
        status, content, headers = responses[request.url.path]
        return httpx.Response(status, text=content, headers=headers)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original_client(
        transport=httpx.MockTransport(respond), **kw))
    return adapter, calls


def report(tracker, **kwargs):
    return tracker.generate_report(include_providers=False, include_tools=False, **kwargs)


def health_row(html, identity):
    return BeautifulSoup(html, "html.parser").find("li", attrs={"data-news-identity": identity})


@pytest.mark.asyncio
async def test_restart_renders_history_without_claiming_current_health(trackers, monkeypatch):
    tracker = trackers()
    adapter, calls = install_rss(monkeypatch, tracker, {"/one": (200, ONE_RSS, {})})
    for _ in range(2):
        assert (await tracker.run(date=NOW, push_breaking=False))["total_fetched"] == 1
    key = adapter._feed_key("https://example.org/one")
    assert tracker.sqlite.count_items(days=1) == 1
    assert tracker.feed_health.get_or_create(key).total_items == 2
    restart = trackers()
    assert restart.feed_results == {}
    assert restart.feed_health.get_or_create(key).total_items == 2
    html = report(restart, output_path=restart.reports_dir / "restart-history.html")
    row = health_row(html, key)
    assert row is not None, "Restart history vanished instead of being labelled separately"
    text = row.get_text(" ", strip=True)
    assert "Current collection: unknown" in text
    assert "Items in current outcome: unknown" in text
    assert "cumulative items from successful observations: 2" in text
    assert "not unique retained news or current-run count" in text
    assert NOW.isoformat() in text
    assert health_row(html, "rss") is not None
    assert calls == ["/one", "/one"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_sibling", [False, True], ids=["all-partial", "partial-plus-failed"])
async def test_partial_items_survive_in_source_consumer(trackers, monkeypatch, failed_sibling):
    tracker = trackers()
    partial = ONE_RSS.replace('</channel>', '<item><title>Invalid URL item</title><link>not-a-url</link></item></channel>')
    responses = {"/partial": (200, partial, {})}
    if failed_sibling:
        responses["/blocked"] = (403, "fixture-private-error", {})
    adapter, _ = install_rss(monkeypatch, tracker, responses)
    result = await tracker.run(date=NOW, push_breaking=False)
    key = adapter._feed_key("https://example.org/partial")
    assert result["total_fetched"] == 1
    assert tracker.sqlite.count_items(days=1) == 1
    assert tracker.feed_results["rss"][key]["status"] == "partial"
    assert tracker.feed_results["rss"][key]["items"] == 1
    assert tracker.feed_health.get_or_create(key).total_items == 0
    html = report(tracker, output_path=tracker.reports_dir / "partial-current.html")
    row = health_row(html, "rss")
    assert row is not None
    assert "Current collection: partial" in row.get_text(" ", strip=True)
    assert "Items in current outcome: 1" in row.get_text(" ", strip=True)
    assert "Current collection: partial" in health_row(html, key).get_text(" ", strip=True)
    assert "cumulative items from successful observations: 0" in health_row(html, key).get_text(" ", strip=True)
    assert next(r for r in result["health_table"] if r["source"] == "rss")["status"] == "partial"


@pytest.mark.asyncio
async def test_cooldown_history_skip_expiry_and_observed_recovery_are_distinct(trackers, monkeypatch):
    tracker = trackers()
    responses = {"/rate": (429, "fixture-private-error", {"Retry-After": "300"})}
    adapter, calls = install_rss(monkeypatch, tracker, responses)
    key = adapter._feed_key("https://example.org/rate")
    await tracker.run(date=NOW, push_breaking=False)
    first = health_row(report(tracker, output_path=tracker.reports_dir / "cooldown-first.html"), key).get_text(" ", strip=True)
    assert "Current collection: failed" in first
    assert "History cooldown: pending" in first
    assert (NOW + timedelta(seconds=300)).isoformat() in first
    await tracker.run(date=NOW, push_breaking=False)
    skip = health_row(report(tracker, output_path=tracker.reports_dir / "cooldown-skip.html"), key).get_text(" ", strip=True)
    assert "Current collection: cooldown" in skip
    assert "Items in current outcome: 0" in skip
    assert "History cooldown: pending" in skip
    assert calls == ["/rate"]
    restart = trackers()
    before = health_row(report(restart, output_path=restart.reports_dir / "cooldown-restart.html"), key).get_text(" ", strip=True)
    assert "Current collection: unknown" in before
    assert "History cooldown: pending" in before
    later = NOW + timedelta(seconds=301)
    restart.health._now = restart.feed_health._now = lambda: later
    expired = health_row(report(restart, output_path=restart.reports_dir / "cooldown-expired.html"), key).get_text(" ", strip=True)
    assert "Current collection: unknown" in expired
    assert "History cooldown: expired — recovery not observed" in expired
    # Keep the same offline adapter/transport, but bind the restored tracker.
    adapter.feed_health = restart.feed_health
    responses["/rate"] = (200, ONE_RSS, {})
    assert (await restart.run(date=later, push_breaking=False))["total_fetched"] == 1
    recovered = health_row(report(restart, output_path=restart.reports_dir / "cooldown-recovered.html"), key).get_text(" ", strip=True)
    assert "Current collection: healthy" in recovered
    assert "Items in current outcome: 1" in recovered
    assert "History cooldown: none recorded" in recovered
    assert "cumulative items from successful observations: 1" in recovered
    assert later.isoformat() in recovered
    assert calls == ["/rate", "/rate"]


@pytest.mark.asyncio
async def test_source_timeout_has_unknown_feed_outcome_without_invented_zero(trackers, monkeypatch):
    import asyncio

    tracker = trackers()
    adapter, calls = install_rss(monkeypatch, tracker, {"/one": (200, ONE_RSS, {})})
    key = adapter._feed_key("https://example.org/one")
    assert (await tracker.run(date=NOW, push_breaking=False))["total_fetched"] == 1
    assert calls == ["/one"]
    tracker.source_timeout_seconds = 0.01

    async def blocked():
        # Current run never reaches a feed observation; do not replay the old adapter result.
        await asyncio.Event().wait()

    monkeypatch.setattr(adapter, "fetch_all", blocked)
    await tracker.run(date=NOW, push_breaking=False)
    assert calls == ["/one"]
    html = report(tracker)
    source = health_row(html, "rss").get_text(" ", strip=True)
    assert "Current collection: failed" in source
    assert "Items in current outcome: unknown" in source
    feed = health_row(html, key).get_text(" ", strip=True)
    assert "Current collection: unknown" in feed
    assert "Items in current outcome: unknown" in feed
    assert "cumulative items from successful observations: 1" in feed


@pytest.mark.asyncio
@pytest.mark.parametrize("theme", ["lain", "default"])
async def test_restored_health_only_renders_allowlisted_identities_and_failure_codes(trackers, monkeypatch, theme):
    import json

    tracker = trackers(theme)
    adapter, _ = install_rss(monkeypatch, tracker, {"/blocked": (403, "fixture-private-canary", {})})
    await tracker.run(date=NOW, push_breaking=False)
    key = adapter._feed_key("https://example.org/blocked")
    current = health_row(report(tracker), key).get_text(" ", strip=True)
    assert "Current failure classification: http_403" in current
    path = tracker.data_dir / "feed_health.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    forged_key = "rss:0123456789abcdef"
    for identity in (forged_key, "https://fixture-private-canary.invalid/token", "rss:NOT-A-HASH", "evil:0123456789abcdef", '<script>fixture-private-canary</script>'):
        state[identity] = dict(state[key], name=identity, last_error="fixture_private_canary")
    path.write_text(json.dumps(state), encoding="utf-8")
    restart = trackers(theme)
    html = report(restart, output_path=restart.reports_dir / "safe-restored.html")
    history = health_row(html, key).get_text(" ", strip=True)
    assert "Current collection: unknown" in history
    assert "Historical failure classification: http_403" in history
    assert "Current failure classification: http_403" not in history
    assert health_row(html, forged_key) is not None
    assert "fixture_private_canary" not in html
    assert "fixture-private-canary" not in html
    assert "NOT-A-HASH" not in html
    assert "evil:0123456789abcdef" not in html
    assert BeautifulSoup(html, "html.parser").select_one("#news-feed-health a") is None
    assert "news-feed-health" not in report(restart, include_news=False)


@pytest.mark.asyncio
async def test_actual_budget_skip_does_not_create_failure_history(trackers, monkeypatch):
    tracker = trackers()
    adapter, calls = install_rss(monkeypatch, tracker, {"/one": (200, ONE_RSS, {})}, feed_budget_seconds=0)
    await tracker.run(date=NOW, push_breaking=False)
    key = adapter._feed_key("https://example.org/one")
    assert tracker.feed_results["rss"][key] == {"status": "not_attempted", "items": 0}
    assert key not in tracker.feed_health._sources
    html = report(tracker)
    feed = health_row(html, key).get_text(" ", strip=True)
    assert "Current collection: not_attempted" in feed
    assert "Items in current outcome: 0" in feed
    assert "cumulative items from successful observations: unknown" in feed
    assert "Historical last failure: unknown" in feed
    assert "Current collection: not_attempted" in health_row(html, "rss").get_text(" ", strip=True)
    assert calls == []


@pytest.mark.asyncio
async def test_source_circuit_skip_leaves_feed_current_unknown(trackers, monkeypatch):
    tracker = trackers(failure_threshold=1)
    adapter, calls = install_rss(monkeypatch, tracker, {"/blocked": (403, "fixture", {})})
    key = adapter._feed_key("https://example.org/blocked")
    await tracker.run(date=NOW, push_breaking=False)
    await tracker.run(date=NOW, push_breaking=False)
    html = report(tracker)
    assert tracker.feed_results == {}
    assert calls == ["/blocked"]
    assert "Current collection: cooldown" in health_row(html, "rss").get_text(" ", strip=True)
    feed = health_row(html, key).get_text(" ", strip=True)
    assert "Current collection: unknown" in feed
    assert "Items in current outcome: unknown" in feed


@pytest.mark.parametrize("theme", ["lain", "default"])
def test_reporter_public_save_and_legacy_contracts_reject_news_health_leaks(tmp_path, theme):
    from tracker_core.reporter_html import HTMLReporter
    from markupsafe import Markup
    from datetime import tzinfo

    class NoOffset(tzinfo):
        def utcoffset(self, dt):
            return None

    reporter = HTMLReporter(theme)
    rows = [{"source": "rss:0123456789abcdef", "current": {"status": "failed", "items": 0, "error": "http_403"},
             "history": {"total_items": 2, "last_success": NOW}}]
    canary = Markup('<img src=x onerror="fixture-private-canary">')
    rows += [None, {"source": canary}, {"source": "rss:NOT-A-HASH"},
             {"source": "youtube:0123456789abcdef", "current": {"status": [canary], "items": canary, "error": canary},
              "history": {"total_items": True, "consecutive_failures": -1, "last_error": "fixture_private_canary",
                          "last_success": datetime(2026, 10, 2, 12, tzinfo=NoOffset()), "last_failure": canary, "cooldown_until": canary}}]
    # Old six positional arguments remain byte-identical to equivalent keywords.
    legacy = reporter.render([], [], [], "2026-10-02", "12:00 UTC", None)
    assert legacy == reporter.render(api_items=[], news_items=[], tool_items=[], report_date="2026-10-02", generated_at="12:00 UTC", persona_image_url=None)
    assert reporter.save(tmp_path / "legacy.html", [], [], [], "2026-10-02", "12:00 UTC", None).read_text(encoding="utf-8") == legacy
    html = reporter.render([], [], [], "2026-10-02", "12:00 UTC", None, news_health_items=rows)
    assert "fixture-private-canary" not in html and "fixture_private_canary" not in html
    assert "NOT-A-HASH" not in html
    unknown = health_row(html, "youtube:0123456789abcdef").get_text(" ", strip=True)
    assert "Current collection: unknown" in unknown
    assert "Items in current outcome: unknown" in unknown
    assert "cumulative items from successful observations: unknown" in unknown
    assert "Historical last success: unknown" in unknown
    assert BeautifulSoup(html, "html.parser").select_one("#news-feed-health a") is None
    saved = reporter.save(tmp_path / "internal.html", [], [], [], "2026-10-02", "12:00 UTC", None, news_health_items=rows)
    assert saved.read_text(encoding="utf-8") == html
    public = reporter.render([], [], [], "2026-10-02", "12:00 UTC", None, news_health_items=rows, public_export=True)
    assert "news-feed-health" not in public and "rss:0123456789abcdef" not in public
    public_saved = reporter.save(tmp_path / "public.html", [], [], [], "2026-10-02", "12:00 UTC", None, news_health_items=rows, public_export=True)
    assert public_saved.read_text(encoding="utf-8") == public


def test_packaged_template_copies_are_identical():
    from pathlib import Path

    root = Path(main.__file__).parent
    for filename in ("report_template.html", "report_template_lain.html"):
        selected = (root / "src/templates" / filename).read_bytes()
        packaged = (root / "src/tracker-core/templates" / filename).read_bytes()
        assert selected == packaged
