"""Customer release storage regressions; all state is isolated in pytest scratch."""
from datetime import datetime, timedelta, timezone, tzinfo
import json
import os

import pytest

from intel_sources.store import IntelStore
from news_tracker.health import SourceHealthTracker
from news_tracker.store_sqlite import SQLiteStore
from tracker_core.schema import NewsItem, SourceType

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)

# 10:15 wall clock with no usable offset. A real feed/adapter can hand the
# schema a datetime whose tzinfo object is non-null but supplies no offset.
OFFSETLESS_WALL = datetime(2024, 1, 2, 10, 15)
OFFSETLESS_STORED_UTC = "2024-01-02 10:15:00.000000"
# The same wall clock at +02:00 is a genuinely known instant: 08:15 UTC. It must
# stay 08:15, proving the guard does not flatten real offsets into the wall time.
OFFSET_AWARE_STORED_UTC = "2024-01-02 08:15:00.000000"


class UnspecifiedOffset(tzinfo):
    def utcoffset(self, dt):
        return None

    def dst(self, dt):
        return None

    def tzname(self, dt):
        return "UNSPECIFIED"


def news(title="Observed AI release", **kwargs):
    return NewsItem(source=SourceType.NEWS, title=title,
                    url="https://example.org/" + title.replace(" ", "-"), **kwargs)


def test_sqlite_duplicate_count_matches_actual_rows_across_restart(tmp_path):
    path = tmp_path / "news.db"
    store = SQLiteStore(path)
    row = news()
    assert store.save_items([row, row]) == 1
    assert store.save_items([row]) == 0
    assert len(store.get_items(days=0)) == 1
    store.close()
    restored = SQLiteStore(path)
    assert restored.save_items([row]) == 0
    assert len(restored.get_items(days=0)) == 1
    restored.close()


@pytest.mark.parametrize("bad_record", [
    {"last_success": "invalid"},
    {"last_success": "2026-10-01T12:00:00"},
    {"consecutive_failures": "three"},
    {"consecutive_failures": -1},
    {"is_disabled": "false"},
    {"total_items": []},
    {"unexpected": "field"},
    None,
])
def test_bad_health_row_does_not_discard_valid_sibling_cooldown(tmp_path, caplog, bad_record):
    path = tmp_path / "health.json"
    tracker = SourceHealthTracker(failure_threshold=1, state_path=str(path))
    tracker._now = lambda: NOW
    tracker.record_failure("rss:valid", "http_429", retry_after_seconds=90)
    tracker.save_state()
    data = json.loads(path.read_text())
    data["rss:bad"] = bad_record
    path.write_text(json.dumps(data))
    restored = SourceHealthTracker(state_path=str(path))
    restored._now = lambda: NOW
    restored.load_state()
    assert not restored.is_source_enabled("rss:valid")
    assert "rss:bad" not in restored._sources
    restored._now = lambda: NOW + timedelta(seconds=91)
    assert restored.is_source_enabled("rss:valid")
    assert restored.get_or_create("rss:valid").consecutive_failures == 1


def test_sqlite_bad_item_keeps_valid_siblings_without_private_diagnostics(tmp_path, caplog):
    store = SQLiteStore(tmp_path / "news.db")
    bad = news("PRIVATE_ITEM_CANARY", raw_data={"unserializable": object()})
    assert store.save_items([news("First AI release"), bad, news("Second AI release")]) == 2
    assert len(store.get_items(days=0)) == 2
    assert "PRIVATE_ITEM_CANARY" not in caplog.text
    assert "TypeError" in caplog.text
    store.close()


@pytest.mark.parametrize("state", [
    {"items": [], "sources": [], "changes": None},
    {"items": [], "sources": [], "changes": [], "initialized": "false"},
    {"items": [], "sources": [], "changes": [] , "baseline": "true"},
    {"items": [], "sources": [], "changes": [{"id": "x"}]},
])
def test_structurally_malformed_intel_is_rejected_without_overwrite(tmp_path, state):
    path = tmp_path / "intel.json"
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        IntelStore(path).update([], [], now=NOW)
    assert path.read_bytes() == before


def test_sqlite_preserves_observation_time_instead_of_reimport_time(tmp_path):
    store = SQLiteStore(tmp_path / "news.db")
    old = datetime.now(timezone.utc) - timedelta(days=30)
    assert store.save_items([news(fetched_date=old)]) == 1
    saved = store.get_items(days=0)[0]
    parsed = datetime.fromisoformat(saved["fetched_date"])
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    assert abs((parsed - old).total_seconds()) < 1
    assert store.get_items(days=7) == []
    assert store.get_weekly_summary()["total_items"] == 0
    store.close()


