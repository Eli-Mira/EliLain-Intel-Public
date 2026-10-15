"""Tests for watchlist, change detection, and notification building."""
import json
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

from intel_sources.watchlist import (
    WatchedItem,
    SavedSearch,
    ChangeEvent,
    Watchlist,
    ChangeDetector,
    EventDeduplicator,
    RetryQueue,
    RetryJob,
    build_notification,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def watchlist(tmp_path):
    """Create a temporary watchlist."""
    return Watchlist(tmp_path / "watchlist")


@pytest.fixture
def watched_item():
    """Create a sample watched item."""
    return WatchedItem(
        id="github:meta-llama/llama.cpp",
        name="llama.cpp",
        kind="project",
        source_id="github-trending",
        url="https://github.com/meta-llama/llama.cpp",
        created_at="2026-10-01T00:00:00Z",
        categories=["devtools", "models"],
        tags=["local", "llm"],
    )


@pytest.fixture
def saved_search():
    """Create a sample saved search."""
    return SavedSearch(
        id="search-1",
        name="Local LLM tools",
        query="local LLM inference",
        categories=["devtools", "models"],
        min_score=70,
        created_at="2026-10-01T00:00:00Z",
    )


# ---------------------------------------------------------------------------
# WatchedItem tests
# ---------------------------------------------------------------------------


class TestWatchedItem:
    def test_from_dict(self):
        data = {
            "id": "test-1",
            "name": "Test Project",
            "kind": "project",
            "source_id": "github",
            "url": "https://example.com",
            "created_at": "2026-10-01T00:00:00Z",
            "categories": ["test"],
            "tags": ["tag1"],
        }
        item = WatchedItem.from_dict(data)
        assert item.id == "test-1"
        assert item.name == "Test Project"
        assert item.to_dict() == data

    def test_to_dict_roundtrip(self, watched_item):
        data = watched_item.to_dict()
        restored = WatchedItem.from_dict(data)
        assert restored == watched_item


# ---------------------------------------------------------------------------
# SavedSearch tests
# ---------------------------------------------------------------------------


class TestSavedSearch:
    def test_from_dict(self):
        data = {
            "id": "search-1",
            "name": "Test Search",
            "query": "test query",
            "categories": ["models"],
            "min_score": 70,
            "created_at": "2026-10-01T00:00:00Z",
        }
        search = SavedSearch.from_dict(data)
        assert search.name == "Test Search"
        assert search.min_score == 70

    def test_default_fields(self):
        data = {
            "id": "search-2",
            "name": "Minimal",
            "query": "q",
            "categories": [],
            "min_score": 60,
            "created_at": "2026-10-01T00:00:00Z",
        }
        search = SavedSearch.from_dict(data)
        assert search.last_run_at is None
        assert search.last_result_count == 0


# ---------------------------------------------------------------------------
# ChangeEvent tests
# ---------------------------------------------------------------------------


class TestChangeEvent:
    def test_from_dict(self):
        data = {
            "id": "evt-1",
            "watched_id": "item-1",
            "kind": "release",
            "description": "New release",
            "previous_state": {"version": "1.0"},
            "new_state": {"version": "2.0"},
            "confidence": 0.9,
            "evidence_urls": ["https://example.com"],
            "detected_at": "2026-10-07T00:00:00Z",
            "severity": "high",
            "notified": False,
            "dedup_key": "abc123",
        }
        event = ChangeEvent.from_dict(data)
        assert event.kind == "release"
        assert event.confidence == 0.9
        assert event.notified is False

    def test_defaults(self):
        data = {
            "id": "evt-2",
            "watched_id": "item-1",
            "kind": "pricing",
            "description": "Price changed",
            "previous_state": {},
            "new_state": {},
            "confidence": 0.5,
            "evidence_urls": [],
            "detected_at": "2026-10-07T00:00:00Z",
        }
        event = ChangeEvent.from_dict(data)
        assert event.severity == "medium"
        assert event.notified is False


# ---------------------------------------------------------------------------
# Watchlist tests
# ---------------------------------------------------------------------------


class TestWatchlist:
    def test_add_and_get_watched(self, watchlist, watched_item):
        watchlist.add_watched(watched_item)
        items = watchlist.get_watched()
        assert len(items) == 1
        assert items[0].id == watched_item.id

    def test_remove_watched(self, watchlist, watched_item):
        watchlist.add_watched(watched_item)
        assert watchlist.remove_watched(watched_item.id)
        assert len(watchlist.get_watched()) == 0
        assert not watchlist.remove_watched("nonexistent")

    def test_filter_by_kind(self, watchlist, watched_item):
        watchlist.add_watched(watched_item)
        all_items = watchlist.get_watched()
        project_items = watchlist.get_watched(kind="project")
        assert len(all_items) == 1
        assert len(project_items) == 1
        assert len(watchlist.get_watched(kind="model")) == 0

    def test_add_and_get_search(self, watchlist, saved_search):
        watchlist.add_search(saved_search)
        searches = watchlist.get_searches()
        assert len(searches) == 1
        assert searches[0].name == "Local LLM tools"

    def test_record_event(self, watchlist, watched_item):
        watchlist.add_watched(watched_item)
        event = ChangeEvent(
            id="evt-1",
            watched_id=watched_item.id,
            kind="release",
            description="New version",
            previous_state={},
            new_state={"version": "2.0"},
            confidence=0.9,
            evidence_urls=[],
            detected_at=datetime.now(timezone.utc).isoformat(),
        )
        watchlist.record_event(event)
        recent = watchlist.get_recent_events(hours=24)
        assert len(recent) == 1
        assert recent[0].kind == "release"

    def test_mark_notified(self, watchlist, watched_item):
        watchlist.add_watched(watched_item)
        now = datetime.now(timezone.utc).isoformat()
        event = ChangeEvent(
            id="evt-2",
            watched_id=watched_item.id,
            kind="pricing",
            description="Price changed",
            previous_state={},
            new_state={},
            confidence=0.5,
            evidence_urls=[],
            detected_at=now,
        )
        watchlist.record_event(event)
        assert watchlist.mark_notified("evt-2")
        recent = watchlist.get_recent_events(hours=24)
        assert len(recent) == 0  # Notified events are filtered out

    def test_persistence(self, watchlist, watched_item):
        """Test that data persists across reloads."""
        watchlist.add_watched(watched_item)
        # Create new watchlist instance pointing to same directory
        reload = Watchlist(watchlist.state_dir)
        assert len(reload.get_watched()) == 1
        assert reload.get_watched()[0].id == watched_item.id


# ---------------------------------------------------------------------------
# ChangeDetector tests
# ---------------------------------------------------------------------------


class TestChangeDetector:
    def test_detect_new_discovery(self, watchlist, watched_item):
        detector = ChangeDetector(watchlist)
        watchlist.add_watched(watched_item)

        # Previous state is empty
        changes = detector.detect_changes(
            candidates=[{
                "id": watched_item.id,
                "name": watched_item.name,
                "source_url": watched_item.url,
                "score": 85,
                "free": {"status": "open-source"},
            }],
            previous_snapshot=[],
        )
        assert len(changes) == 1
        assert changes[0].kind == "new_alternative"

    def test_detect_no_change(self, watchlist, watched_item):
        detector = ChangeDetector(watchlist)
        watchlist.add_watched(watched_item)

        current = [{
            "id": watched_item.id,
            "name": watched_item.name,
            "source_url": watched_item.url,
            "score": 85,
            "free": {"status": "open-source"},
        }]
        changes = detector.detect_changes(candidates=current, previous_snapshot=current)
        assert len(changes) == 0

    def test_detect_license_change(self, watchlist, watched_item):
        detector = ChangeDetector(watchlist)
        watchlist.add_watched(watched_item)

        previous = [{
            "id": watched_item.id,
            "name": watched_item.name,
            "license": "MIT",
            "free": {"status": "open-source"},
        }]
        current = [{
            "id": watched_item.id,
            "name": watched_item.name,
            "license": "Apache-2.0",  # Changed
            "free": {"status": "open-source"},
        }]
        changes = detector.detect_changes(candidates=current, previous_snapshot=previous)
        assert len(changes) == 1
        assert changes[0].kind == "license"

    def test_event_deduplication(self, watchlist, watched_item):
        detector = ChangeDetector(watchlist)
        watchlist.add_watched(watched_item)

        current = [{
            "id": watched_item.id,
            "name": watched_item.name,
            "license": "MIT",
        }]
        changes1 = detector.detect_changes(candidates=current, previous_snapshot=[])
        changes2 = detector.detect_changes(candidates=current, previous_snapshot=[])
        # Same changes detected twice
        assert len(changes1) == 1
        assert len(changes2) == 1
        assert changes1[0].dedup_key == changes2[0].dedup_key


# ---------------------------------------------------------------------------
# EventDeduplicator tests
# ---------------------------------------------------------------------------


class TestEventDeduplicator:
    def test_detects_duplicates(self, tmp_path, monkeypatch):
        # Use a temporary directory for seen events
        seen_file = tmp_path / "seen_events.json"
        monkeypatch.setattr(
            "intel_sources.watchlist.Path.home",
            lambda: tmp_path
        )
        dedup = EventDeduplicator(window_hours=24)
        now = datetime.now(timezone.utc).isoformat()

        event1 = ChangeEvent(
            id="evt-1",
            watched_id="item-1",
            kind="release",
            description="New version 2.0",
            previous_state={},
            new_state={},
            confidence=0.9,
            evidence_urls=[],
            detected_at=now,
            dedup_key="abc123",
        )
        event2 = ChangeEvent(
            id="evt-2",
            watched_id="item-1",
            kind="release",
            description="New version 2.0",
            previous_state={},
            new_state={},
            confidence=0.9,
            evidence_urls=[],
            detected_at=now,
            dedup_key="abc123",  # Same dedup key
        )

        assert not dedup.is_duplicate(event1)
        dedup.mark_seen(event1)
        assert dedup.is_duplicate(event2)

    def test_different_events_not_duplicates(self, tmp_path, monkeypatch):
        seen_file = tmp_path / "seen_events.json"
        monkeypatch.setattr(
            "intel_sources.watchlist.Path.home",
            lambda: tmp_path
        )
        dedup = EventDeduplicator(window_hours=24)
        now = datetime.now(timezone.utc).isoformat()

        event1 = ChangeEvent(
            id="evt-1",
            watched_id="item-1",
            kind="release",
            description="New version",
            previous_state={},
            new_state={},
            confidence=0.9,
            evidence_urls=[],
            detected_at=now,
            dedup_key="key1",
        )
        event2 = ChangeEvent(
            id="evt-2",
            watched_id="item-2",
            kind="pricing",
            description="Price changed",
            previous_state={},
            new_state={},
            confidence=0.8,
            evidence_urls=[],
            detected_at=now,
            dedup_key="key2",
        )

        assert not dedup.is_duplicate(event1)
        assert not dedup.is_duplicate(event2)


# ---------------------------------------------------------------------------
# RetryQueue tests
# ---------------------------------------------------------------------------


class TestRetryQueue:
    def test_add_and_get_due(self):
        queue = RetryQueue(Path(tempfile.mkdtemp()))
        now = datetime.now(timezone.utc)
        job = RetryJob(
            id="job-1",
            event_id="evt-1",
            channel="telegram",
            payload="test",
            attempts=0,
            max_attempts=3,
            created_at=now.isoformat(),
            next_retry_at=now.isoformat(),  # Due now
        )
        queue.add(job)
        due = queue.get_due(now)
        assert len(due) == 1
        assert due[0].id == "job-1"

    def test_increment_and_retry(self):
        queue = RetryQueue(Path(tempfile.mkdtemp()))
        now = datetime.now(timezone.utc)
        job = RetryJob(
            id="job-1",
            event_id="evt-1",
            channel="telegram",
            payload="test",
            attempts=0,
            max_attempts=3,
            created_at=now.isoformat(),
            next_retry_at=now.isoformat(),
        )
        queue.add(job)
        # First retry
        should_retry = queue.increment("job-1", error="timeout")
        assert should_retry is True
        # Check attempts incremented
        updated = queue.jobs["job-1"]
        assert updated.attempts == 1
        # Third retry should fail
        queue.increment("job-1")
        queue.increment("job-1")
        should_retry = queue.increment("job-1")
        assert should_retry is False

    def test_remove(self):
        queue = RetryQueue(Path(tempfile.mkdtemp()))
        now = datetime.now(timezone.utc)
        job = RetryJob(
            id="job-1",
            event_id="evt-1",
            channel="telegram",
            payload="test",
            attempts=0,
            max_attempts=3,
            created_at=now.isoformat(),
            next_retry_at=now.isoformat(),
        )
        queue.add(job)
        assert queue.remove("job-1")
        assert len(queue.jobs) == 0


# ---------------------------------------------------------------------------
# Notification builder tests
# ---------------------------------------------------------------------------


class TestBuildNotification:
    def test_basic_notification(self, watched_item):
        now = datetime.now(timezone.utc).isoformat()
        event = ChangeEvent(
            id="evt-1",
            watched_id=watched_item.id,
            kind="release",
            description="New version 2.0 released",
            previous_state={"version": "1.0"},
            new_state={"version": "2.0"},
            confidence=0.9,
            evidence_urls=[watched_item.url],
            detected_at=now,
        )
        msg = build_notification(event, watched_item)
        assert "Intel Change Detected" in msg
        assert watched_item.name in msg
        assert "RELEASE" in msg
        assert "90%" in msg
        assert watched_item.url in msg

    def test_notification_with_action(self, watched_item):
        now = datetime.now(timezone.utc).isoformat()
        event = ChangeEvent(
            id="evt-1",
            watched_id=watched_item.id,
            kind="pricing",
            description="Pricing tier changed",
            previous_state={},
            new_state={},
            confidence=0.8,
            evidence_urls=[],
            detected_at=now,
        )
        msg = build_notification(event, watched_item, context={
            "recommended_action": "Check new pricing at official site"
        })
        assert "Action:" in msg
        assert "Check new pricing" in msg
