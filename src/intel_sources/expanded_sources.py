"""V3 expanded discovery source adapters (offline-fixture driven, disabled).

These adapters extend discovery across four additional source families:

- ``fetch_official_feed``   — generic RSS/Atom for official vendor/research/
                              editorial feeds (registry family official-news /
                              research / editorial).
- ``fetch_github_releases`` — documented GitHub REST releases for a bounded
                              tracked-repo list (family github-releases).
- ``fetch_replace_so_reference`` — manual/reference connector for Replace.so
                              rows (family open-source-directory): zero
                              network, curated upstream repo references only.

None of these are wired into the daily default scan. They are invoked only
from tests against saved fixtures and, after per-source owner approval, from
explicitly configured runs. All outbound functions reuse the existing guarded
HTTP helpers (SSRF/WAF/byte-cap/deadline) and treat every fetched string as
untrusted data (sanitized before projection; never executed).
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any
from urllib.parse import urlsplit

from .daily_digest import _sanitize_text
from .evidence import canonical_url

logger = logging.getLogger(__name__)

_MAX_FEED_ENTRIES = 20
_MAX_RELEASE_REPOS = 8
_MAX_RELEASES_PER_RUN = 12
_MAX_SUMMARY_CHARS = 300


def _now_iso(now_iso: str | None) -> str:
    return now_iso or "1970-01-01T00:00:00+00:00"


def _summary_text(value: Any) -> str:
    """Sanitize and bound untrusted feed/skill/release text for projection."""
    cleaned = _sanitize_text(value if isinstance(value, str) else "")
    return cleaned[:_MAX_SUMMARY_CHARS]


def _safe_public_link(url: str) -> str:
    """Scheme/host-level link validation for candidate identity.

    Lighter than the public-export URL policy (queries are legitimate parts of
    feed and release links); display still goes through _sanitize_text, the
    daily gates revalidate hosts, and the public export applies its own
    stricter sanitizer downstream.
    """
    if not isinstance(url, str):
        return ""
    candidate = url.strip()
    if not candidate or any(c.isspace() or ord(c) < 32 for c in candidate):
        return ""
    parts = urlsplit(candidate)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    if parts.username or parts.password:
        return ""
    return candidate


def _candidate_base(source_id: str, source_name: str, url: str, now_iso: str,
                    category: str, why: str) -> dict[str, Any] | None:
    """Shared candidate skeleton; returns None when the URL is not publishable."""
    safe_url = _safe_public_link(url)
    if not safe_url:
        return None
    return {
        "id": "",  # set by the caller
        "name": "",
        "what": "",
        "category": category,
        "source_id": source_id,
        "source_url": safe_url,
        "published_at": None,
        "date_kind": "observed",
        "observed_at": now_iso,
        "why": _summary_text(why),
        "free": {"status": "unknown", "detail": "", "evidence_url": ""},
        "api": {"status": "unknown", "detail": "Availability and costs not assessed",
                "evidence_url": ""},
        "topics": [category],
        "evidence_kind": "fixture_or_reference",
    }


def _error_status(source_id: str, source_name: str, now_iso: str,
                  error_code: str, detail: str) -> dict[str, Any]:
    return {"id": source_id, "name": source_name, "status": "error",
            "checked_at": now_iso, "returned": 0, "accepted": 0,
            "error_code": error_code, "detail": _summary_text(detail)}


def _ok_status(source_id: str, source_name: str, now_iso: str,
               returned: int, accepted: int, note: str = "") -> dict[str, Any]:
    status = {"id": source_id, "name": source_name, "status": "ok",
              "checked_at": now_iso, "returned": returned, "accepted": accepted,
              "error_code": None}
    if note:
        status["truncation"] = _summary_text(note)
    return status


# ---------------------------------------------------------------------------
# Generic RSS/Atom feed adapter (official vendor / research / editorial)
# ---------------------------------------------------------------------------
def normalize_feed_entry(entry: dict[str, Any], source_id: str, source_name: str,
                         now_iso: str, feed_url: str) -> dict[str, Any] | None:
    """Normalize one parsed feed entry into a candidate, or None to skip."""
    from .evidence import canonical_url
    link = canonical_url(entry.get("link") or "")
    title = _summary_text(entry.get("title"))
    if not link or not title:
        return None
    cand = _candidate_base(source_id, source_name, link, now_iso, "news",
                           f"Announced via {source_name}: {title}")
    if cand is None:
        return None
    unique = entry.get("id") or link
    digest = hashlib.sha256(unique.encode("utf-8")).hexdigest()[:16]
    cand["id"] = f"feed:{source_id}:{digest}"
    cand["name"] = title
    summary = entry.get("summary") or entry.get("description") or ""
    cand["what"] = _summary_text(summary) or title
    for key in ("published", "updated"):
        parsed = entry.get(f"{key}_parsed")
        if parsed:
            import time as _time
            ts = _time.mktime(parsed)
            from datetime import datetime as _dt, timezone as _tz
            cand["published_at"] = _dt.fromtimestamp(ts, tz=_tz.utc).isoformat()
            cand["date_kind"] = f"{key}_at"
            break
    cand["evidence_kind"] = "official_feed" if source_id.startswith(("official-", "arxiv-")) else "editorial_feed"
    return cand


async def fetch_official_feed(client: Any, cfg: dict[str, Any], now_iso: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch one configured RSS/Atom feed through the guarded GET path."""
    from .daily_sources import _safe_get

    now_iso = _now_iso(now_iso)
    source_id = cfg.get("id", "unknown-feed")
    source_name = cfg.get("name", source_id)
    url = cfg.get("url", "")
    try:
        if not url or urlsplit(url).scheme not in ("http", "https"):
            return [], _error_status(source_id, source_name, now_iso, "bad_config",
                                     "feed url missing or not http(s)")
        response, _ = await _safe_get(client, url)
        response.raise_for_status()
        text = response.text
        import feedparser
        parsed = feedparser.parse(text)
        if parsed.get("bozo") and not parsed.get("entries"):
            return [], _error_status(source_id, source_name, now_iso, "malformed_feed",
                                     "feed did not parse and produced no entries")
        entries = list(parsed.get("entries") or [])[:_MAX_FEED_ENTRIES]
        candidates: list[dict[str, Any]] = []
        for entry in entries:
            cand = normalize_feed_entry(entry, source_id, source_name, now_iso, url)
            if cand is not None:
                candidates.append(cand)
        note = (f"feed returned {len(parsed.get('entries') or [])} entries; "
                f"bounded to first {len(entries)}") if len(entries) == _MAX_FEED_ENTRIES else ""
        return candidates, _ok_status(source_id, source_name, now_iso,
                                      len(entries), len(candidates), note)
    except Exception as exc:
        logger.debug("feed fetch failed for %s", source_id)
        code = "rate_limited" if "429" in str(type(exc)) or getattr(exc, "response", None) is not None and getattr(getattr(exc, "response", None), "status_code", 0) == 429 else "source_failed"
        return [], _error_status(source_id, source_name, now_iso, code,
                                 "feed fetch failed")