def test_sqlite_database_error_rolls_back_batch_instead_of_claiming_success(tmp_path, caplog):
    import sqlite3
    store = SQLiteStore(tmp_path / "news.db")
    store._get_conn().execute("""CREATE TRIGGER reject_item BEFORE INSERT ON news_items
        WHEN NEW.title = 'PRIVATE_DB_CANARY'
        BEGIN SELECT RAISE(ABORT, 'PRIVATE_SQL_CANARY'); END;""")
    with pytest.raises(sqlite3.IntegrityError):
        store.save_items([news("First AI release"), news("PRIVATE_DB_CANARY")])
    assert store.get_items(days=0) == []
    assert "PRIVATE_DB_CANARY" not in caplog.text
    assert "PRIVATE_SQL_CANARY" not in caplog.text
    assert store.save_items([news("Next AI release")]) == 1
    store.close()


@pytest.mark.parametrize("field", ["id", "source", "source_name"])
def test_bad_incoming_intel_does_not_poison_last_good_snapshot(tmp_path, field):
    path = tmp_path / "intel.json"
    store = IntelStore(path)
    item = {"id": "catalog:a", "source": "catalog"}
    sources = [{"name": "catalog", "status": "ok", "checked_at": NOW.isoformat()}]
    store.update([item], sources, now=NOW)
    before = path.read_bytes()
    if field == "source_name":
        sources = [dict(sources[0], name="")]
    else:
        item = dict(item, **{field: ""})
    with pytest.raises(ValueError):
        store.update([item], sources, now=NOW)
    assert path.read_bytes() == before


def test_health_failure_diagnostics_never_echo_upstream_private_message(tmp_path, caplog):
    path = tmp_path / "health.json"
    tracker = SourceHealthTracker(failure_threshold=1, state_path=str(path))
    tracker.record_failure("rss:fixture", "https://private.example/?key=PRIVATE_CANARY")
    assert tracker.get_health_table()[0]["last_error"] == "fetch_failure"
    tracker.save_state()
    assert "PRIVATE_CANARY" not in path.read_text()
    assert "PRIVATE_CANARY" not in caplog.text


@pytest.mark.parametrize("error", ["https://private.example/?key=PRIVATE_CANARY", [], {}])
def test_health_restored_error_is_safe_to_display_and_resave(tmp_path, error):
    path = tmp_path / "health.json"
    path.write_text(json.dumps({"rss:fixture": {"last_error": error}}))
    tracker = SourceHealthTracker(state_path=str(path))
    tracker.load_state()
    assert tracker.get_health_table()[0]["last_error"] == "fetch_failure"
    tracker.save_state()
    assert "PRIVATE_CANARY" not in path.read_text()


@pytest.mark.parametrize("hours,freshness", [(0, "fresh"), (25, "stale"), (-1, "stale")])
def test_intel_restart_recalculates_age_without_refreshing_observation(tmp_path, hours, freshness):
    path = tmp_path / "intel.json"
    checked = NOW - timedelta(hours=hours)
    store = IntelStore(path)
    sources = [{"name": "catalog", "status": "ok", "checked_at": checked.isoformat()}]
    store.update([{"id": "catalog:a", "source": "catalog"}], sources, now=NOW)
    row = IntelStore(path).read(now=NOW)["items"][0]
    assert row["checked_at"] == checked.isoformat()
    assert row["freshness"] == freshness


@pytest.mark.parametrize("contents", ["{broken", "[]", "null"])
def test_bad_health_document_is_nonfatal_and_never_rewrites_on_load(tmp_path, contents):
    path = tmp_path / "health.json"
    path.write_text(contents)
    health = SourceHealthTracker(state_path=str(path))
    health.load_state()
    assert health.get_health_table() == []
    assert path.read_text() == contents


def test_health_failed_replace_keeps_last_snapshot_and_cleans_temp(tmp_path, monkeypatch):
    path = tmp_path / "health.json"
    health = SourceHealthTracker(state_path=str(path))
    health.record_success("rss:fixture", 2)
    health.save_state()
    before = path.read_bytes()
    health.record_failure("rss:fixture", "timeout")
    def failed_replace(*args):
        raise OSError("PRIVATE_PATH_CANARY")
    monkeypatch.setattr(os, "replace", failed_replace)
    with pytest.raises(OSError):
        health.save_state()
    assert path.read_bytes() == before
    assert list(tmp_path.iterdir()) == [path]


def test_sqlite_malformed_database_fails_explicitly_without_replacing_bytes(tmp_path):
    import sqlite3
    path = tmp_path / "news.db"
    path.write_bytes(b"not a database")
    with pytest.raises(sqlite3.DatabaseError):
        SQLiteStore(path)
    assert path.read_bytes() == b"not a database"


