"""Release regression: the SQLite save log must not mislabel the batch size.

`save_items` logged `f"Saved {inserted} new items to SQLite ({len(items)} total)"`.
`len(items)` is the size of the batch handed to the call, not the number of rows
retained in the database, so the line reads as a running total when it is not one.

Hosted run 36860505208 logged `Saved 21 new items to SQLite (160 total)`; the
report from that same run retained 308 stories. An operator reading the log would
believe the database held 160 rows.
"""
import datetime
import logging

from news_tracker.store_sqlite import SQLiteStore
from tracker_core.schema import NewsItem


def _items(prefix: str, count: int) -> list[NewsItem]:
    now = datetime.datetime.now(datetime.timezone.utc)
    return [NewsItem(source="hackernews", title=f"{prefix}{n}",
                     url=f"https://example.org/{prefix}{n}",
                     fetched_date=now, relevance_score=0.5)
            for n in range(count)]


def test_save_log_reports_retained_rows_not_the_batch_size(tmp_path, caplog):
    store = SQLiteStore(tmp_path / "n.db")
    with caplog.at_level(logging.INFO, logger="news_tracker.store_sqlite"):
        store.save_items(_items("a", 20))
    first = caplog.text
    assert "20 retained" in first, first
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="news_tracker.store_sqlite"):
        store.save_items(_items("b", 5))
    # The batch was 5 and only 5 were new, but the database now holds 25.
    assert "5 new" in caplog.text, caplog.text
    assert "25 retained" in caplog.text, caplog.text
    assert "(5 total)" not in caplog.text, caplog.text


def test_save_log_is_truthful_when_the_batch_is_a_replay(tmp_path, caplog):
    """A restored CI cache replays yesterday's items; nothing new is retained."""
    store = SQLiteStore(tmp_path / "n.db")
    store.save_items(_items("a", 7))
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="news_tracker.store_sqlite"):
        inserted = store.save_items(_items("a", 7))
    assert inserted == 0, inserted
    assert "0 new" in caplog.text, caplog.text
    assert "7 retained" in caplog.text, caplog.text
