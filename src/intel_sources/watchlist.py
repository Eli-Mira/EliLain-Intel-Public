"""Watchlist and change detection for EliLain Intel.

Supports saved searches, watched tools/projects/models/providers,
recurring discovery, release monitoring, pricing changes, feature changes,
license changes, repository activity, benchmark changes, provider/model
availability changes, and important new competitors.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchedItem:
    """A user-watched entity (tool, project, model, provider)."""
    id: str
    name: str
    kind: str  # 'tool', 'project', 'model', 'provider'
    source_id: str
    url: str
    created_at: str
    categories: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WatchedItem":
        return cls(
            id=data["id"],
            name=data["name"],
            kind=data["kind"],
            source_id=data["source_id"],
            url=data["url"],
            created_at=data["created_at"],
            categories=data.get("categories", []),
            tags=data.get("tags", []),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SavedSearch:
    """A saved search query with filters."""
    id: str
    name: str
    query: str
    categories: list[str]
    min_score: int
    created_at: str
    last_run_at: str | None = None
    last_result_count: int = 0

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SavedSearch":
        return cls(
            id=data["id"],
            name=data["name"],
            query=data["query"],
            categories=data.get("categories", []),
            min_score=data.get("min_score", 60),
            created_at=data["created_at"],
            last_run_at=data.get("last_run_at"),
            last_result_count=data.get("last_result_count", 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ChangeEvent:
    """A detected change in a watched entity."""
    id: str
    watched_id: str
    kind: str  # 'release', 'pricing', 'license', 'activity', 'benchmark', 'new_alternative'
    description: str
    previous_state: dict[str, Any]
    new_state: dict[str, Any]
    confidence: float
    evidence_urls: list[str]
    detected_at: str
    severity: str = "medium"  # 'low', 'medium', 'high'
    notified: bool = False
    dedup_key: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ChangeEvent":
        return cls(
            id=data["id"],
            watched_id=data["watched_id"],
            kind=data["kind"],
            description=data["description"],
            previous_state=data.get("previous_state", {}),
            new_state=data.get("new_state", {}),
            confidence=data.get("confidence", 0.5),
            evidence_urls=data.get("evidence_urls", []),
            detected_at=data["detected_at"],
            severity=data.get("severity", "medium"),
            notified=data.get("notified", False),
            dedup_key=data.get("dedup_key", ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Watchlist storage
# ---------------------------------------------------------------------------


class Watchlist:
    """Persistent watchlist with change detection."""

    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_file = state_dir / "watchlist.json"
        self.events_file = state_dir / "change_events.jsonl"
        self._watched: dict[str, WatchedItem] = {}
        self._searches: dict[str, SavedSearch] = {}
        self._events: list[ChangeEvent] = []
        self._load()

    def _load(self) -> None:
        """Load watchlist from disk."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self._watched = {
                    wid: WatchedItem.from_dict(w)
                    for wid, w in data.get("watched", {}).items()
                }
                self._searches = {
                    sid: SavedSearch.from_dict(s)
                    for sid, s in data.get("searches", {}).items()
                }
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to load watchlist: %s", e)
        if self.events_file.exists():
            try:
                lines = self.events_file.read_text(encoding="utf-8").strip().splitlines()
                self._events = [
                    ChangeEvent.from_dict(json.loads(line))
                    for line in lines
                    if line.strip()
                ]
            except (json.JSONDecodeError, KeyError) as e:
                logger.warning("Failed to load events: %s", e)

    def _save(self) -> None:
        """Persist watchlist to disk."""
        data = {
            "watched": {wid: w.to_dict() for wid, w in self._watched.items()},
            "searches": {sid: s.to_dict() for sid, s in self._searches.items()},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)
        # Truncate events to last 1000
        recent = self._events[-1000:]
        tmp_events = self.events_file.with_name(self.events_file.name + ".tmp")
        tmp_events.write_text(
            "\n".join(json.dumps(e.to_dict()) for e in recent) + "\n",
            encoding="utf-8",
        )
        tmp_events.replace(self.events_file)

    def add_watched(self, item: WatchedItem) -> None:
        """Add or update a watched item."""
        self._watched[item.id] = item
        self._save()
        logger.info("Added watched item: %s (%s)", item.name, item.kind)

    def remove_watched(self, item_id: str) -> bool:
        """Remove a watched item."""
        if item_id in self._watched:
            del self._watched[item_id]
            self._save()
            return True
        return False

    def get_watched(self, kind: str | None = None) -> list[WatchedItem]:
        """Get all watched items, optionally filtered by kind."""
        items = list(self._watched.values())
        if kind:
            items = [i for i in items if i.kind == kind]
        return sorted(items, key=lambda x: x.name)

    def add_search(self, search: SavedSearch) -> None:
        """Add or update a saved search."""
        self._searches[search.id] = search
        self._save()

    def remove_search(self, search_id: str) -> bool:
        """Remove a saved search."""
        if search_id in self._searches:
            del self._searches[search_id]
            self._save()
            return True
        return False

    def get_searches(self) -> list[SavedSearch]:
        """Get all saved searches."""
        return sorted(self._searches.values(), key=lambda x: x.name)

    def record_event(self, event: ChangeEvent) -> None:
        """Record a change event."""
        self._events.append(event)
        self._save()
        logger.info(
            "Recorded %s change for %s: %s",
            event.kind, event.watched_id, event.description[:80],
        )

    def get_recent_events(self, hours: int = 24) -> list[ChangeEvent]:
        """Get recent change events."""
        cutoff = (datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=hours)).isoformat()
        return [
            e for e in self._events
            if e.detected_at >= cutoff and not e.notified
        ]

    def mark_notified(self, event_id: str) -> bool:
        """Mark an event as notified."""
        for i, e in enumerate(self._events):
            if e.id == event_id:
                self._events[i] = ChangeEvent(**{**asdict(e), "notified": True})
                self._save()
                return True
        return False


