"""Regression tests: HEAD 404/405 must fall back to one bounded GET.

Recorded defect (2026-10-15 live release-evidence replay):
every fresh UnoRouter free-tier candidate was rejected "link not verified"
because https://api.unorouter.com/api/pricing/catalog answers 404 to HEAD
while the same URL served the adapter's successful GET moments earlier.
Five genuinely fresh, genuinely free-tier records were silently excluded
from the daily report at the approved min_score=60.
"""
import asyncio
from datetime import datetime, timezone, timedelta

import httpx
from unittest.mock import AsyncMock, patch

from intel_sources.daily_digest import build_digest, _check_gates, _score_item
from intel_sources.daily_sources import verify_candidates
from intel_sources.settings import default_settings

NOW = datetime(2026, 10, 15, 14, 38, tzinfo=timezone.utc)
UNOROUTER_URL = "https://api.unorouter.com/api/pricing/catalog"


def _response(status: int, method: str = "GET") -> httpx.Response:
    return httpx.Response(status, headers={}, request=httpx.Request(method, UNOROUTER_URL))


def unorouter_candidate(item_id: str = "unorouter:step-5-preview:free") -> dict:
    """Shape mirrors a real recorded UnoRouter free-tier row."""
    return {
        "id": item_id,
        "name": "step-5-preview:free",
        "what": "UnoRouter pricing catalog entry for a free-tier model with verified zero pricing.",
        "category": "models",
        "source_id": "unorouter-pricing",
        "source_url": UNOROUTER_URL,
        "published_at": (NOW - timedelta(days=1)).isoformat(),
        "date_kind": "release",
        "observed_at": NOW.isoformat(),
        "free": {"status": "free-tier",
                 "detail": "UnoRouter catalog is_free=True; pricing input=0 output=0 fixed=0 (all verified zero)",
                 "evidence_url": UNOROUTER_URL},
        "api": {"status": "available", "detail": "REST endpoint", "evidence_url": UNOROUTER_URL},
        "topics": ["models", "free-api"],
        "why": ("UnoRouter catalog entry: vendor step-5-preview (input=0, output=0, "
                "fixed=0). Free-tier. Entitlement unverified beyond catalog listing."),
    }


def openrouter_candidate(item_id: str, slug: str) -> dict:
    """Shape mirrors a real recorded fresh OpenRouter model row."""
    return {
        "id": f"openrouter:{item_id}",
        "name": item_id,
        "what": "OpenRouter lists a newly created model with documented capabilities and pricing.",
        "category": "models",
        "source_id": "openrouter-models",
        "source_url": f"https://openrouter.ai/{item_id}",
        "published_at": (NOW - timedelta(days=1)).isoformat(),
        "date_kind": "catalog-created",
        "observed_at": NOW.isoformat(),
        "free": {"status": "free-tier",
                 "detail": "OpenRouter pricing prompt=0 completion=0 (both verified zero)",
                 "evidence_url": "https://openrouter.ai/api/v1/models"},
        "api": {"status": "available", "detail": "REST API",
                "evidence_url": "https://openrouter.ai/api/v1/models"},
        "topics": ["models", "free-api"],
        "why": (f"OpenRouter lists {item_id} with prompt=0 and completion=0 "
                "(both zero); free API access."),
    }


# ---------------------------------------------------------------------------
# Verification-level regression: the GET fallback itself
# ---------------------------------------------------------------------------
def test_head_404_falls_back_to_get_and_verifies():
    """HEAD 404 on a GET-only endpoint must not exclude a reachable link."""
    cand = unorouter_candidate()
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.return_value = _response(404, method="HEAD")
        mock_get.return_value = (_response(200), None)
        result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "verified"
    assert result[0]["link_verification"]["http_status"] == 200
    mock_get.assert_awaited_once()


def test_head_405_also_falls_back_to_get():
    """405 Method Not Allowed is the standard HEAD-unsupported signal."""
    cand = unorouter_candidate("unorouter:mimo-v2.6-flash:free")
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.return_value = _response(405, method="HEAD")
        mock_get.return_value = (_response(200), None)
        result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "verified"


def test_head_200_does_not_call_get():
    cand = unorouter_candidate()
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.return_value = _response(200, method="HEAD")
        asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert mock_get.await_count == 0


def test_waf_head_response_is_never_reprobed():
    """A WAF-flagged HEAD must stay unverified without a follow-up GET."""
    cand = unorouter_candidate()
    waf = httpx.Response(403, headers={"server": "cloudflare"},
                         request=httpx.Request("HEAD", UNOROUTER_URL))
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.return_value = waf
        result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "unverified"
    assert result[0]["link_verification"]["http_status"] == 403
    assert mock_get.await_count == 0


def test_unverified_when_fallback_get_also_404():
    """A dead link stays unverified even when HEAD and GET both 404."""
    cand = unorouter_candidate("unorouter:dead-model:free")
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.return_value = _response(404, method="HEAD")
        mock_get.return_value = (_response(404), None)
        result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "unverified"


def test_unverified_when_fallback_get_raises():
    cand = unorouter_candidate("unorouter:error-model:free")
    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.return_value = _response(404, method="HEAD")
        mock_get.side_effect = TimeoutError("Request timed out")
        result = asyncio.run(verify_candidates([cand], default_settings(), now=NOW))
    assert result[0]["link_verification"]["status"] == "unverified"
    assert result[0]["link_verification"]["http_status"] == 404  # truthful HEAD outcome


