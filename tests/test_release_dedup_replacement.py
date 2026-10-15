"""Regression coverage for replacement bookkeeping in news deduplication."""

from datetime import datetime, timezone

import pytest

from news_tracker.dedup_filter import Deduplicator
from tracker_core.schema import NewsItem, SourceType


FETCHED_AT = datetime(2026, 10, 1, tzinfo=timezone.utc)


def item(title: str, url_suffix: str, score: float) -> NewsItem:
    return NewsItem(
        source=SourceType.GITHUB,
        title=title,
        url=f"https://example.com/{url_suffix}",
        relevance_score=score,
        fetched_date=FETCHED_AT,
    )


def test_url_replacement_does_not_leave_removed_title_in_index():
    x = item("Amber observatory report", "shared", 0.1)
    y = item("Zephyr compiler launch", "shared", 0.2)
    z = item(x.title, "different", 0.3)
    w = item("Ocean conservation census", "unrelated", 0.4)

    result = Deduplicator().deduplicate([x, y, z, w])

    assert result == [y, z, w]
    assert all(retained is not x for retained in result)


@pytest.mark.asyncio
async def test_production_run_survives_replacement_and_persists_winners(tmp_path, monkeypatch):
    import main
    monkeypatch.setenv('NOTIFICATION_CHANNEL', 'none')
    tracker = main.NewsTracker(data_dir=tmp_path / 'data', cache_dir=tmp_path / 'cache',
                               reports_dir=tmp_path / 'reports')
    x = item('OpenAI amber observatory report', 'shared', 0.80)
    y = item('OpenAI zephyr compiler launch', 'shared', 0.90)
    z = item(x.title, 'different', 0.95)
    w = item('OpenAI ocean conservation census', 'unrelated', 0.85)

    async def fetch_fixture():
        return [x, y, z, w]

    monkeypatch.setattr(tracker, 'fetch_all', fetch_fixture)
    result = await tracker.run(push_breaking=False)
    assert result['total_fetched'] == 4
    assert result['dedup']['duplicate_count'] == 1
    assert result['inserted'] == 3
    retained = tracker.sqlite.get_items(limit=100, days=0)
    assert {row['url'] for row in retained} == {str(row.url) for row in (y, z, w)}
    assert x.title not in {row['title'] for row in retained if row['url'] == str(x.url)}



@pytest.mark.parametrize("replacement_score", [0.3, 0.5])
def test_url_replacement_cannot_orphan_an_existing_title(replacement_score):
    x = item("Amber observatory report", "shared", 0.1)
    anchor = item("Zephyr compiler launch", "anchor", 0.4)
    y = item("Zephyr compiler launch update", "shared", replacement_score)
    dedup = Deduplicator()

    result = dedup.deduplicate([x, anchor, y])

    winner = y if replacement_score > anchor.relevance_score else anchor
    assert result == [winner]
    assert dedup.deduplicate(result) == result


def test_chained_url_and_fuzzy_replacements_release_old_aliases():
    x = item("Zephyr compiler launch", "original", 0.1)
    y = item("Zephyr compiler launch update", "second", 0.2)
    z = item("Amber observatory report", "second", 0.3)
    w = item(x.title, "third", 0.4)
    old_url_reused = item("Ocean conservation census", "original", 0.05)
    inputs = [x, y, z, w, old_url_reused]
    snapshots = [row.model_dump() for row in inputs]
    dedup = Deduplicator()

    result = dedup.deduplicate(inputs)

    assert result == [z, w, old_url_reused]
    assert dedup.deduplicate(result) == result
    assert dedup.deduplicate(inputs) == result
    assert [row.model_dump() for row in inputs] == snapshots


@pytest.mark.parametrize("duplicate_kind", ["url", "fuzzy_title"])
@pytest.mark.parametrize("score", [0.1, 0.8])
def test_lower_or_equal_score_duplicates_keep_incumbent(duplicate_kind, score):
    incumbent = item("Zephyr compiler launch", "first", 0.8)
    duplicate = item(
        "Amber observatory report" if duplicate_kind == "url"
        else "Zephyr compiler launch update",
        "first" if duplicate_kind == "url" else "second",
        score,
    )

    result = Deduplicator().deduplicate([incumbent, duplicate])

    assert len(result) == 1
    assert result[0] is incumbent


@pytest.mark.parametrize("duplicate_kind", ["url", "fuzzy_title"])
def test_rejected_duplicates_do_not_register_orphan_aliases(duplicate_kind):
    incumbent = item("Zephyr compiler launch", "first", 0.8)
    if duplicate_kind == "url":
        rejected = item("Amber observatory report", "first", 0.2)
        independent = item(rejected.title, "third", 0.1)
    else:
        rejected = item("Zephyr compiler launch update", "second", 0.2)
        independent = item("Amber observatory report", "second", 0.1)
    dedup = Deduplicator()

    result = dedup.deduplicate([incumbent, rejected, independent])

    assert result == [incumbent, independent]
    assert dedup.deduplicate(result) == result


def test_fuzzy_replacement_releases_old_url_for_lower_score_item():
    x = item("Zephyr compiler launch", "original", 0.4)
    y = item("Zephyr compiler launch update", "replacement", 0.8)
    reused = item("Amber observatory report", "original", 0.1)

    assert Deduplicator().deduplicate([x, y, reused]) == [y, reused]


def test_url_winner_title_is_used_for_later_fuzzy_replacement():
    x = item("Amber observatory report", "shared", 0.1)
    y = item("Zephyr compiler launch", "shared", 0.2)
    z = item("Zephyr compiler launch update", "different", 0.3)

    assert Deduplicator().deduplicate([x, y, z]) == [z]


def test_empty_normalized_title_replacement_keeps_indexes_consistent():
    x = item("!!!", "original", 0.1)
    y = item("???", "replacement", 0.2)
    reused = item("Amber observatory report", "original", 0.05)

    assert Deduplicator().deduplicate([x, y, reused]) == [y, reused]


@pytest.mark.parametrize("score", [0.3, 0.5])
def test_fuzzy_bridge_replacement_is_idempotent(score):
    first = item("aaaabbbb", "first", 0.2)
    second = item("bbbbcccc", "second", 0.4)
    bridge = item("aaaabbbbcccc", "bridge", score)
    dedup = Deduplicator(threshold=0.65)

    result = dedup.deduplicate([first, second, bridge])

    assert result == ([bridge] if score > second.relevance_score else [first, second])
    assert dedup.deduplicate(result) == result
