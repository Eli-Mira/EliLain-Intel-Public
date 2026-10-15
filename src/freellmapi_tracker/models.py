"""Data models for free LLM provider tracking."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone
from enum import Enum
from typing import Any


def aware_utc(value: datetime | str | None) -> datetime | None:
    """Legacy naive timestamps were UTC; never replace a missing time with now."""
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def sanitize_evidence(value: Any, secrets: tuple[str, ...] = ()) -> Any:
    """Recursively produce JSON-safe evidence without credentials or secret URLs."""
    if isinstance(value, dict):
        sensitive = {"authorization", "proxy_authorization", "api_key", "apikey", "key",
                     "password", "token", "access_token", "refresh_token", "secret", "credential", "credentials"}
        return {str(k): "[REDACTED]" if str(k).lower().replace("-", "_") in sensitive
                or str(k).lower().replace("-", "_").endswith(("_api_key", "_password", "_secret", "_token"))
                else sanitize_evidence(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_evidence(v, secrets) for v in value]
    if isinstance(value, datetime):
        return aware_utc(value).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        value = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"']+", "Bearer [REDACTED]", value)
        value = re.sub(r"(?i)\b(?:sk-|gsk_|hf_|freellmapi-)[A-Za-z0-9_-]+", "[REDACTED]", value)
        value = re.sub(r"(?i)(api[_-]?key|token|password|secret|authorization)\s*[=:]\s*[^\s,;\"']+", r"\1=[REDACTED]", value)
        def redact_url(match):
            url = match.group(0)
            try:
                parts = urlsplit(url)
            except ValueError:
                return "[REDACTED_URL]"
            return "[REDACTED_URL]" if parts.query or parts.fragment or parts.username else url
        return re.sub(r"https?://[^\s\"'<>]+", redact_url, value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return "[UNSUPPORTED]"


class ProviderStatus(Enum):
    """Current operational status of a provider."""
    
    LIVE = "live"              # Working, free tier available
    DEGRADED = "degraded"      # Working but with issues (rate limits, partial outages)
    PAYWALLED = "paywalled"    # Was free, now requires payment
    EXHAUSTED = "exhausted"    # Free tier quota exhausted, awaiting reset
    BROKEN_KEY = "broken_key"  # Key exists but auth fails (401/403)
    UNKNOWN = "unknown"        # Never probed or no recent data
    DISABLED = "disabled"      # Explicitly disabled in freellmapi


class ProviderTier(Enum):
    """Type of free tier offered."""
    
    DAILY_TOKENS = "daily_tokens"           # e.g., 6M tokens/day (Groq)
    MONTHLY_TOKENS = "monthly_tokens"       # e.g., 1M tokens/month (Google)
    DAILY_REQUESTS = "daily_requests"       # e.g., 50 req/day (OpenRouter free)
    CREDIT_DOLLARS = "credit_dollars"       # e.g., $0.10/month (HuggingFace)
    ALWAYS_FREE_MODELS = "always_free_models"  # Specific models always free
    TRIAL_ONLY = "trial_only"               # One-time trial credits
    NO_FREE_TIER = "no_free_tier"           # Pay only
    UNKNOWN = "unknown"


class FreellmapiIntegrationType(Enum):
    """How this provider integrates with freellmapi."""
    
    IN_CATALOG_KEY_IN_DB = "in_catalog_key_in_db"      # Already in catalog, key stored
    IN_CATALOG_NO_KEY = "in_catalog_no_key"            # In catalog but no key in DB
    IN_REGISTRY_NOT_CATALOG = "in_registry_not_catalog"  # Provider class exists, not in catalog
    NOT_IN_REGISTRY = "not_in_registry"                # Must use custom endpoint
    UNKNOWN = "unknown"


@dataclass
class FreeProvider:
    """A tracked free LLM provider with its current state."""
    
    # Identity
    name: str                          # Display name (e.g., "Groq")
    platform: str                      # freellmapi platform ID (e.g., "groq")
    
    # Free tier info
    tier_type: ProviderTier = ProviderTier.UNKNOWN
    tier_details: str = ""             # Human-readable (e.g., "1000 req/day, ~6M tokens/month")
    reset_schedule: str = ""           # e.g., "daily at 00:00 UTC", "rolling 5min"
    
    # Current status
    status: ProviderStatus = ProviderStatus.UNKNOWN
    status_reason: str = ""            # Why this status
    last_verified: datetime | None = None
    last_verified_source: str = ""     # "health_check" | "probe" | "dashboard" | "manual"
    
    # freellmapi integration
    freellmapi_key_id: int | None = None
    freellmapi_integration: FreellmapiIntegrationType = FreellmapiIntegrationType.UNKNOWN
    freellmapi_action: str = ""        # Exact next step (curl command, DB insert, etc.)
    freellmapi_key_label: str = ""     # Label in DB if exists
    
    # Key format
    key_format: str = ""               # "API key" | "account_id:token" | "AWS IAM" | "OAuth" | etc.
    dashboard_url: str = ""            # Where to get/manage keys
    
    # Discovery metadata
    discovered_from: str = ""          # "catalog" | "dashboard" | "community" | "search"
    discovery_date: datetime = field(default_factory=datetime.utcnow)
    is_new: bool = False               # First time seen
    
    # Probing
    probe_enabled: bool = True
    probe_model: str = ""              # Model to use for probing
    probe_cost_estimate: str = ""      # "~1 request" | "~100 tokens" | etc.
    
    # Metadata
    notes: str = ""
    tags: list[str] = field(default_factory=list)

    checked_at: datetime | None = None
    probe_source: str = ""
    probe_evidence: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.checked_at = aware_utc(self.checked_at)
        self.last_verified = aware_utc(self.last_verified)
        self.discovery_date = aware_utc(self.discovery_date)

    def freshness(self, now: datetime | None = None, max_age_hours: float = 24) -> str:
        """Report observation age, without inventing a verification time."""
        if self.checked_at is None:
            return "unverified"
        checked = self.checked_at.replace(tzinfo=timezone.utc) if self.checked_at.tzinfo is None else self.checked_at
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        age = (current - checked).total_seconds()
        return "fresh" if 0 <= age <= max_age_hours * 3600 else "stale"

    def effective_status(self, now: datetime | None = None, max_age_hours: float = 24) -> ProviderStatus:
        if self.status == ProviderStatus.DISABLED:
            return ProviderStatus.DISABLED
        return self.status if self.freshness(now, max_age_hours) == "fresh" else ProviderStatus.UNKNOWN

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for serialization."""
        return sanitize_evidence({
            "name": self.name,
            "platform": self.platform,
            "tier_type": self.tier_type.value,
            "tier_details": self.tier_details,
            "reset_schedule": self.reset_schedule,
            "status": self.status.value,
            "status_reason": self.status_reason,
            "last_verified": self.last_verified.isoformat() if self.last_verified else None,
            "last_verified_source": self.last_verified_source,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
            "probe_source": self.probe_source,
            "probe_evidence": self.probe_evidence,
            "freellmapi_key_id": self.freellmapi_key_id,
            "freellmapi_integration": self.freellmapi_integration.value,
            "freellmapi_action": self.freellmapi_action,
            "freellmapi_key_label": self.freellmapi_key_label,
            "key_format": self.key_format,
            "dashboard_url": self.dashboard_url,
            "discovered_from": self.discovered_from,
            "discovery_date": self.discovery_date.isoformat(),
            "is_new": self.is_new,
            "probe_enabled": self.probe_enabled,
            "probe_model": self.probe_model,
            "probe_cost_estimate": self.probe_cost_estimate,
            "notes": self.notes,
            "tags": self.tags,
        })
    
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FreeProvider:
        """Create from dict."""
        data = dict(data)
        for name in ("last_verified", "checked_at", "discovery_date"):
            if name in data:
                data[name] = aware_utc(data[name])
        # Old real probes have a usable alias; gateway health pings do not.
        if "checked_at" not in data and data.get("last_verified_source") in ("probe", "local:freellmapi"):
            data["checked_at"] = data.get("last_verified")
            data.setdefault("probe_source", "local:freellmapi")
        # Convert enums
        data["tier_type"] = ProviderTier(data.get("tier_type", "unknown"))
        data["status"] = ProviderStatus(data.get("status", "unknown"))
        data["freellmapi_integration"] = FreellmapiIntegrationType(data.get("freellmapi_integration", "unknown"))
        return cls(**data)


