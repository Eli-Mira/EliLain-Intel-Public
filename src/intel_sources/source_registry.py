"""INTEL V3 source registry: candidate discovery sources with provenance.

Research documentation target from the V3 directive — this catalog records
what each candidate source IS, how it would be acquired, its legal/access
state and why it is or is not enabled. It is NOT an enablement switch: no
code path reads ``enabled`` from here to run live fetches. All newly
researched sources default to disabled; per-source activation is a separate
owner decision recorded in docs/INTEL_V3_SOURCE_REGISTRY.md.

Schema is strict (unknown keys, unknown enums and inconsistent enablement
raise ValueError) and ``registry_sources()`` returns fresh copies per call,
matching the existing settings immutability contract.
"""
from __future__ import annotations

from typing import Any

FAMILIES = frozenset({
    "provider-catalog", "trending", "skills-directory", "open-source-directory",
    "official-news", "github-releases", "model-catalog", "research",
    "editorial", "community",
})
METHODS = frozenset({
    "documented_api", "official_feed", "repo_releases", "manual_reference",
    "permitted_html_research", "existing_adapter",
})
TIERS = frozenset({"primary", "secondary", "community", "editorial"})
STATUSES = frozenset({
    "ACTIVE_EXISTING", "READY_OFFLINE", "CANDIDATE",
    "PENDING_PERMISSION", "BLOCKED", "DEPRECATED",
})
CONTENT_KINDS = frozenset({
    "news", "release", "changelog", "model", "paper", "repo", "skill",
    "catalog", "editorial", "comparison",
})

_REQUIRED_FIELDS = (
    "id", "name", "family", "domain", "method", "tier", "status",
    "docs_url", "last_checked", "check_provenance", "reason",
    "content_kinds", "auth", "rate_notes", "enabled",
)

# check_provenance values reference where the row's facts came from.
_RESEARCH_GUIDE = "INTEL_V3_SOURCE_RESEARCH.md section A/F (public web inspection 2026-10-26)"
_REPORTED = "reported in INTEL_V3_SOURCE_RESEARCH.md section B/E; NOT revalidated by this agent"