@pytest.mark.asyncio
async def test_title_fallback_and_feed_excerpt_survive_sqlite_restart_without_inference(tmp_path, monkeypatch):
    from news_tracker.summarizer import Summarizer, summarize_news
    async def forbidden(*args, **kwargs):
        pytest.fail("No inference permitted in unconfigured summary path")
    monkeypatch.setattr(Summarizer, "_call_llm", forbidden)
    rows = await summarize_news([news("AI title"), news("AI feed", summary="Observed publisher excerpt")])
    path = tmp_path / "news.db"
    store = SQLiteStore(path)
    assert store.save_items(rows) == 2
    store.close()
    restored = SQLiteStore(path)
    summaries = {r["title"]: r["summary"] for r in restored.get_items(days=0)}
    assert summaries == {"AI title": "Title excerpt (not AI summary): AI title", "AI feed": "Observed publisher excerpt"}
    restored.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["github", "reddit", "hackernews", "arxiv", "rss", "huggingface", "youtube"])
async def test_every_default_subfeed_is_enumerated_and_persisted_without_network(tmp_path, source):
    from news_tracker.adapters import get_all_adapters
    from tracker_core.pipeline.fetcher import FetchResult
    path = tmp_path / "feeds.json"
    health = SourceHealthTracker(state_path=str(path))
    adapter = next(a for a in get_all_adapters({"feed_health": health, "rate_limit_delay": 0}) if a.name == source)
    requests = []
    xml = ('<rss version="2.0"><channel><title>Fixture</title><item><title>AI fixture</title>'
           '<link>https://example.org/ai-fixture</link><pubDate>Thu, 01 Oct 2026 00:00:00 GMT</pubDate>'
           '<description>Publisher excerpt</description></item></channel></rss>')
    async def fetch(url, **kwargs):
        requests.append(url)
        status, body = 200, xml
        if "api.github.com/search" in url:
            body = json.dumps({"items": [{"full_name": "example/ai", "html_url": "https://github.com/example/ai", "description": "AI fixture"}]})
        elif "api.github.com/orgs/" in url:
            org = url.split("/orgs/")[1].split("/")[0]
            body = json.dumps([{"full_name": org + "/fixture"}])
        elif "releases/latest" in url:
            status, body = 404, "{}"
        elif "hn.algolia.com" in url:
            body = json.dumps({"hits": [{"title": "AI fixture", "url": "https://example.org/ai-fixture", "created_at_i": 1790812800}]})
        elif "huggingface.co/models" in url:
            body = '<a href="/example/ai-fixture">AI model</a>'
        elif "huggingface.co/papers" in url:
            body = '<a href="/papers/2609.12345">Paper</a>'
        return FetchResult(body, {}, status, url, 0, status == 200)
    adapter.fetcher.fetch = fetch
    items = await adapter.fetch_all()
    assert items
    expected = {"github": 1 + 2 * len(adapter.orgs) if source == "github" else 0,
                "reddit": 6, "hackernews": 8, "arxiv": 5, "rss": 17, "huggingface": 2, "youtube": 3}[source]
    assert len(requests) == expected
    assert len(adapter.feed_results) == expected
    assert all(row["status"] == "healthy" for row in adapter.feed_results.values())
    restored = SourceHealthTracker(state_path=str(path)); restored.load_state()
    assert {r["source"] for r in restored.get_health_table()} == set(adapter.feed_results)


def test_sqlite_future_observation_is_history_not_recent_news(tmp_path):
    store = SQLiteStore(tmp_path / "news.db")
    assert store.save_items([news(fetched_date=datetime.now(timezone.utc) + timedelta(days=1))]) == 1
    assert len(store.get_items(days=0)) == 1
    assert store.get_items(days=7) == []
    assert store.get_weekly_summary()["total_items"] == 0
    store.close()


def test_schema_keeps_accepting_observation_with_non_null_timezone_without_offset():
    """Storage is what must be corrected; the model still accepts this input."""
    ambiguous = news(fetched_date=OFFSETLESS_WALL.replace(tzinfo=UnspecifiedOffset()))
    assert ambiguous.fetched_date.tzinfo is not None
    assert ambiguous.fetched_date.utcoffset() is None


def test_sqlite_offsetless_observation_never_becomes_machine_local_time(tmp_path):
    """The SQLite fetched_date contract attaches UTC to offsetless observations.

    A non-null tzinfo with no offset must not send astimezone() through the host
    machine's local zone; on a UTC host the old and new values coincide, so this
    asserts the contract everywhere and reproduces the defect on non-UTC hosts.
    """
    store = SQLiteStore(tmp_path / "news.db")
    assert store.save_items([
        news("Offsetless observation", fetched_date=OFFSETLESS_WALL.replace(tzinfo=UnspecifiedOffset()))
    ]) == 1
    assert store.get_items(days=0)[0]["fetched_date"] == OFFSETLESS_STORED_UTC
    store.close()


