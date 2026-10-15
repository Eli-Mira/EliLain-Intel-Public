"""News date provenance through the actual persisted-row/report boundary.

Synthetic fixtures only: no collection, inference, summary or notification.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from bs4 import BeautifulSoup
import httpx
import pytest

import main as tracker_main
from tracker_core.reporter_html import LainReporter, _news_timestamp
from tracker_core.schema import NewsItem
from test_intel_reporter import browser  # Shared strict launch/no-catch-all fixture.

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_DIRS = [ROOT / "src/templates", ROOT / "src/tracker-core/templates"]


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("date-provenance tests must not make network requests")
    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)


def test_internal_news_retains_publication_and_saved_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    observed = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=1)
    publication = datetime(2024, 1, 2, 12, 15, tzinfo=timezone(timedelta(hours=2)))
    tracker = tracker_main.NewsTracker(
        data_dir=tmp_path / "data", cache_dir=tmp_path / "cache",
        reports_dir=tmp_path / "reports", freellmapi_db_path=tmp_path / "absent-router.db")
    try:
        assert tracker.sqlite.save_items([
            NewsItem(source="hackernews", title="Dated fixture", url="https://example.org/dated",
                     published_date=publication, fetched_date=observed, relevance_score=0.9),
            NewsItem(source="hackernews", title="Unknown publication fixture",
                     url="https://example.org/unknown", published_date=None,
                     fetched_date=observed - timedelta(minutes=5), relevance_score=0.8),
        ]) == 2
        before = tracker.sqlite.get_items(days=0)
        html = tracker.generate_report(
            include_providers=False, include_tools=False,
            output_path=tmp_path / "reports" / "internal.html")
        assert tracker.sqlite.get_items(days=0) == before
        soup = BeautifulSoup(html, "html.parser")
        rows = soup.select("#news li.item:not(.empty-state)")
        assert len(rows) == 2
        known = rows[0].select_one(".news-dates")
        assert known is not None, "persisted publication/observation dates disappeared before rendering"
        assert "Publication (UTC):" in known.get_text(" ", strip=True)
        assert "Saved observation (UTC):" in known.get_text(" ", strip=True)
        times = known.select("time")
        assert [node["datetime"] for node in times] == [
            publication.astimezone(timezone.utc).isoformat(), observed.isoformat()]
        assert [node.get_text(strip=True) for node in times] == [node["datetime"] for node in times]
        missing = rows[1].select_one(".news-dates")
        assert missing is not None
        assert "Publication (UTC): unknown" in missing.get_text(" ", strip=True)
        assert [node["datetime"] for node in missing.select("time")] == [
            (observed - timedelta(minutes=5)).isoformat()]
        assert "regenerating this report does not refresh either" in soup.select_one("#news").get_text()
    finally:
        tracker.sqlite.close()


@pytest.mark.parametrize("value, sqlite_utc, expected", [
    (None, False, None),
    ("", True, None),
    (True, True, None),
    (42, True, None),
    ({"date": "2024-01-02T10:15:00Z"}, False, None),
    ([], True, None),
    ("MALFORMED DATE", True, None),
    ("2024-01-02", True, None),
    ("2024-01-02T10:15:00", False, None),
    ("2024-01-02 10:15:00.123456", True, "2024-01-02T10:15:00.123456+00:00"),
    ("2024-01-02T12:15:00+02:00", False, "2024-01-02T10:15:00+00:00"),
    ("2024-01-02T10:15:00Z", False, "2024-01-02T10:15:00+00:00"),
    (datetime(2024, 1, 2, 10, 15), False, None),
    (datetime(2024, 1, 2, 10, 15), True, "2024-01-02T10:15:00+00:00"),
    (datetime(2024, 1, 2, 10, 15, tzinfo=timezone.utc), False, "2024-01-02T10:15:00+00:00"),
    ("0001-01-01T00:00:00+14:00", False, None),
    ("2024-01-02T10:15:00Z" + " " * 70, True, None),
])
def test_news_timestamp_keeps_only_unambiguous_saved_time(value, sqlite_utc, expected):
    assert _news_timestamp(value, sqlite_utc) == expected


@pytest.mark.parametrize("sqlite_utc, expected", [
    (False, None),
    (True, "2024-01-02T10:15:00+00:00"),
])
def test_non_null_timezone_without_offset_never_implies_local_time(sqlite_utc, expected):
    from datetime import tzinfo

    class UnspecifiedOffset(tzinfo):
        def utcoffset(self, dt):
            return None

        def dst(self, dt):
            return None

    ambiguous = datetime(2024, 1, 2, 10, 15, tzinfo=UnspecifiedOffset())
    assert ambiguous.tzinfo is not None and ambiguous.utcoffset() is None
    assert _news_timestamp(ambiguous, sqlite_utc) == expected


@pytest.mark.parametrize("template_dir", TEMPLATE_DIRS)
def test_legacy_news_with_no_dates_is_explicitly_unknown(monkeypatch, template_dir):
    monkeypatch.setattr(LainReporter, "TEMPLATE_DIR", template_dir)
    html = LainReporter().render(api_items=[], news_items=[{
        "headline": "Legacy fixture", "summary": "", "source": "hackernews",
        "url": "https://example.org/legacy"}], report_date="2026-10-02", generated_at="12:00 UTC")
    dates = BeautifulSoup(html, "html.parser").select_one("#news .news-dates")
    assert dates is not None
    assert dates.get_text(" ", strip=True) == "Publication (UTC): unknown · Saved observation (UTC): unknown"
    assert not dates.select("time")


@pytest.mark.parametrize("template_dir", TEMPLATE_DIRS)
def test_invalid_news_dates_are_not_echoed_or_made_into_time_elements(monkeypatch, template_dir):
    monkeypatch.setattr(LainReporter, "TEMPLATE_DIR", template_dir)
    poison = '<img src="https://example.org/DATE_CANARY" onerror="alert(1)">'
    html = LainReporter().render(api_items=[], news_items=[{
        "headline": "Malformed fixture", "summary": "", "source": "hackernews",
        "url": "https://example.org/malformed", "published_date": poison, "fetched_date": poison}],
        report_date="2026-10-02", generated_at="12:00 UTC")
    soup = BeautifulSoup(html, "html.parser")
    dates = soup.select_one("#news .news-dates")
    assert dates is not None and not dates.select("time")
    assert dates.get_text(" ", strip=True).count("unknown") == 2
    assert "DATE_CANARY" not in html
    assert not soup.select("#news img, #news [onerror]")


@pytest.mark.parametrize("template_dir", TEMPLATE_DIRS)
def test_public_news_boundary_is_byte_identical_with_private_date_canaries(monkeypatch, template_dir):
    monkeypatch.setattr(LainReporter, "TEMPLATE_DIR", template_dir)
    reporter = LainReporter()
    arguments = {"api_items": [], "public_export": True, "report_date": "2026-10-02",
                 "generated_at": "12:00 UTC"}
    clean = reporter.render(news_items=[], **arguments)
    forged = reporter.render(news_items=[{
        "headline": "INTERNAL_NEWS_CANARY", "summary": "INTERNAL_SUMMARY_CANARY",
        "source": "INTERNAL_SOURCE_CANARY", "url": "https://example.org/INTERNAL_URL_CANARY",
        "published_date": "2020-01-02T10:15:00Z", "fetched_date": "2020-01-03 10:15:00"}],
        **arguments)
    assert forged == clean
    news = BeautifulSoup(forged, "html.parser").select_one("#news")
    assert "News excluded from this public export" in news.get_text()
    assert not news.select("time, .news-dates")


def test_restart_and_regeneration_do_not_refresh_news_observation(tmp_path, monkeypatch):
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    render_time = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    observed = render_time - timedelta(hours=1)
    config = {"data_dir": tmp_path / "data", "cache_dir": tmp_path / "cache",
              "reports_dir": tmp_path / "reports", "freellmapi_db_path": tmp_path / "absent-router.db"}
    initial = tracker_main.NewsTracker(**config)
    initial.sqlite.save_items([NewsItem(source="hackernews", title="Retained fixture",
        url="https://example.org/retained", fetched_date=observed, published_date=None)])
    initial.sqlite.close()
    tracker = tracker_main.NewsTracker(**config)

    class FrozenDateTime(datetime):
        current = render_time

        @classmethod
        def now(cls, tz=None):
            return cls.current.astimezone(tz) if tz is not None else cls.current.replace(tzinfo=None)

    monkeypatch.setattr(tracker_main, "datetime", FrozenDateTime)
    try:
        before = tracker.sqlite.get_items(days=0)
        first = tracker.generate_report(include_providers=False, include_tools=False,
                                        output_path=tmp_path / "first.html")
        FrozenDateTime.current = render_time + timedelta(minutes=7)
        second = tracker.generate_report(include_providers=False, include_tools=False,
                                         output_path=tmp_path / "second.html")
        assert tracker.sqlite.get_items(days=0) == before
        first_dom = BeautifulSoup(first, "html.parser")
        second_dom = BeautifulSoup(second, "html.parser")
        assert first_dom.select_one(".news-dates").get_text() == second_dom.select_one(".news-dates").get_text()
        assert second_dom.select_one("#news time")["datetime"] == observed.isoformat()
        assert render_time.strftime("%H:%M UTC") in first_dom.select_one("footer").get_text()
        assert FrozenDateTime.current.strftime("%H:%M UTC") in second_dom.select_one("footer").get_text()
    finally:
        tracker.sqlite.close()


@pytest.mark.parametrize("width", [390, 1440])
@pytest.mark.parametrize("javascript", [True, False])
def test_news_dates_are_visible_offline_without_overflow(browser, tmp_path, width, javascript):
    artifact = tmp_path / "synthetic-internal-news.html"
    artifact.write_text(LainReporter().render(api_items=[], news_items=[{
        "headline": "Synthetic date fixture", "summary": "No actual fetch occurred.",
        "source": "hackernews", "url": "https://example.org/synthetic-date",
        "published_date": "2024-01-02T12:15:00+02:00",
        "fetched_date": "2026-10-01 20:00:00.123456"}],
        report_date="2026-10-02", generated_at="12:00 UTC"), encoding="utf-8")
    context = browser.new_context(viewport={"width": width, "height": 900},
                                  java_script_enabled=javascript, offline=True)
    try:
        page = context.new_page()
        requests, errors = [], []
        page.on("request", lambda request: requests.append(request.url))
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.goto(artifact.as_uri(), wait_until="load")
        dates = page.locator("#news .news-dates")
        assert dates.count() == 1 and dates.is_visible()
        assert dates.locator("time").evaluate_all(
            "nodes => nodes.map(node => node.getAttribute('datetime'))") == [
                "2024-01-02T10:15:00+00:00", "2026-10-01T20:00:00.123456+00:00"]
        assert "Publication (UTC):" in dates.inner_text()
        assert "Saved observation (UTC):" in dates.inner_text()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1")
        assert len(requests) == 1 and requests[0].startswith("file:")
        assert not errors
    finally:
        context.close()