@dataclass
class ProviderProbeResult:
    """Result of probing a provider via freellmapi."""
    
    provider_platform: str
    success: bool
    model_tested: str
    http_status: int | None = None
    error_type: str | None = None       # "auth" | "quota" | "rate_limit" | "server_error" | "network"
    error_message: str = ""
    remaining_quota: int | None = None
    quota_limit: int | None = None
    quota_reset_at: str | None = None
    response_time_ms: int = 0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    checked_at: datetime | None = None
    probe_source: str = "local:freellmapi"
    attempted_models: list[str] = field(default_factory=list)
    response_category: str = ""
    evidence: list[dict] = field(default_factory=list)

    def __post_init__(self):
        self.checked_at = aware_utc(self.checked_at or self.timestamp)
        self.timestamp = self.checked_at

    def to_dict(self) -> dict[str, Any]:
        return sanitize_evidence(asdict(self))
    
    def to_status(self) -> tuple[ProviderStatus, str]:
        """Convert probe result to provider status + reason."""
        message = sanitize_evidence(self.error_message)
        if self.error_type == "disabled":
            return ProviderStatus.DISABLED, "Probing disabled"
        if self.success:
            if self.remaining_quota is not None and self.remaining_quota == 0:
                return ProviderStatus.EXHAUSTED, "Quota exhausted (0 remaining)"
            return ProviderStatus.LIVE, "Probe successful"
        
        if self.error_type in ("router_auth", "config", "network", "timeout", "router_error", "model_not_found"):
            return ProviderStatus.UNKNOWN, message or "Provider unverified"
        if self.http_status in (401, 403) or self.error_type == "auth":
            return ProviderStatus.BROKEN_KEY, message or "Authentication failed (401)"
        if self.http_status == 402 or self.error_type == "quota":
            return ProviderStatus.EXHAUSTED, message or "Quota/credits exhausted"
        if self.http_status == 429 or self.error_type == "rate_limit":
            return ProviderStatus.DEGRADED, message or "Rate limited"
        if self.http_status and 500 <= self.http_status < 600:
            return ProviderStatus.DEGRADED, f"Server error ({self.http_status})"
        
        return ProviderStatus.UNKNOWN, message or "Unknown error"


@dataclass
class ToolDiscovery:
    """A discovered GitHub tool relevant to our stack."""
    
    repo_name: str                  # "owner/repo"
    description: str
    url: str
    stars: int
    language: str
    topics: list[str]
    last_push: datetime | None
    relevance_reason: str           # Relevance to the configured project profile
    category: str                   # "data-engineering" | "local-llm" | "automation" | "freellmapi" | etc.
    actionable: bool = False        # Can we use this directly?
    install_instructions: str = ""  # How to install/use
    
    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_name": self.repo_name,
            "description": self.description,
            "url": self.url,
            "stars": self.stars,
            "language": self.language,
            "topics": self.topics,
            "last_push": self.last_push.isoformat() if self.last_push else None,
            "relevance_reason": self.relevance_reason,
            "category": self.category,
            "actionable": self.actionable,
            "install_instructions": self.install_instructions,
        }