def test_sqlite_offsetless_and_naive_observations_agree_and_survive_restart(tmp_path):
    """Unknown-offset and naive inputs share one contract; a real offset is kept."""
    store = SQLiteStore(tmp_path / "news.db")
    assert store.save_items([
        news("Offsetless observation", fetched_date=OFFSETLESS_WALL.replace(tzinfo=UnspecifiedOffset())),
        news("Naive observation", fetched_date=OFFSETLESS_WALL),
        news("Offset aware observation",
             fetched_date=OFFSETLESS_WALL.replace(tzinfo=timezone(timedelta(hours=2)))),
    ]) == 3
    values = {row["title"]: row["fetched_date"] for row in store.get_items(days=0)}
    store.close()
    restored = SQLiteStore(tmp_path / "news.db")
    assert {row["title"]: row["fetched_date"]
            for row in restored.get_items(days=0)} == values
    restored.close()
    assert values["Offsetless observation"] == OFFSETLESS_STORED_UTC
    assert values["Naive observation"] == OFFSETLESS_STORED_UTC
    assert values["Offset aware observation"] == OFFSET_AWARE_STORED_UTC


class _OffsetlessSpy(datetime):
    """An offsetless wall clock that records whether astimezone() was used on it.

    On a UTC host the wrong and right stored values coincide, so a value
    assertion cannot tell the shipped guard from the old buggy call. This spy
    makes the decision observable everywhere: the fixed guard must attach UTC
    WITHOUT ever calling astimezone(), because astimezone() on an offsetless
    value reads the wall clock as machine-local time.

    Which host proves what:
      * this test (any host, Windows included): fails RED if the guard is reverted.
      * a non-UTC POSIX host: also reproduces the original wrong stored value.
    """

    def __new__(cls, *args, **kwargs):
        return super().__new__(cls, *args, **kwargs)

    def astimezone(self, tz=None):
        # The defect is calling astimezone() while the value is STILL offsetless:
        # that is what reads the wall clock as machine-local time. Calling it
        # after the guard has already attached UTC is a no-op and is fine.
        if self.utcoffset() is None:
            type(self).offsetless_astimezone_calls += 1
        return super().astimezone(tz)


def test_offsetless_ingestion_never_routes_through_astimezone(tmp_path):
    """The defect is the CALL, not the value: on UTC hosts the value looks right."""
    _OffsetlessSpy.offsetless_astimezone_calls = 0
    ambiguous = _OffsetlessSpy(2024, 1, 2, 10, 15, tzinfo=UnspecifiedOffset())
    store = SQLiteStore(tmp_path / "news.db")
    try:
        assert store.save_items([news("Offsetless observation", fetched_date=ambiguous)]) == 1
        assert store.get_items(days=0)[0]["fetched_date"] == OFFSETLESS_STORED_UTC
    finally:
        store.close()
    assert _OffsetlessSpy.offsetless_astimezone_calls == 0, (
        "offsetless observations must be given UTC directly, never passed through "
        "astimezone() while still offsetless, which reads the wall clock as "
        "machine-local time")


def test_offsetless_and_naive_and_aware_still_differ_as_required(tmp_path):
    """Control whose expected values DIFFER, so a broad flattening guard fails."""
    _OffsetlessSpy.offsetless_astimezone_calls = 0
    store = SQLiteStore(tmp_path / "news.db")
    try:
        assert store.save_items([
            news("Offsetless", fetched_date=_OffsetlessSpy(2024, 1, 2, 10, 15, tzinfo=UnspecifiedOffset())),
            news("Naive", fetched_date=datetime(2024, 1, 2, 10, 15)),
            news("Aware", fetched_date=datetime(2024, 1, 2, 10, 15, tzinfo=timezone(timedelta(hours=2)))),
        ]) == 3
        values = {row["title"]: row["fetched_date"] for row in store.get_items(days=0)}
    finally:
        store.close()
    assert values["Offsetless"] == OFFSETLESS_STORED_UTC
    assert values["Naive"] == OFFSETLESS_STORED_UTC
    assert values["Aware"] == OFFSET_AWARE_STORED_UTC
    assert values["Aware"] != values["Offsetless"], \
        "a genuinely offset-aware observation must not be flattened to the wall clock"
    assert _OffsetlessSpy.offsetless_astimezone_calls == 0
