"""Validated persistent settings for intel sources."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

try:
    import zoneinfo
except ImportError:
    from backports import zoneinfo

# Known settings keys - exact schema validation
KNOWN_KEYS = frozenset({
    "schema_version",
    "sources",
    "topics",
    "priorities",
    "categories",
    "max_items",
    "max_age_days",
    "min_score",
    "schedule",
})

# Valid categories per contract (order matters for default_settings)
VALID_CATEGORIES = ("free-api", "models", "agents", "mcp", "skills", "devtools", "news")

# Required source fields
REQUIRED_SOURCE_FIELDS = frozenset({"id", "name", "adapter", "enabled"})
OPTIONAL_SOURCE_FIELDS = frozenset({"url", "repo", "query"})

# Portable built-in default sources - four daily adapters from daily_sources.py
# These are used when no config/sources.json file is found or is invalid
_PORTABLE_DEFAULT_SOURCES = (
    {"id": "openrouter-models", "name": "OpenRouter Models", "adapter": "catalog", "enabled": True, "url": "https://openrouter.ai/api/v1/models"},
    {"id": "github-trending", "name": "GitHub Trending", "adapter": "github", "enabled": True, "url": "https://github.com/trending?since=daily"},
    {"id": "skills-hot", "name": "Skills Hot", "adapter": "skills", "enabled": True, "url": "https://skills.sh/trending"},
    {"id": "unorouter-pricing", "name": "UnoRouter Pricing", "adapter": "catalog", "enabled": True, "url": "https://api.unorouter.com/api/pricing/catalog"},
)


def validate_source(source: dict[str, Any]) -> dict[str, Any]:
    """Validate a single source configuration."""
    if not isinstance(source, dict):
        raise ValueError("source must be a dict")
    
    # Check required fields
    for field in REQUIRED_SOURCE_FIELDS:
        if field not in source:
            raise ValueError(f"source missing required field: {field}")
    
    # Validate types
    if not isinstance(source["id"], str) or not source["id"]:
        raise ValueError("source id must be non-empty string")
    if not isinstance(source["name"], str) or not source["name"]:
        raise ValueError("source name must be non-empty string")
    if not isinstance(source["adapter"], str) or not source["adapter"]:
        raise ValueError("source adapter must be non-empty string")
    if not isinstance(source["enabled"], bool):
        raise ValueError("source enabled must be bool")
    
    # Validate optional fields
    for field in OPTIONAL_SOURCE_FIELDS:
        if field in source and not isinstance(source[field], str):
            raise ValueError(f"source {field} must be string")
    
    # Reject unknown fields
    all_allowed = REQUIRED_SOURCE_FIELDS | OPTIONAL_SOURCE_FIELDS
    for key in source:
        if key not in all_allowed:
            raise ValueError(f"source unknown field: {key}")
    
    return source


def validate_sources(sources: Any) -> list[dict[str, Any]]:
    """Validate sources list."""
    if not isinstance(sources, list):
        raise ValueError("sources must be a list")
    return [validate_source(s) for s in sources]


def validate_priorities(priorities: Any) -> dict[str, int]:
    """Validate priorities dict with bounds 0..5."""
    if not isinstance(priorities, dict):
        raise ValueError("priorities must be a dict")
    result = {}
    for category, value in priorities.items():
        if not isinstance(category, str):
            raise ValueError("priority key must be string")
        if category not in VALID_CATEGORIES:
            raise ValueError(f"unknown priority category: {category}")
        if not isinstance(value, int) or not (0 <= value <= 5):
            raise ValueError(f"priority for {category} must be int 0..5")
        result[category] = value
    return result


def validate_categories(categories: Any) -> list[str]:
    """Validate categories list."""
    if not isinstance(categories, list):
        raise ValueError("categories must be a list")
    for cat in categories:
        if not isinstance(cat, str) or cat not in VALID_CATEGORIES:
            raise ValueError(f"invalid category: {cat}")
    return list(categories)


def validate_topics(topics: Any) -> list[str]:
    """Validate topics list - bounded strings."""
    if not isinstance(topics, list):
        raise ValueError("topics must be a list")
    result = []
    for topic in topics:
        if not isinstance(topic, str) or not topic or len(topic) > 64:
            raise ValueError("topic must be non-empty string <= 64 chars")
        result.append(topic)
    return result


def validate_max_items(value: Any) -> int:
    """Validate max_items 1..10."""
    if not isinstance(value, int) or not (1 <= value <= 10):
        raise ValueError("max_items must be int 1..10")
    return value


def validate_max_age_days(value: Any) -> int:
    """Validate max_age_days 1..30."""
    if not isinstance(value, int) or not (1 <= value <= 30):
        raise ValueError("max_age_days must be int 1..30")
    return value


def validate_min_score(value: Any) -> int:
    """Validate min_score 0..100."""
    if not isinstance(value, int) or not (0 <= value <= 100):
        raise ValueError("min_score must be int 0..100")
    return value


def validate_schedule(schedule: Any) -> dict[str, Any]:
    """Validate schedule with timezone, hour, minute."""
    if not isinstance(schedule, dict):
        raise ValueError("schedule must be a dict")
    
    # Validate timezone
    tz = schedule.get("timezone", "Africa/Cairo")
    if not isinstance(tz, str):
        raise ValueError("schedule timezone must be string")
    try:
        zoneinfo.ZoneInfo(tz)
    except Exception:
        raise ValueError(f"invalid timezone: {tz}")
    
    # Validate hour
    hour = schedule.get("hour", 10)
    if not isinstance(hour, int) or not (0 <= hour <= 23):
        raise ValueError("schedule hour must be int 0..23")
    
    # Validate minute
    minute = schedule.get("minute", 0)
    if not isinstance(minute, int) or not (0 <= minute <= 59):
        raise ValueError("schedule minute must be int 0..59")
    
    return {"timezone": tz, "hour": hour, "minute": minute}


def validate_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Full settings validation with exact schema.

    Schema-only validator: ``sources`` defaults to ``[]`` when not provided.
    Portable built-in defaults (four daily adapters) are applied only by the
    loader in ``default_settings()``; explicit ``sources`` lists, including
    ``[]`` and disabled sources, are preserved exactly.
    """
    if not isinstance(settings, dict):
        raise ValueError("settings must be a dict")
    
    # Reject unknown keys
    for key in settings:
        if key not in KNOWN_KEYS:
            raise ValueError(f"unknown settings key: {key}")
    
    # Validate schema_version
    version = settings.get("schema_version", 1)
    if not isinstance(version, int) or version != 1:
        raise ValueError("schema_version must be 1")
    
    # Validate each field with defaults
    result = {"schema_version": 1}
    
    if "sources" in settings:
        result["sources"] = validate_sources(settings["sources"])
    else:
        # Schema-only: missing sources validates to [] (portable fallback is loader-only)
        result["sources"] = []
    
    if "topics" in settings:
        result["topics"] = validate_topics(settings["topics"])
    else:
        result["topics"] = []
    
    if "priorities" in settings:
        result["priorities"] = validate_priorities(settings["priorities"])
    else:
        result["priorities"] = {cat: 0 for cat in VALID_CATEGORIES}
    
    if "categories" in settings:
        result["categories"] = validate_categories(settings["categories"])
    else:
        result["categories"] = list(VALID_CATEGORIES)
    
    if "max_items" in settings:
        result["max_items"] = validate_max_items(settings["max_items"])
    else:
        result["max_items"] = 5
    
    if "max_age_days" in settings:
        result["max_age_days"] = validate_max_age_days(settings["max_age_days"])
    else:
        result["max_age_days"] = 7
    
    if "min_score" in settings:
        result["min_score"] = validate_min_score(settings["min_score"])
    else:
        result["min_score"] = 60
    
    if "schedule" in settings:
        result["schedule"] = validate_schedule(settings["schedule"])
    else:
        result["schedule"] = {"timezone": "Africa/Cairo", "hour": 10, "minute": 0}
    
    return result