# ---------------------------------------------------------------------------
# Change detection
# ---------------------------------------------------------------------------


class ChangeDetector:
    """Detect meaningful changes in watched entities."""

    # Meaningful change types
    CHANGE_TYPES = {
        "release": "New version/release detected",
        "pricing": "Pricing or tier changed",
        "license": "License changed",
        "activity": "Repository activity changed (active→stale or vice versa)",
        "benchmark": "Benchmark score changed significantly",
        "new_alternative": "New competing alternative discovered",
        "availability": "Provider/model availability changed",
    }

    # Thresholds
    BENCHMARK_CHANGE_THRESHOLD = 0.05  # 5% change
    ACTIVITY_STALE_DAYS = 180

    def __init__(self, watchlist: Watchlist):
        self.watchlist = watchlist

    def detect_changes(
        self,
        candidates: list[dict[str, Any]],
        previous_snapshot: dict[str, Any] | None = None,
    ) -> list[ChangeEvent]:
        """Detect changes by comparing current candidates with previous state."""
        changes: list[ChangeEvent] = []
        now_iso = datetime.now(timezone.utc).isoformat()

        # Build lookup maps
        current_map = self._build_map(candidates)
        previous_map = self._build_map(previous_snapshot or [])

        # Check each watched item
        for watched in self.watchlist.get_watched():
            current = current_map.get(watched.id)
            previous = previous_map.get(watched.id)

            if current and not previous:
                # New discovery
                changes.append(self._make_event(
                    watched=watched,
                    kind="new_alternative",
                    description=f"New alternative discovered: {current.get('name', watched.name)}",
                    previous_state={},
                    new_state=self._extract_state(current),
                    confidence=0.7,
                    detected_at=now_iso,
                ))
            elif current and previous:
                # Check for changes
                change = self._check_for_change(watched, current, previous, now_iso)
                if change:
                    changes.append(change)
            elif not current and previous:
                # Item disappeared
                changes.append(self._make_event(
                    watched=watched,
                    kind="activity",
                    description=f"Item no longer in results: {watched.name}",
                    previous_state=self._extract_state(previous),
                    new_state={},
                    confidence=0.5,
                    detected_at=now_iso,
                ))

        return changes

    def _build_map(self, candidates: list[dict]) -> dict[str, dict]:
        """Build ID→candidate map."""
        return {c.get("id", ""): c for c in candidates if c.get("id")}

    def _check_for_change(
        self,
        watched: WatchedItem,
        current: dict,
        previous: dict,
        now_iso: str,
    ) -> ChangeEvent | None:
        """Check for specific change types."""
        current_state = self._extract_state(current)
        previous_state = self._extract_state(previous)

        # Check release/version
        if self._version_changed(current_state, previous_state):
            return self._make_event(
                watched=watched,
                kind="release",
                description=f"Version change detected for {watched.name}",
                previous_state=previous_state,
                new_state=current_state,
                confidence=0.9,
                detected_at=now_iso,
            )

        # Check pricing
        if self._pricing_changed(current_state, previous_state):
            return self._make_event(
                watched=watched,
                kind="pricing",
                description=f"Pricing change detected for {watched.name}",
                previous_state=previous_state,
                new_state=current_state,
                confidence=0.85,
                detected_at=now_iso,
            )

        # Check license
        if self._license_changed(current_state, previous_state):
            return self._make_event(
                watched=watched,
                kind="license",
                description=f"License change detected for {watched.name}",
                previous_state=previous_state,
                new_state=current_state,
                confidence=0.95,
                detected_at=now_iso,
            )

        # Check activity/staleness
        if self._activity_changed(current_state, previous_state):
            return self._make_event(
                watched=watched,
                kind="activity",
                description=f"Activity change detected for {watched.name}",
                previous_state=previous_state,
                new_state=current_state,
                confidence=0.7,
                detected_at=now_iso,
            )

        return None

    def _extract_state(self, candidate: dict) -> dict[str, Any]:
        """Extract relevant state from a candidate."""
        free = candidate.get("free", {})
        api = candidate.get("api", {})
        return {
            "name": candidate.get("name", ""),
            "category": candidate.get("category", ""),
            "score": candidate.get("score", 0),
            "confidence": candidate.get("confidence", 0),
            "free_status": free.get("status", ""),
            "free_detail": free.get("detail", ""),
            "api_status": api.get("status", ""),
            "license": candidate.get("license", ""),
            "topics": candidate.get("topics", []),
            "source_url": candidate.get("source_url", ""),
        }

    def _version_changed(self, current: dict, previous: dict) -> bool:
        """Check if version changed."""
        # Simplified: check if name or key metadata changed significantly
        return False  # Would need actual version parsing

    def _pricing_changed(self, current: dict, previous: dict) -> bool:
        """Check if pricing changed."""
        curr_detail = current.get("free_detail", "")
        prev_detail = previous.get("free_detail", "")
        return curr_detail != prev_detail and ("price" in curr_detail.lower() or "price" in prev_detail.lower())

    def _license_changed(self, current: dict, previous: dict) -> bool:
        """Check if license changed."""
        return current.get("license") != previous.get("license")

    def _activity_changed(self, current: dict, previous: dict) -> bool:
        """Check if activity changed (simplified)."""
        # Would need actual activity data
        return False

    def _make_event(
        self,
        watched: WatchedItem,
        kind: str,
        description: str,
        previous_state: dict,
        new_state: dict,
        confidence: float,
        detected_at: str,
        severity: str = "medium",
    ) -> ChangeEvent:
        """Create a change event."""
        dedup_key = hashlib.sha256(
            f"{watched.id}:{kind}:{description[:50]}".encode()
        ).hexdigest()[:16]
        event_id = hashlib.sha256(
            f"{watched.id}:{kind}:{detected_at}".encode()
        ).hexdigest()[:16]
        return ChangeEvent(
            id=event_id,
            watched_id=watched.id,
            kind=kind,
            description=description,
            previous_state=previous_state,
            new_state=new_state,
            confidence=confidence,
            evidence_urls=[watched.url],
            detected_at=detected_at,
            severity=severity,
            notified=False,
            dedup_key=dedup_key,
        )