SOURCE_CATALOG: tuple[dict[str, Any], ...] = (
    # --- Existing portable defaults (behavior and semantics unchanged) ---
    {
        "id": "openrouter-models", "name": "OpenRouter Models",
        "family": "provider-catalog", "domain": "openrouter.ai",
        "method": "documented_api", "tier": "primary",
        "status": "ACTIVE_EXISTING", "docs_url": "https://openrouter.ai/api/v1/models",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": "Existing portable default; keyless JSON catalog with created timestamps.",
        "content_kinds": ["model", "catalog"], "auth": "none",
        "rate_notes": "Keyless public endpoint; bounded to latest 10 per run.", "enabled": True,
    },
    {
        "id": "github-trending", "name": "GitHub Trending",
        "family": "trending", "domain": "github.com",
        "method": "permitted_html_research", "tier": "secondary",
        "status": "ACTIVE_EXISTING", "docs_url": "https://github.com/trending?since=daily",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": "Existing portable default; word-boundary AI classification + bounded license metadata.",
        "content_kinds": ["repo"], "auth": "none",
        "rate_notes": "HTML page + max 5 api.github.com metadata probes per run.", "enabled": True,
    },
    {
        "id": "skills-hot", "name": "Skills Hot",
        "family": "skills-directory", "domain": "skills.sh",
        "method": "permitted_html_research", "tier": "secondary",
        "status": "ACTIVE_EXISTING", "docs_url": "https://skills.sh/trending",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": "Existing portable default; strict /owner/repo/skill identity; bounded detail fetches.",
        "content_kinds": ["skill"], "auth": "none",
        "rate_notes": "Max 5 detail pages + 5 license probes per run.", "enabled": True,
    },
    {
        "id": "unorouter-pricing", "name": "UnoRouter Pricing",
        "family": "provider-catalog", "domain": "api.unorouter.com",
        "method": "documented_api", "tier": "primary",
        "status": "ACTIVE_EXISTING", "docs_url": "https://api.unorouter.com/api/pricing/catalog",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": "Existing portable default; GET-only endpoint; same-run GET evidence reused for verification.",
        "content_kinds": ["model", "catalog"], "auth": "none",
        "rate_notes": "Keyless; bounded to latest 5 dated rows per run.", "enabled": True,
    },
    # --- READY_OFFLINE: adapter + offline fixtures built in V3, not enabled live ---
    {
        "id": "official-vendor-feeds", "name": "Official vendor news feeds",
        "family": "official-news", "domain": "openai.com/deepmind.google/huggingface.co/…",
        "method": "official_feed", "tier": "primary",
        "status": "READY_OFFLINE", "docs_url": "INTEL_V3_SOURCE_RESEARCH.md#section-b",
        "last_checked": "not_revalidated", "check_provenance": _REPORTED,
        "reason": ("Generic RSS/Atom adapter implemented against offline fixtures; each concrete "
                   "feed URL must be revalidated (endpoint, format, ToS) before live enablement."),
        "content_kinds": ["news", "release", "changelog"], "auth": "none",
        "rate_notes": "One bounded GET per feed per run when enabled.", "enabled": False,
    },
    {
        "id": "github-releases-tracked", "name": "GitHub tracked repository releases",
        "family": "github-releases", "domain": "api.github.com",
        "method": "repo_releases", "tier": "primary",
        "status": "READY_OFFLINE", "docs_url": "https://docs.github.com/en/rest/releases/releases",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": ("Documented public REST releases API implemented against fixtures for a bounded "
                   "tracked-repo list; live use needs owner approval (rate limits are per-IP keyless)."),
        "content_kinds": ["release", "changelog"], "auth": "none (keyless, rate-limited)",
        "rate_notes": "Anonymous 60 req/h per IP; adapter honors 403/429 and skips the run.", "enabled": False,
    },
    {
        "id": "hf-models-hub", "name": "Hugging Face Hub model metadata",
        "family": "model-catalog", "domain": "huggingface.co",
        "method": "documented_api", "tier": "primary",
        "status": "READY_OFFLINE", "docs_url": "https://huggingface.co/docs/hub/en/api",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": "Documented Hub API; adapter implemented against fixtures; live enablement needs approval.",
        "content_kinds": ["model", "repo"], "auth": "none (keyless, rate-limited)",
        "rate_notes": "All Hub calls rate limited; no free-tier inference assumed.", "enabled": False,
    },
    {
        "id": "arxiv-research-feeds", "name": "arXiv category research feeds",
        "family": "research", "domain": "export.arxiv.org",
        "method": "official_feed", "tier": "primary",
        "status": "READY_OFFLINE", "docs_url": "https://export.arxiv.org/rss/cs.AI",
        "last_checked": "not_revalidated", "check_provenance": _REPORTED,
        "reason": ("cs.AI/cs.CL/cs.LG/stat.ML Atom feeds parsed by the generic feed adapter against "
                   "fixtures; endpoint freshness and rate must be revalidated before live use."),
        "content_kinds": ["paper"], "auth": "none",
        "rate_notes": "Preprint ≠ peer-reviewed; abstract-only evidence is labeled as such.", "enabled": False,
    },
    {
        "id": "replace-so-reference", "name": "Replace.so reviewed alternatives (manual reference)",
        "family": "open-source-directory", "domain": "replace.so",
        "method": "manual_reference", "tier": "secondary",
        "status": "READY_OFFLINE", "docs_url": "https://replace.so/tools",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": ("Site verified; no stable public API presumed. Manual/reference connector reads a "
                   "curated list of upstream repo references — no unattended scraping."),
        "content_kinds": ["comparison", "repo"], "auth": "none",
        "rate_notes": "Zero automated requests; directory ranking is stars-based context, not our rating.",
        "enabled": False,
    },
    # --- PENDING_PERMISSION: real machine access needs a per-source owner decision ---
    {
        "id": "skills-sh-hot-page", "name": "Skills.sh hot page",
        "family": "skills-directory", "domain": "skills.sh",
        "method": "permitted_html_research", "tier": "secondary",
        "status": "PENDING_PERMISSION", "docs_url": "https://skills.sh/hot",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": "Short-window popularity signal; retrieval permission/format not verified; popularity ≠ safe.",
        "content_kinds": ["skill"], "auth": "none",
        "rate_notes": "Would reuse existing skills adapter machinery if approved.", "enabled": False,
    },
    {
        "id": "anthropic-news", "name": "Anthropic news",
        "family": "official-news", "domain": "anthropic.com",
        "method": "manual_reference", "tier": "primary",
        "status": "PENDING_PERMISSION", "docs_url": "https://www.anthropic.com/news",
        "last_checked": "2026-10-26", "check_provenance": _REPORTED,
        "reason": "No official RSS confirmed; do not invent feed endpoints; manual reference only for now.",
        "content_kinds": ["news", "release"], "auth": "none",
        "rate_notes": "n/a until a permitted retrieval path is approved.", "enabled": False,
    },
    {
        "id": "mistral-news", "name": "Mistral news",
        "family": "official-news", "domain": "mistral.ai",
        "method": "manual_reference", "tier": "primary",
        "status": "PENDING_PERMISSION", "docs_url": "https://mistral.ai/news",
        "last_checked": "not_revalidated", "check_provenance": _REPORTED,
        "reason": "Official feed availability unverified.",
        "content_kinds": ["news", "release"], "auth": "none",
        "rate_notes": "n/a until verified.", "enabled": False,
    },
    {
        "id": "github-changelog", "name": "GitHub changelog",
        "family": "official-news", "domain": "github.blog",
        "method": "official_feed", "tier": "primary",
        "status": "PENDING_PERMISSION", "docs_url": "https://github.blog/changelog/",
        "last_checked": "not_revalidated", "check_provenance": _REPORTED,
        "reason": "Official developer-platform updates; permitted feed variant must be confirmed first.",
        "content_kinds": ["changelog"], "auth": "none",
        "rate_notes": "n/a until verified.", "enabled": False,
    },
    {
        "id": "semantic-scholar", "name": "Semantic Scholar Graph API",
        "family": "research", "domain": "api.semanticscholar.org",
        "method": "documented_api", "tier": "secondary",
        "status": "PENDING_PERMISSION", "docs_url": "https://api.semanticscholar.org/api-docs/",
        "last_checked": "not_revalidated", "check_provenance": _REPORTED,
        "reason": "Optional metadata source; current quota/terms must be verified before any automation.",
        "content_kinds": ["paper"], "auth": "none (keyless tier)",
        "rate_notes": "Shared keyless quota historically tight.", "enabled": False,
    },
    # --- CANDIDATE editorial/community rows (existing adapters already cover HN/Reddit/YouTube) ---
    {
        "id": "simonwillison-atom", "name": "Simon Willison Atom feed",
        "family": "editorial", "domain": "simonwillison.net",
        "method": "official_feed", "tier": "editorial",
        "status": "CANDIDATE", "docs_url": "https://simonwillison.net/atom/everything/",
        "last_checked": "not_revalidated", "check_provenance": _REPORTED,
        "reason": "Strong independent technical commentary; generic feed adapter applies if enabled.",
        "content_kinds": ["editorial", "news"], "auth": "none",
        "rate_notes": "One bounded GET per run if enabled.", "enabled": False,
    },
    {
        "id": "ars-technica-ai", "name": "Ars Technica AI feed",
        "family": "editorial", "domain": "arstechnica.com",
        "method": "official_feed", "tier": "editorial",
        "status": "CANDIDATE", "docs_url": "https://arstechnica.com/ai/feed/",
        "last_checked": "not_revalidated", "check_provenance": _REPORTED,
        "reason": "Independent secondary signal; consequential claims need primary corroboration.",
        "content_kinds": ["editorial", "news"], "auth": "none",
        "rate_notes": "One bounded GET per run if enabled.", "enabled": False,
    },
    {
        "id": "hacker-news-existing", "name": "Hacker News (existing adapter)",
        "family": "community", "domain": "news.ycombinator.com",
        "method": "existing_adapter", "tier": "community",
        "status": "ACTIVE_EXISTING", "docs_url": "https://hn.algolia.com/api",
        "last_checked": "2026-10-26", "check_provenance": _RESEARCH_GUIDE,
        "reason": "Existing news_tracker adapter (Algolia API); community signal, not primary evidence.",
        "content_kinds": ["news"], "auth": "none",
        "rate_notes": "Existing adapter timeouts/budgets apply.", "enabled": True,
    },
)


