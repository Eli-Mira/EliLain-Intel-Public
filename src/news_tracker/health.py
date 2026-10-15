"""Source health tracking — circuit breaker pattern.

If a source fails N consecutive times, auto-disable it for the current run
and flag it in the health table instead of retrying indefinitely.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from pathlib import Path
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


logger = logging.getLogger(__name__)


def _safe_error(error: Any) -> str | None:
    if error is None:
        return None
    if isinstance(error, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", error):
        return error
    return "fetch_failure"


@dataclass
class SourceHealth:
    """Health record for a single source."""
    name: str
    consecutive_failures: int = 0
    last_success: datetime | None = None
    last_failure: datetime | None = None
    is_disabled: bool = False
    total_items: int = 0
    last_error: str | None = None
    cooldown_until: datetime | None = None

    @property
    def is_healthy(self) -> bool:
        return not self.is_disabled and self.consecutive_failures == 0 and self.last_success is not None


class SourceHealthTracker:
    """Track health of all sources — circuit breaker for flaky ones."""

    def __init__(self, failure_threshold: int = 3, state_path: str | None = None):
        """
        Args:
            failure_threshold: Consecutive failures before disabling a source
            state_path: Path to JSON file for persisting health state
        """
        self.failure_threshold = failure_threshold
        self.state_path = state_path
        self._sources: dict[str, SourceHealth] = {}
        self.cooldown_seconds = 300.0
        self._now = lambda: datetime.now(timezone.utc)

    def get_or_create(self, source_name: str) -> SourceHealth:
        """Get or create health record for a source."""
        if source_name not in self._sources:
            self._sources[source_name] = SourceHealth(name=source_name)
        return self._sources[source_name]

    def is_source_enabled(self, source_name: str) -> bool:
        """Check if a source is currently enabled (not circuit-broken)."""
        source = self.get_or_create(source_name)
        if source.is_disabled and source.cooldown_until is not None and self._now() >= source.cooldown_until:
            source.is_disabled = False
            source.cooldown_until = None
        return not source.is_disabled

    def record_success(self, source_name: str, item_count: int = 0) -> None:
        """Record a successful fetch — resets failure counter."""
        source = self.get_or_create(source_name)
        source.consecutive_failures = 0
        source.last_success = self._now()
        source.is_disabled = False
        source.last_error = None
        source.cooldown_until = None
        source.total_items += item_count
        logger.info(f"Source {source_name}: OK ({item_count} items)")

    def record_failure(self, source_name: str, error: str | None = None, *, retry_after_seconds: float | None = None) -> bool:
        """
        Record a failed fetch.

        Returns:
            True if source is now disabled (circuit broken)
        """
        error = _safe_error(error)
        source = self.get_or_create(source_name)
        source.consecutive_failures += 1
        source.last_failure = self._now()
        source.last_error = error

        if source.consecutive_failures >= self.failure_threshold or retry_after_seconds is not None:
            source.is_disabled = True
            source.cooldown_until = source.last_failure + timedelta(seconds=self.cooldown_seconds if retry_after_seconds is None else retry_after_seconds)
            logger.warning(
                f"Source {source_name}: CIRCUIT BROKEN after "
                f"{source.consecutive_failures} consecutive failures. "
                f"Last error: {error}"
            )
            return True

        logger.warning(
            f"Source {source_name}: failure {source.consecutive_failures}/"
            f"{self.failure_threshold}"
        )
        return False

    def get_health_table(self) -> list[dict[str, Any]]:
        """Get health status for all sources — for display/reporting."""
        table = []
        for source in self._sources.values():
            self.is_source_enabled(source.name)
            table.append({
                "source": source.name,
                "status": "disabled" if source.is_disabled else "failed" if source.consecutive_failures else "healthy" if source.last_success else "unknown",
                "consecutive_failures": source.consecutive_failures,
                "last_success": source.last_success.isoformat() if source.last_success else None,
                "last_failure": source.last_failure.isoformat() if source.last_failure else None,
                "total_items": source.total_items,
                "last_error": source.last_error,
                "cooldown_until": source.cooldown_until.isoformat() if source.cooldown_until else None,
            })

        # Sort: disabled first, then by most failures
        table.sort(key=lambda x: (x["status"] != "disabled", x["consecutive_failures"]), reverse=True)
        return table

    def get_disabled_sources(self) -> list[str]:
        """Get list of currently disabled source names."""
        return [s.name for s in self._sources.values() if s.is_disabled]

    def get_healthy_sources(self) -> list[str]:
        """Get list of currently healthy source names."""
        return [s.name for s in self._sources.values() if s.is_healthy]

    def load_state(self) -> None:
        """Load optional per-feed state; never permanently disable a legacy record."""
        if not self.state_path or not Path(self.state_path).exists():
            return
        try:
            data = json.loads(Path(self.state_path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("invalid health state")
        except (ValueError, TypeError, OSError):
            logger.warning("Could not load feed health state; no cooldown restored")
            return
        for name, raw in data.items():
            try:
                if not isinstance(raw, dict):
                    raise ValueError("invalid health record")
                values = dict(raw)
                values["name"] = name
                for key in ("consecutive_failures", "total_items"):
                    value = values.get(key, 0)
                    if type(value) is not int or value < 0:
                        raise ValueError("invalid health count")
                if type(values.get("is_disabled", False)) is not bool:
                    raise ValueError("invalid disabled flag")
                for key in ("last_success", "last_failure", "cooldown_until"):
                    value = values.get(key)
                    values[key] = datetime.fromisoformat(value) if value else None
                    if values[key] is not None and values[key].tzinfo is None:
                        raise ValueError("naive observation time")
                if values.get("is_disabled") and not values.get("cooldown_until"):
                    values["is_disabled"] = False
                values["last_error"] = _safe_error(values.get("last_error"))
                source = SourceHealth(**values)
            except (ValueError, TypeError, AttributeError):
                logger.warning("Invalid feed health record skipped; valid siblings retained")
                continue
            self._sources[name] = source

    def save_state(self) -> None:
        """Atomically save optional state with safe failure classifications only."""
        if not self.state_path:
            return
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        for name, source in self._sources.items():
            values = asdict(source)
            for key in ("last_success", "last_failure", "cooldown_until"):
                values[key] = values[key].isoformat() if values[key] else None
            values["last_error"] = _safe_error(values.get("last_error"))
            data[name] = values
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(data, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def reset_source(self, source_name: str) -> None:
        """Reset failure count for a source (e.g., after manual intervention)."""
        source = self.get_or_create(source_name)
        source.consecutive_failures = 0
        source.is_disabled = False
        logger.info(f"Source {source_name}: manually reset")
        source.last_error = None
        source.cooldown_until = None

    def reset_all(self) -> None:
        """Reset all sources to healthy."""
        for source in self._sources.values():
            source.consecutive_failures = 0
            source.is_disabled = False
            source.last_error = None
            source.cooldown_until = None
        logger.info("All sources reset to healthy")