# ---------------------------------------------------------------------------
# Acceptance fixture: five qualified records selected, negatives excluded.
# Mirrors the real recorded Oct 15 candidate mix (fresh free-tier models,
# stale catalog rows, dead links, unknown-license repos) offline.
# ---------------------------------------------------------------------------
def _acceptance_candidates():
    fresh = [
        openrouter_candidate("unbiased/pareto-latest", "pareto-latest"),
        openrouter_candidate("unbiased/pareto-26.9", "pareto-26.9"),
        openrouter_candidate("liquid/lfm-2.5-24b-a2b", "lfm-2.5"),
        openrouter_candidate("google/gemini-4-argon", "gemini-4-argon"),
        unorouter_candidate("unorouter:step-5-preview:free"),
    ]
    too_old = openrouter_candidate("dots-studio/dots-3-note-preview:free", "dots-3")
    too_old["published_at"] = "2026-08-14T04:06:01+00:00"  # real recorded stale row
    dead_link = unorouter_candidate("unorouter:dead-model:free")
    dead_link["source_url"] = "https://unorouter.com/removed/catalog-entry"
    dead_link["free"]["evidence_url"] = "https://unorouter.com/removed/catalog-entry"
    duplicate = openrouter_candidate("unbiased/pareto-latest-dup", "pareto-latest")
    duplicate["id"] = "openrouter:unbiased/pareto-latest-dup"
    # duplicate shares the first fresh item's canonical URL
    duplicate["source_url"] = fresh[0]["source_url"]
    unlicensed = {
        "id": "github:obra/superpowers",
        "name": "obra/superpowers",
        "what": "A fresh trending agent repository with no verified license metadata yet.",
        "category": "agents",
        "source_id": "github-trending",
        "source_url": "https://github.com/obra/superpowers",
        "published_at": NOW.isoformat(),
        "date_kind": "trending",
        "observed_at": NOW.isoformat(),
        "free": {"status": "unknown", "detail": "GitHub public repository; license not verified",
                 "evidence_url": "https://github.com/obra/superpowers"},
        "api": {"status": "unknown", "detail": "API availability and service costs not assessed",
                "evidence_url": ""},
        "topics": ["agents"],
        "why": "Listed on GitHub's daily trending page: a fresh trending agent repository (Python).",
    }
    return fresh + [too_old, dead_link, duplicate, unlicensed]


def test_daily_selection_selects_five_qualified_records_with_negative_exclusions():
    cands = _acceptance_candidates()
    cfg = default_settings()

    async def _fake_head(client, url, timeout=None):
        if url == UNOROUTER_URL:
            return _response(404, method="HEAD")  # live GET-only endpoint
        if url == "https://unorouter.com/removed/catalog-entry":
            return _response(404, method="HEAD")  # genuinely removed entry
        return httpx.Response(200, headers={}, request=httpx.Request("HEAD", url))

    async def _fake_get(client, url, timeout=None, max_bytes=None, extra_headers=None):
        if url == UNOROUTER_URL:
            return _response(200), None
        if url == "https://unorouter.com/removed/catalog-entry":
            return _response(404), None
        raise ValueError("unexpected GET probe")

    with patch("intel_sources.daily_sources._safe_head", new_callable=AsyncMock) as mock_head, \
         patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_head.side_effect = _fake_head
        mock_get.side_effect = _fake_get
        verified = asyncio.run(verify_candidates(cands, cfg, now=NOW))

    statuses = {c["id"]: c["link_verification"]["status"] for c in verified}
    # The defect case: fresh free-tier UnoRouter row verified via GET fallback.
    assert statuses["unorouter:step-5-preview:free"] == "verified"
    assert statuses["unorouter:dead-model:free"] == "unverified"

    digest = build_digest({"candidates": verified, "sources": []}, cfg, now=NOW)
    counts = digest["counts"]
    # 9 fetched: 5 qualified + too-old + dead-link + duplicate + below-threshold
    assert counts["fetched"] == 9
    assert counts["selected"] == 5
    assert counts["eligible"] == 6  # 5 selected + 1 duplicate rejected post-scoring
    assert counts["rejected"] == 4  # too old, dead link, below threshold, duplicate

    selected_urls = {item["source_url"] for item in digest["items"]}
    assert len(selected_urls) == 5  # no repeated unchanged items in one report
    assert UNOROUTER_URL in selected_urls
    for item in digest["items"]:
        assert item["score"] >= cfg["min_score"]
        breakdown = item["scoring_breakdown"]
        assert breakdown["total"] == item["score"]
        assert breakdown["recency"] > 0

    # Traceable exclusion reasons, recomputed with the same gate functions.
    by_id = {c["id"]: c for c in verified}
    assert _check_gates(by_id["openrouter:dots-studio/dots-3-note-preview:free"], cfg, NOW) == "too old"
    assert _check_gates(by_id["unorouter:dead-model:free"], cfg, NOW) == "link not verified"
    score, _ = _score_item(by_id["github:obra/superpowers"], cfg, NOW,
                           {"github-trending": 1}, {"github-trending": 1})
    assert score == 51  # honest calibration gap: owner policy decision, not hidden
    assert score < cfg["min_score"]


def test_strict_settings_unchanged_by_pipeline():
    """default_settings() keeps min_score=60 and fresh per-call copies."""
    first = default_settings()
    second = default_settings()
    assert first["min_score"] == 60
    assert first["max_age_days"] == 7
    assert first is not second
    first["min_score"] = 1
    assert default_settings()["min_score"] == 60