def validate_registry_entry(entry: Any) -> dict[str, Any]:
    """Validate one registry entry with a strict schema (fail closed)."""
    if not isinstance(entry, dict):
        raise ValueError("registry entry must be a dict")
    for field in _REQUIRED_FIELDS:
        if field not in entry:
            raise ValueError(f"registry entry missing required field: {field}")
    for key in entry:
        if key not in _REQUIRED_FIELDS:
            raise ValueError(f"registry entry unknown field: {key}")
    for field in ("id", "name", "domain", "docs_url", "last_checked",
                  "check_provenance", "reason", "auth", "rate_notes"):
        if not isinstance(entry[field], str) or not entry[field]:
            raise ValueError(f"registry entry {field} must be a non-empty string")
    if entry["family"] not in FAMILIES:
        raise ValueError(f"registry entry unknown family: {entry['family']}")
    if entry["method"] not in METHODS:
        raise ValueError(f"registry entry unknown method: {entry['method']}")
    if entry["tier"] not in TIERS:
        raise ValueError(f"registry entry unknown tier: {entry['tier']}")
    if entry["status"] not in STATUSES:
        raise ValueError(f"registry entry unknown status: {entry['status']}")
    if not isinstance(entry["content_kinds"], list) or not entry["content_kinds"]:
        raise ValueError("registry entry content_kinds must be a non-empty list")
    for kind in entry["content_kinds"]:
        if kind not in CONTENT_KINDS:
            raise ValueError(f"registry entry unknown content kind: {kind}")
    if not isinstance(entry["enabled"], bool):
        raise ValueError("registry entry enabled must be bool")
    # Fail closed: only ACTIVE_EXISTING rows may be enabled, and every
    # ACTIVE_EXISTING row must be enabled.
    if entry["enabled"] and entry["status"] != "ACTIVE_EXISTING":
        raise ValueError("only ACTIVE_EXISTING sources may be enabled")
    if entry["status"] == "ACTIVE_EXISTING" and not entry["enabled"]:
        raise ValueError("ACTIVE_EXISTING sources must be enabled")
    if entry["docs_url"].lower().startswith("http"):
        pass  # http(s) documentation link
    elif not entry["docs_url"] or " " in entry["docs_url"]:
        raise ValueError("registry entry docs_url must be a URL or doc reference")
    return entry