# ---------------------------------------------------------------------------
# GitHub tracked-repository releases (documented REST API, keyless)
# ---------------------------------------------------------------------------
async def fetch_github_releases(client: Any, cfg: dict[str, Any], now_iso: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch latest published releases for a bounded tracked-repo list.

    Honors documented rate-limit semantics: a 403/429 ends the run for this
    source with error_code ``rate_limited`` and any Retry-After value is
    recorded on the status for scheduling decisions. Repos without releases
    (404) are skipped as a note, not an error.
    """
    from .daily_sources import _fetch_json, _GITHUB_USER_AGENT

    now_iso = _now_iso(now_iso)
    source_id = cfg.get("id", "github-releases")
    source_name = cfg.get("name", source_id)
    repos = [r for r in (cfg.get("repos") or []) if isinstance(r, str) and r.count("/") == 1][:_MAX_RELEASE_REPOS]
    if not repos:
        return [], _error_status(source_id, source_name, now_iso, "bad_config",
                                 "no valid owner/repo entries configured")
    candidates: list[dict[str, Any]] = []
    skipped: list[str] = []
    for repo in repos:
        if len(candidates) >= _MAX_RELEASES_PER_RUN:
            break
        url = f"https://api.github.com/repos/{repo}/releases/latest"
        try:
            meta = await _fetch_json(client, url,
                                     extra_headers={"Accept": "application/vnd.github+json",
                                                    "User-Agent": _GITHUB_USER_AGENT})
        except Exception as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            if status_code in (403, 429):
                retry_after = getattr(getattr(exc, "response", None), "headers", {}).get("Retry-After", "")
                detail = f"rate limited by GitHub; Retry-After={retry_after or 'unspecified'}"
                return candidates, _error_status(source_id, source_name, now_iso,
                                                 "rate_limited", detail)
            if status_code == 404:
                skipped.append(f"{repo} (no published releases)")
                continue
            skipped.append(f"{repo} (lookup failed)")
            continue
        tag = _summary_text(meta.get("tag_name") or "")
        html_url = meta.get("html_url") or f"https://github.com/{repo}/releases"
        release_name = _summary_text(meta.get("name") or tag)
        body = _summary_text(meta.get("body") or "")
        published = meta.get("published_at") or None
        cand = _candidate_base(source_id, source_name, html_url, now_iso, "devtools",
                               f"Published release {tag} of {repo} on GitHub: {release_name}")
        if cand is None:
            skipped.append(f"{repo} (unpublishable release URL)")
            continue
        cand["id"] = f"github-release:{repo}@{tag or 'untagged'}"
        cand["name"] = f"{repo} {tag}".strip()
        cand["what"] = body or release_name or f"Release {tag} of {repo}."
        cand["published_at"] = published
        cand["date_kind"] = "release"
        cand["version"] = tag or None
        cand["evidence_kind"] = "repo_releases"
        candidates.append(cand)
    note = f"tracked repos: {len(repos)}; skipped: {'; '.join(skipped)}" if skipped else f"tracked repos: {len(repos)}"
    return candidates, _ok_status(source_id, source_name, now_iso,
                                  len(repos), len(candidates), note)


# ---------------------------------------------------------------------------
# Replace.so manual/reference connector (zero network)
# ---------------------------------------------------------------------------
def fetch_replace_so_reference(cfg: dict[str, Any], now_iso: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build candidates from a curated Replace.so reference list.

    Replace.so has no stable public API presumed by this project; unattended
    page extraction is PENDING_PERMISSION. This connector reads only curated
    rows (tool name + upstream repository URL + factual note) supplied by
    config/fixtures. The directory's ranking is context, never our rating;
    license/self-hostability must be verified from the upstream repo.
    """
    now_iso = _now_iso(now_iso)
    source_id = cfg.get("id", "replace-so-reference")
    source_name = cfg.get("name", "Replace.so references")
    rows = cfg.get("references") or []
    candidates: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if not isinstance(row, dict):
            skipped += 1
            continue
        repo_url = row.get("upstream_repo_url") or ""
        note = _summary_text(row.get("note") or "")
        tool = _summary_text(row.get("tool") or "")
        if not tool or len(note) < 20:
            skipped += 1
            continue
        clean_repo_url = canonical_url(repo_url)
        cand = _candidate_base(source_id, source_name, clean_repo_url, now_iso,
                               row.get("category") or "devtools",
                               "Listed on Replace.so as a reviewed open-source alternative; "
                               "upstream repository referenced for verification.")
        if cand is None:
            skipped += 1
            continue
        import re as _re
        owner_repo = _re.sub(r"^https?://github\.com/", "", clean_repo_url, flags=_re.IGNORECASE).strip("/").lower()
        if owner_repo.count("/") != 1:
            skipped += 1
            continue
        cand["id"] = f"replace-so:{owner_repo}"
        cand["name"] = tool
        cand["what"] = note
        cand["evidence_kind"] = "manual_reference"
        candidates.append(cand)
    note = f"curated rows: {len(rows)}; skipped: {skipped}"
    return candidates, _ok_status(source_id, source_name, now_iso, len(rows), len(candidates), note)


# ---------------------------------------------------------------------------
# Hugging Face Hub model metadata (documented public API, keyless)
# ---------------------------------------------------------------------------
async def fetch_hf_models(client: Any, cfg: dict[str, Any], now_iso: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Fetch recently-updated model metadata from the documented Hub API.

    Keyless, bounded (``limit``, default 10, max 30), GET-only. A model card
    is upstream source metadata, not an independent benchmark; license and
    free-tier claims stay UNKNOWN unless separately evidenced. No inference
    is ever triggered — metadata discovery only.
    """
    from .daily_sources import _fetch_json

    now_iso = _now_iso(now_iso)
    source_id = cfg.get("id", "hf-models-hub")
    source_name = cfg.get("name", "Hugging Face models")
    try:
        limit = max(1, min(int(cfg.get("limit", 10)), 30))
    except (TypeError, ValueError):
        limit = 10
    url = (f"https://huggingface.co/api/models?sort=lastModified&direction=-1"
           f"&limit={limit}&full=false")
    try:
        rows = await _fetch_json(client, url)
        if not isinstance(rows, list):
            raise ValueError("unexpected Hub response shape")
    except Exception:
        logger.debug("HF Hub fetch failed")
        return [], _error_status(source_id, source_name, now_iso, "source_failed",
                                 "Hub metadata fetch failed")
    candidates: list[dict[str, Any]] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        model_id = str(row.get("modelId") or row.get("id") or "").strip()
        if not model_id:
            continue
        model_url = f"https://huggingface.co/{model_id}"
        last_modified = row.get("lastModified") or None
        likes = row.get("likes") if isinstance(row.get("likes"), int) else None
        downloads = row.get("downloads") if isinstance(row.get("downloads"), int) else None
        signals = []
        if likes is not None:
            signals.append(f"likes={likes}")
        if downloads is not None:
            signals.append(f"downloads={downloads}")
        signal_text = f" ({', '.join(signals)})" if signals else ""
        cand = _candidate_base(source_id, source_name, model_url, now_iso, "models",
                               f"Recently updated model repository on the Hugging Face Hub"
                               f"{signal_text}; metadata is upstream self-description.")
        if cand is None:
            continue
        cand["id"] = f"hf:{model_id}"
        cand["name"] = model_id
        cand["what"] = (f"Hub repository {model_id} last modified {last_modified or 'unknown'}"
                        f"{signal_text}. Card text is upstream metadata, not a benchmark.")
        cand["published_at"] = last_modified
        cand["date_kind"] = "updated_at"
        cand["evidence_kind"] = "documented_api"
        candidates.append(cand)
    note = f"bounded to latest {limit} by lastModified"
    return candidates, _ok_status(source_id, source_name, now_iso, limit, len(candidates), note)
