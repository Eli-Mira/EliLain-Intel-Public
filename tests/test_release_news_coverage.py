"""Release regression: the news section must not silently truncate.

`SQLiteStore.get_items` defaults to `limit=100`, and `generate_report` called it
with no limit. The report rendered exactly 100 stories and printed "100 stories"
- indistinguishable from a day that genuinely produced 100 - while quietly
discarding the rest. In hosted run 36856918737 the database held 160 rows and the
report showed 100 with no disclosure. The discoveries section already discloses
"N of M"; news must not silently truncate either.
"""
import re
from datetime import datetime, timedelta, timezone

import pytest

import main as tracker_main
from news_tracker.store_sqlite import SQLiteStore
from tracker_core.schema import NewsItem


def _seed(tmp_path, count: int) -> SQLiteStore:
    store = SQLiteStore(tmp_path / "news.db")
    now = datetime.now(timezone.utc)
    store.save_items([
        NewsItem(source="hackernews", title=f"Story {n}", url=f"https://example.org/{n}",
                 fetched_date=now - timedelta(minutes=n % 60),
                 relevance_score=n / count, is_relevant=True, summary=f"Summary {n}")
        for n in range(count)
    ])
    return store


def _news_meta(html: str) -> str:
    section = re.search(r"ai-news-tracker.*?</section>", html, re.S).group()
    return re.search(r'<p class="meta">(.*?)</p>', section, re.S).group(1).strip()


def _news_rows(html: str) -> int:
    section = re.search(r"ai-news-tracker.*?</section>", html, re.S).group()
    return section.count('<li class="item">') - section.count('class="item empty-state"')


@pytest.mark.parametrize("saved", [37, 100, 260])
def test_news_section_renders_every_saved_story_and_says_so(tmp_path, monkeypatch, saved):
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    store = _seed(tmp_path, saved)
    tracker = tracker_main.NewsTracker(data_dir=tmp_path)
    tracker.sqlite = store
    html = tracker.generate_report(
        output_path=tmp_path / "r.html",
        include_providers=False, include_news=True, include_tools=False)
    line = _news_meta(html)
    rendered = _news_rows(html)
    # Nothing observed today may be dropped without saying so.
    assert str(saved) in line, f"disclosure {line!r} does not name the {saved} saved stories"
    assert rendered >= saved or "of" in line, (rendered, saved, line)


def test_saved_count_survives_the_hundred_row_boundary(tmp_path, monkeypatch):
    """The exact regression: >100 rows must still be disclosed, not shown as 100."""
    monkeypatch.setenv("NOTIFICATION_CHANNEL", "none")
    store = _seed(tmp_path, 160)
    tracker = tracker_main.NewsTracker(data_dir=tmp_path)
    tracker.sqlite = store
    html = tracker.generate_report(
        output_path=tmp_path / "r.html",
        include_providers=False, include_news=True, include_tools=False)
    line = _news_meta(html)
    assert "100 stories" not in line, f"still silently truncated: {line!r}"
    assert "160" in line, line


def test_get_items_can_return_everything_when_asked(tmp_path):
    store = _seed(tmp_path, 140)
    assert len(store.get_items(days=3650, limit=1000)) == 140
