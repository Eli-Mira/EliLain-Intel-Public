"""M3 regression tests: license evidence states, GET-evidence reuse, exclusion codes.

V3 source-expansion batch 1. Three repairs under unchanged score policy:
1. GitHub/Skills license classification distinguishes no-declared-license from
   lookup failure from verified OSI — all remain free_status 'unknown' (no +15
   bonus), but the evidence text is now truthful about WHY.
2. UnoRouter candidates carry same-run guarded-GET evidence; verification does
   not re-request a URL the adapter just fetched (GET-only endpoints answer
   404 to HEAD).
3. build_digest(include_rejections=True) exposes {id, reason} exclusion codes
   for private raw evidence; default public schema unchanged.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from intel_sources.daily_digest import build_digest
from intel_sources.daily_sources import (
    _fetch_github_trending,
    _fetch_skills_hot,
    verify_candidates,
)
from intel_sources.settings import default_settings

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)

TRENDING_HTML = """
<html><body>
<article class="Box-row">
  <h2><a href="/example/tool">example / tool</a></h2>
  <p>An agent framework for local inference with documented behavior.</p>
  <span itemprop="programmingLanguage">Python</span>
</article>
</body></html>
"""

SKILLS_LIST_HTML = """
<html><body>
<a href="/owner/repo/skill-slug"><h3>Skill Slug</h3></a>
</body></html>
"""

SKILLS_DETAIL_HTML = """
<html><head><meta name="description" content="A real documented skill description long enough to pass."></head><body></body></html>
"""


def _verify_stub(status=200):
    async def fake_head(client, url, timeout=None):
        return httpx.Response(status, headers={}, request=httpx.Request("HEAD", url))
    return fake_head


# ---------------------------------------------------------------------------
# 1. License evidence states
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_github_license_no_file_reported_distinctly():
    """API reachable, license=None -> 'no license file', not a lookup failure."""
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=TRENDING_HTML), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value={"full_name": "example/tool", "license": None}):
        candidates, _ = await _fetch_github_trending(None, NOW.isoformat())
    assert len(candidates) == 1
    free = candidates[0]["free"]
    assert free["status"] == "unknown"  # policy unchanged: no bonus
    assert "no license file" in free["detail"]
    assert "unverified" in free["detail"]


@pytest.mark.asyncio
async def test_github_license_lookup_failure_reported_distinctly():
    """API error/rate limit -> 'lookup unavailable', not 'no license'."""
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=TRENDING_HTML), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               side_effect=httpx.HTTPStatusError("rate limited",
                   request=httpx.Request("GET", "https://api.github.com/repos/example/tool"),
                   response=httpx.Response(403))):
        candidates, _ = await _fetch_github_trending(None, NOW.isoformat())
    assert len(candidates) == 1
    free = candidates[0]["free"]
    assert free["status"] == "unknown"
    assert "lookup unavailable" in free["detail"]


@pytest.mark.asyncio
async def test_github_license_verified_osi_still_open_source():
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=TRENDING_HTML), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value={"full_name": "example/tool", "license": {"spdx_id": "MIT"}}):
        candidates, _ = await _fetch_github_trending(None, NOW.isoformat())
    assert candidates[0]["free"]["status"] == "open-source"


@pytest.mark.asyncio
async def test_skills_license_no_file_reported_distinctly():
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               side_effect=[SKILLS_LIST_HTML, SKILLS_DETAIL_HTML]), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value={"license": None}):
        candidates, _ = await _fetch_skills_hot(None, NOW.isoformat())
    assert len(candidates) == 1
    free = candidates[0]["free"]
    assert free["status"] == "unknown"
    assert "no license file" in free["detail"]


@pytest.mark.asyncio
async def test_skills_license_lookup_failure_reported_distinctly():
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               side_effect=[SKILLS_LIST_HTML, SKILLS_DETAIL_HTML]), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               side_effect=TimeoutError("Request timed out")):
        candidates, _ = await _fetch_skills_hot(None, NOW.isoformat())
    free = candidates[0]["free"]
    assert free["status"] == "unknown"
    assert "lookup unavailable" in free["detail"]


# ---------------------------------------------------------------------------
# 2. Same-run GET evidence reuse
# ---------------------------------------------------------------------------
def _unorouter_style_candidate(marker=True):
    return {
        "id": "unorouter:free-model:free", "name": "free-model",
        "what": "UnoRouter pricing catalog entry for a free-tier model, all zero pricing verified.",
        "category": "models", "source_id": "unorouter-pricing",
        "source_url": "https://api.unorouter.com/api/pricing/catalog",
        "published_at": (NOW - timedelta(days=1)).isoformat(),
        "date_kind": "release", "observed_at": NOW.isoformat(),
        "free": {"status": "free-tier", "detail": "verified zero pricing",
                 "evidence_url": "https://api.unorouter.com/api/pricing/catalog"},
        "api": {"status": "available", "detail": "REST endpoint",
                "evidence_url": "https://api.unorouter.com/api/pricing/catalog"},
        "topics": ["models", "free-api"],
        "why": "UnoRouter catalog free-tier entry with verified zero pricing.",
        **({"_source_get_verified": NOW.isoformat()} if marker else {}),
    }


def test_source_get_verified_marker_skips_http_recheck():
    cand = _unorouter_style_candidate()
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "verified"
    assert result[0]["link_verification"]["http_status"] == 200
    assert mock_head.await_count == 0
    assert mock_get.await_count == 0


def test_source_get_verified_marker_requires_https():
    """The marker never bypasses scheme/host safety revalidation."""
    cand = _unorouter_style_candidate()
    cand["source_url"] = "http://api.unorouter.com/api/pricing/catalog"
    result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "unverified"


def test_source_get_verified_marker_requires_safe_host():
    cand = _unorouter_style_candidate()
    cand["source_url"] = "https://127.0.0.1/api/pricing/catalog"
    result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "unverified"


def test_unmarked_candidate_still_uses_head_with_get_fallback():
    """Pre-existing a41e3fb behavior unchanged for candidates without the marker."""
    cand = _unorouter_style_candidate(marker=False)
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.return_value = httpx.Response(404, headers={},
            request=httpx.Request("HEAD", "https://api.unorouter.com/api/pricing/catalog"))
        mock_get.return_value = (httpx.Response(200, headers={},
            request=httpx.Request("GET", "https://api.unorouter.com/api/pricing/catalog")), None)
        result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "verified"
    mock_head.assert_awaited_once()
    mock_get.assert_awaited_once()


# ---------------------------------------------------------------------------
# 3. Exclusion codes via include_rejections
# ---------------------------------------------------------------------------
def _qualifying(cid, url):
    return {
        "id": cid, "name": cid, "what": "A documented model release with real capabilities.",
        "category": "models", "source_id": "openrouter-models", "source_url": url,
        "published_at": (NOW - timedelta(days=1)).isoformat(), "date_kind": "catalog-created",
        "observed_at": NOW.isoformat(),
        "free": {"status": "free-tier", "detail": "zero pricing",
                 "evidence_url": "https://openrouter.ai/api/v1/models"},
        "api": {"status": "available", "detail": "REST",
                "evidence_url": "https://openrouter.ai/api/v1/models"},
        "topics": ["models", "free-api"],
        "why": "OpenRouter lists this model with verified zero pricing; free API access.",
        "link_verification": {"status": "verified", "checked_at": NOW.isoformat(),
                              "url": url, "http_status": 200},
    }


def test_include_rejections_off_by_default():
    stale = _qualifying("openrouter:stale", "https://openrouter.ai/stale")
    stale["published_at"] = "2026-08-01T00:00:00+00:00"
    digest = build_digest({"candidates": [stale], "sources": []}, default_settings(), now=NOW)
    assert "rejections" not in digest


def test_include_rejections_exposes_traceable_codes():
    fresh = _qualifying("openrouter:fresh", "https://openrouter.ai/fresh")
    stale = _qualifying("openrouter:stale", "https://openrouter.ai/stale")
    stale["published_at"] = "2026-08-01T00:00:00+00:00"  # real recorded stale row shape
    dead = _qualifying("openrouter:dead", "https://openrouter.ai/dead")
    dead["link_verification"] = {"status": "unverified", "checked_at": NOW.isoformat(),
                                 "url": dead["source_url"], "http_status": 404}
    digest = build_digest({"candidates": [fresh, stale, dead], "sources": []},
                          default_settings(), now=NOW, include_rejections=True)
    reasons = {r["id"]: r["reason"] for r in digest["rejections"]}
    assert reasons["openrouter:stale"] == "too old"
    assert reasons["openrouter:dead"] == "link not verified"
    assert digest["counts"] == {"fetched": 3, "eligible": 1, "rejected": 2, "selected": 1}
    # Frozen clock: identical input + now -> identical output (no wall clock).
    digest2 = build_digest({"candidates": [fresh, stale, dead], "sources": []},
                           default_settings(), now=NOW, include_rejections=True)
    assert digest2["rejections"] == digest["rejections"]
    assert digest2["generated_at"] == digest["generated_at"]