def default_settings(config_dir: str | None = None) -> dict[str, Any]:
    """Return default settings, optionally loading sources from config/sources.json.
    
    Falls back to built-in portable defaults (four daily adapters) when:
    - config_dir is None or doesn't exist
    - config/sources.json is missing
    - config/sources.json is invalid JSON or fails validation
    """
    # Start with validated defaults (includes all non-sources fields)
    defaults = validate_settings({})

    # Apply portable built-in defaults as the fallback source list
    defaults["sources"] = [dict(s) for s in _PORTABLE_DEFAULT_SOURCES]

    # Apply approved selection-intent defaults (conservative public defaults)
    # These translate the human selection intent into working defaults so that
    # high-quality records can reach min_score=60 without relaxing gates.
    defaults["topics"] = ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"]
    defaults["priorities"] = {
        "free-api": 5,
        "models": 4,
        "agents": 3,
        "mcp": 2,
        "skills": 2,
        "devtools": 1,
        "news": 1,
    }

    # Auto-discover config directory relative to project root if not provided
    if config_dir is None:
        project_root = Path(__file__).parent.parent.parent
        config_dir = str(project_root / "config")

    # Load sources from config file if provided - overrides portable defaults
    if config_dir:
        sources_path = Path(config_dir) / "sources.json"
        if sources_path.exists():
            try:
                with sources_path.open("r", encoding="utf-8") as f:
                    sources_data = json.load(f)
                # Preserve the entire configured selection policy. A bare list
                # remains supported for legacy sources-only configuration.
                if isinstance(sources_data, dict):
                    return validate_settings(sources_data)
                defaults["sources"] = validate_sources(sources_data)
            except (json.JSONDecodeError, ValueError):
                # Keep portable defaults on config error
                pass

    return defaults


def load_settings(path: str | Path | None) -> dict[str, Any]:
    """Load and validate settings from JSON file, raising on invalid data."""
    if path is None:
        return default_settings()
    path = Path(path)
    if not path.exists():
        return default_settings()
    
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default_settings()
    
    return validate_settings(data)


def save_settings(path: str | Path, value: dict[str, Any]) -> dict[str, Any]:
    """Validate and atomically save settings to JSON file."""
    path = Path(path)
    validated = validate_settings(value)
    
    # Atomic write
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False
        ) as handle:
            temp_path = Path(handle.name)
            json.dump(validated, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    
    return validated
