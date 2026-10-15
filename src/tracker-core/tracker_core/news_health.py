"""Internal news-health projection: fixed identities, current outcomes, separate history."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

SOURCES = frozenset({"arxiv", "github", "hackernews", "huggingface", "reddit", "rss", "youtube"})
STATUSES = frozenset({"healthy", "partial", "failed", "cooldown", "not_attempted"})
FAILURE_CODES = frozenset({
    "timeout", "collection_deadline", "transport_failure", "fetch_failure",
    "response_too_large", "invalid_items", "invalid_feed", "invalid_repository_list",
    "channel_not_found", "uploads_playlist_missing", "invalid_video_item",
})


def _failure_code(value: Any) -> str | None:
    if isinstance(value, str) and (value in FAILURE_CODES or re.fullmatch(r"http_[1-5][0-9]{2}", value)):
        return value
    return None


def _count(value: Any) -> int | None:
    return value if type(value) is int and 0 <= value <= 10**12 else None


def _identity(value: Any) -> bool:
    return isinstance(value, str) and (value in SOURCES or
        re.fullmatch(r"(?:arxiv|github|hackernews|huggingface|reddit|rss|youtube):[0-9a-f]{16}", value) is not None)


def project_news_health(rows: Any) -> list[dict[str, Any]]:
    """Reject unsafe fields rather than passing restored strings to Jinja."""
    from .reporter_html import _news_timestamp

    projected = []
    if not isinstance(rows, list):
        return projected
    for row in rows:
        if not isinstance(row, dict) or not _identity(row.get("source")):
            continue
        outcome = row.get("current")
        outcome = outcome if isinstance(outcome, dict) else {}
        status = outcome.get("status")
        status = status if isinstance(status, str) and status in STATUSES else "unknown"
        history = row.get("history")
        history = history if isinstance(history, dict) else {}
        cooldown_until = _news_timestamp(history.get("cooldown_until"))
        as_of = _news_timestamp(row.get("as_of"))
        now = datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
        cooldown = ("pending" if datetime.fromisoformat(cooldown_until) > now
                    else "expired — recovery not observed") if cooldown_until else "none recorded"
        projected.append({
            "source": row["source"],
            "kind": "feed" if ":" in row["source"] else "source",
            "status": status,
            "items": _count(outcome.get("items")) if status != "unknown" else None,
            "total_items": _count(history.get("total_items")),
            "consecutive_failures": _count(history.get("consecutive_failures")),
            "last_success": _news_timestamp(history.get("last_success")),
            "last_failure": _news_timestamp(history.get("last_failure")),
            "cooldown_until": cooldown_until,
            "cooldown": cooldown,
            "current_error": _failure_code(outcome.get("error")) if status in {"failed", "partial"} else None,
            "historical_error": _failure_code(history.get("last_error")),
        })
    return sorted(projected, key=lambda row: row["source"])


def aggregate_news_outcomes(results: dict) -> dict[str, Any]:
    """Summarize observed feed outcomes; surviving partials are never failures."""
    outcomes = list(results.values())
    statuses = [r.get("status") if isinstance(r, dict) else None for r in outcomes]
    if not statuses or any(not isinstance(s, str) or s not in STATUSES for s in statuses):
        return {"status": "unknown"}
    if all(s == "healthy" for s in statuses):
        status = "healthy"
    elif any(s in {"healthy", "partial"} for s in statuses):
        status = "partial"
    else:
        status = "failed" if "failed" in statuses else "cooldown" if "cooldown" in statuses else "not_attempted"
    counts = [_count(r.get("items")) for r in outcomes]
    return {"status": status, "items": sum(counts) if all(c is not None for c in counts) else None}


def build_news_health(source_history: dict, feed_history: dict, feed_results: dict, *,
                      source_results: dict | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """Join current adapter outcomes to history without promoting history to current."""
    rows = []
    for source, results in feed_results.items():
        if source not in SOURCES or not isinstance(results, dict):
            continue
        if results:
            rows.append({"source": source, "current": aggregate_news_outcomes(results),
                "history": source_history.get(source, {})})
        for key, outcome in results.items():
            if _identity(key) and key.startswith(source + ":"):
                rows.append({"source": key, "current": outcome, "history": feed_history.get(key, {})})
    for source, outcome in (source_results or {}).items():
        if source not in SOURCES:
            continue
        rows = [row for row in rows if row["source"] != source]
        rows.append({"source": source, "current": outcome, "history": source_history.get(source, {})})
    present = {row["source"] for row in rows}
    for history in (source_history, feed_history):
        for key, value in history.items():
            if _identity(key) and key not in present:
                rows.append({"source": key, "history": value})
                present.add(key)
    for row in rows:
        row["as_of"] = now
    return rows
