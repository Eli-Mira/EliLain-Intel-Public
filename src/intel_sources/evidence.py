"""V3 evidence layer: canonical identity, corroboration grouping, novelty.

Minimal additive contract over the existing pipeline. Candidates flow in,
groups flow out; nothing here changes scoring policy or the daily gates.

- ``canonical_url``   — tracking-parameter/fragment-insensitive URL identity
                        (proper URL parsing, not string replaces).
- ``entity_key``      — prefers real-world identifiers (repo owner/name,
                        arXiv id, version-qualified release) over headline
                        hashes so one launch is one entity.
- ``group_findings``  — one canonical finding per entity with ALL provenance
                        preserved as corroborating links; different versions
                        or genuinely different tools are never merged.
- ``novelty``         — first-seen vs repeat against a caller-supplied
                        baseline of entity keys; missing dates yield
                        UNKNOWN_FRESHNESS, never "just launched".
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "ref", "source", "medium", "fbclid", "gclid", "mc_cid", "mc_eid",
}
_REPO_PATH_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:/.*)?$")
_ARXIV_RE = re.compile(r"^/abs/([a-z0-9./-]+?)(?:v\d+)?/?$", re.IGNORECASE)

# Higher wins when choosing the canonical finding of a corroborated group.
_EVIDENCE_TIER_RANK = {
    "official_feed": 4,
    "repo_releases": 4,
    "editorial_feed": 2,
    "manual_reference": 1,
    "fixture_or_reference": 0,
}


def canonical_url(url: str) -> str:
    """Return a normalized URL identity: lowercase host, no fragment, no
    tracking params, no default ports, no trailing slash. Non-URLs have no
    identity and normalize to the empty string."""
    if not url or "://" not in url:
        return ""
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return ""
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if not host:
        return ""
    port = parts.port
    default = {"http": 80, "https": 443}.get(scheme)
    netloc = host if port is None or port == default else f"{host}:{port}"
    query = urlencode([(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
                       if k.lower() not in _TRACKING_PARAMS])
    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, query, ""))


def entity_key(cand: dict[str, Any]) -> str:
    """Prefer real-world identifiers over URL text when available."""
    url = cand.get("source_url") or ""
    canonical = canonical_url(url)
    parts = urlsplit(canonical)
    host = parts.hostname or ""
    if host in ("github.com", "www.github.com"):
        match = _REPO_PATH_RE.match(parts.path or "")
        if match:
            base = f"repo:{match.group(1).lower()}/{match.group(2).lower()}"
            version = cand.get("version")
            return f"{base}@{version}" if version else base
    if host in ("arxiv.org", "www.arxiv.org", "export.arxiv.org"):
        match = _ARXIV_RE.match(parts.path or "")
        if match:
            return f"arxiv:{match.group(1).lower()}"
    return canonical


def _group_sort_key(cand: dict[str, Any]) -> tuple[int, str, str]:
    rank = _EVIDENCE_TIER_RANK.get(cand.get("evidence_kind", ""), 0)
    published = cand.get("published_at") or ""
    return (-rank, published, str(cand.get("id", "")))


def group_findings(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group corroborating candidates into canonical findings.

    All provenance is preserved: every group carries ``corroborations`` with
    each contributing source. Same entity + different version stays separate
    (a v1 release and a v2 release are different events); similarly named but
    distinct repos never merge.
    """
    groups: dict[str, list[dict[str, Any]]] = {}
    for cand in candidates:
        key = entity_key(cand)
        if not key:
            continue
        groups.setdefault(key, []).append(cand)

    findings = []
    for key in sorted(groups):
        members = sorted(groups[key], key=_group_sort_key)
        canonical = dict(members[0])
        canonical["entity_key"] = key
        canonical["corroborations"] = [
            {"source_id": m.get("source_id", ""), "url": m.get("source_url", ""),
             "published_at": m.get("published_at"),
             "evidence_kind": m.get("evidence_kind", ""),
             "source_url_of_record": m.get("source_url", "")}
            for m in members[1:]
        ]
        published = [m.get("published_at") for m in members if m.get("published_at")]
        canonical["freshness"] = "UNKNOWN_FRESHNESS" if not published else "dated"
        canonical["first_published_at"] = min(published) if published else None
        findings.append(canonical)
    return findings


def novelty(findings: list[dict[str, Any]], baseline: set[str]) -> list[dict[str, Any]]:
    """Annotate each finding with first_seen/repeat against a baseline of
    previously observed entity keys (caller-owned persistence)."""
    annotated = []
    for finding in findings:
        marked = dict(finding)
        marked["novelty"] = "first_seen" if finding["entity_key"] not in baseline else "repeat"
        annotated.append(marked)
    return annotated