def validate_registry(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        raise ValueError("registry must be a list")
    seen: set[str] = set()
    validated = []
    for entry in entries:
        validated.append(validate_registry_entry(entry))
        if entry["id"] in seen:
            raise ValueError(f"duplicate registry id: {entry['id']}")
        seen.add(entry["id"])
    return validated


def registry_sources() -> list[dict[str, Any]]:
    """Fresh deep-enough copies of the catalog (no shared mutable state)."""
    return [dict(entry, content_kinds=list(entry["content_kinds"]))
            for entry in SOURCE_CATALOG]


def registry_entry(source_id: str) -> dict[str, Any] | None:
    """Return a fresh copy of one entry by id, or None."""
    for entry in SOURCE_CATALOG:
        if entry["id"] == source_id:
            return dict(entry, content_kinds=list(entry["content_kinds"]))
    return None


def registry_summary() -> dict[str, Any]:
    """Counts by status and family for docs/diagnostics."""
    by_status: dict[str, int] = {}
    by_family: dict[str, int] = {}
    for entry in SOURCE_CATALOG:
        by_status[entry["status"]] = by_status.get(entry["status"], 0) + 1
        by_family[entry["family"]] = by_family.get(entry["family"], 0) + 1
    return {"total": len(SOURCE_CATALOG), "by_status": by_status, "by_family": by_family}