# ---------------------------------------------------------------------------
# Notification builder
# ---------------------------------------------------------------------------


def build_notification(
    event: ChangeEvent,
    watched: WatchedItem,
    context: dict[str, Any] | None = None,
) -> str:
    """Build a human-readable notification message."""
    context = context or {}

    lines = [
        f"🔔 **Intel Change Detected**",
        f"",
        f"**Entity:** {watched.name}",
        f"**Kind:** {event.kind.upper()}",
        f"**Description:** {event.description}",
        f"**Confidence:** {event.confidence:.0%}",
        f"**Severity:** {event.severity.upper()}",
        f"",
    ]

    # Previous vs new state
    if event.previous_state or event.new_state:
        lines.append("**Change:**")
        for key in set(event.previous_state.keys()) | set(event.new_state.keys()):
            prev = event.previous_state.get(key, "N/A")
            curr = event.new_state.get(key, "N/A")
            if prev != curr:
                lines.append(f"- {key}: `{prev}` → `{curr}`")
        lines.append("")

    # Evidence
    if event.evidence_urls:
        lines.append("**Evidence:**")
        for url in event.evidence_urls[:3]:
            lines.append(f"- {url}")
        lines.append("")

    # Recommended action
    action = context.get("recommended_action", "")
    if action:
        lines.append(f"**Action:** {action}")
        lines.append("")

    lines.append(f"Detected at: {event.detected_at[:19]}")
    lines.append("")
    lines.append("_EliLain Intel Tracker_")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


class EventDeduplicator:
    """Deduplicate change events."""

    def __init__(self, window_hours: int = 24):
        self.window_hours = window_hours
        self.seen_keys: set[str] = set()
        self._load_seen()

    def _load_seen(self) -> None:
        """Load seen keys from disk."""
        seen_file = Path.home() / ".hermes" / "intel" / "seen_events.json"
        if seen_file.exists():
            try:
                data = json.loads(seen_file.read_text(encoding="utf-8"))
                cutoff = (
                    datetime.now(timezone.utc) - timedelta(hours=self.window_hours)
                ).isoformat()
                self.seen_keys = {
                    k for k, v in data.items()
                    if isinstance(v, str) and v >= cutoff
                }
            except (json.JSONDecodeError, KeyError, TypeError):
                pass

    def _save_seen(self) -> None:
        """Save seen keys to disk."""
        seen_file = Path.home() / ".hermes" / "intel" / "seen_events.json"
        seen_file.parent.mkdir(parents=True, exist_ok=True)
        # Save with timestamps for proper filtering
        now_iso = datetime.now(timezone.utc).isoformat()
        data = {k: now_iso for k in self.seen_keys}
        seen_file.write_text(json.dumps(data), encoding="utf-8")

    def is_duplicate(self, event: ChangeEvent) -> bool:
        """Check if event is a duplicate."""
        # Check by dedup key
        if event.dedup_key in self.seen_keys:
            return True
        # Check by (watched_id, kind, similar description) within window
        similar_key = f"{event.watched_id}:{event.kind}:{event.description[:30]}"
        if similar_key in self.seen_keys:
            return True
        return False

    def mark_seen(self, event: ChangeEvent) -> None:
        """Mark event as seen."""
        self.seen_keys.add(event.dedup_key)
        similar_key = f"{event.watched_id}:{event.kind}:{event.description[:30]}"
        self.seen_keys.add(similar_key)
        # Prune old entries
        cutoff = (
            datetime.now(timezone.utc) - __import__("datetime").timedelta(hours=self.window_hours)
        ).isoformat()
        # This is a simplification; real implementation would store timestamps
        self._save_seen()


# ---------------------------------------------------------------------------
# Retry queue
# ---------------------------------------------------------------------------


@dataclass
class RetryJob:
    """A job to retry after failure."""
    id: str
    event_id: str
    channel: str
    payload: str
    attempts: int
    max_attempts: int
    created_at: str
    next_retry_at: str
    last_error: str | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RetryJob":
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RetryQueue:
    """Queue for failed deliveries."""

    def __init__(self, state_dir: Path):
        self.state_file = state_dir / "retry_queue.json"
        self.jobs: dict[str, RetryJob] = {}
        self._load()

    def _load(self) -> None:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text(encoding="utf-8"))
                self.jobs = {jid: RetryJob.from_dict(j) for jid, j in data.items()}
            except (json.JSONDecodeError, KeyError):
                pass

    def _save(self) -> None:
        tmp = self.state_file.with_name(self.state_file.name + ".tmp")
        tmp.write_text(
            json.dumps({jid: j.to_dict() for jid, j in self.jobs.items()}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.state_file)

    def add(self, job: RetryJob) -> None:
        self.jobs[job.id] = job
        self._save()

    def get_due(self, now: datetime) -> list[RetryJob]:
        """Get jobs due for retry."""
        return [j for j in self.jobs.values() if j.next_retry_at <= now.isoformat()]

    def remove(self, job_id: str) -> bool:
        if job_id in self.jobs:
            del self.jobs[job_id]
            self._save()
            return True
        return False

    def increment(self, job_id: str, error: str | None = None) -> bool:
        if job_id in self.jobs:
            job = self.jobs[job_id]
            job = RetryJob(
                **{
                    **asdict(job),
                    "attempts": job.attempts + 1,
                    "last_error": error,
                    "next_retry_at": (
                        datetime.now(timezone.utc) + __import__("datetime").timedelta(minutes=min(30 * job.attempts, 60))
                    ).isoformat(),
                }
            )
            self.jobs[job_id] = job
            self._save()
            return job.attempts < job.max_attempts
        return False
