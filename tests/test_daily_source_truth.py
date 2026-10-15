"""Source-truth regression tests for daily_sources adapters (RED/GREEN).

Covers three concrete defects:
1. UnoRouter free-status tri-state: literal is_free is True + explicit finite
   nonnegative prices; missing/invalid -> unknown; nonzero -> paid; a single
   invalid row must not zero the whole source; truncation partition is accurate.
2. GitHub/Skills api status uses 'unknown' with explicit not-assessed detail
   (source-code license stays distinct from service costs).
3. GitHub metadata failure stays local unknown; 403 cause is inspected from
   the actual status; plain noncredential User-Agent is sent on ordinary
   public metadata requests. No token/auth support, no transport lifecycle edits.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from intel_sources.daily_sources import (
    _fetch_github_trending,
    _fetch_skills_hot,
    _fetch_unorouter,
    _unorouter_free_status,
)


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def _unorouter_catalog_with_statuses(*statuses) -> dict:
    """Build a small UnoRouter catalog with controlled free-status rows.

    *statuses must be one of 'free-tier', 'paid', 'unknown'.
    Each input maps directly to the expected classification.
    """
    models = []
    for s in statuses:
        if s == "free-tier":
            models.append({
                "model_name": "free-model",
                "vendor": "Vendor",
                "is_free": True,
                "release_ts": 1791462849000,
                "input_price": 0,
                "output_price": 0,
                "fixed_price": 0,
            })
        elif s == "paid":
            models.append({
                "model_name": "paid-model",
                "vendor": "Vendor",
                "is_free": True,
                "release_ts": 1791397883000,
                "input_price": 2,
                "output_price": 5.4,
                "fixed_price": 0,
            })
        else:
            # unknown: could be is_free=False with valid prices, missing price,
            # null price, string is_free, etc. We pick a representative set here.
            models.append({
                "model_name": "unknown-model",
                "vendor": "Vendor",
                "is_free": False,
                "release_ts": 1791244800000,
                "input_price": 0,
                "output_price": 0,
                "fixed_price": 0,
            })
    return {"models": models}


# ---------------------------------------------------------------------------
# Part 1 -- UnoRouter free-status tri-state (unit under the helper)
# ---------------------------------------------------------------------------
class TestUnorouterFreeStatus:
    """Regression tests for the _unorouter_free_status helper."""

    def test_free_tier_when_is_free_true_and_all_prices_zero(self):
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": 0, "output_price": 0, "fixed_price": 0,
        }) == "free-tier"

    def test_paid_when_prices_nonzero_even_if_is_free_true(self):
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": 2, "output_price": 5.4, "fixed_price": 0,
        }) == "paid"

    def test_unknown_when_is_free_false_with_all_zero_prices(self):
        # Contradictory signal: is_free=False but all prices zero.
        # Old code would call bool(False)=False and then prices==0 => NOT free,
        # so classified paid. New: must stay unknown (not inferred).
        assert _unorouter_free_status({
            "is_free": False,
            "input_price": 0, "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_is_free_is_string_false(self):
        # BUG FIX: bool('false') is True in old code. Now unknown.
        assert _unorouter_free_status({
            "is_free": "false",
            "input_price": 0, "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_input_price_missing(self):
        # BUG FIX: missing input_price defaulted to 0, making model appear free.
        assert _unorouter_free_status({
            "is_free": True,
            "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_input_price_is_null(self):
        # BUG FIX: null price caused Decimal("None") → InvalidOperation → whole
        # source exception (row zeroed the source). Now row is unknown.
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": None,
            "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_price_negative(self):
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": -1, "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_price_nan(self):
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": float("nan"), "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_price_inf(self):
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": float("inf"), "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_price_is_boolean(self):
        # bool is subclass of int; reject it explicitly.
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": True, "output_price": 0, "fixed_price": 0,
        }) == "unknown"

    def test_unknown_when_price_is_string_number(self):
        assert _unorouter_free_status({
            "is_free": True,
            "input_price": "0", "output_price": 0, "fixed_price": 0,
        }) == "unknown"


# ---------------------------------------------------------------------------
# Part 2 -- UnoRouter adapter: invalid row must not zero entire source
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_unorouter_single_invalid_row_does_not_zero_source():
    """A single row with a null price must not raise / drop the whole source."""
    catalog = {
        "models": [
            {"model_name": "valid-paid", "vendor": "V", "is_free": True,
             "release_ts": 1791462849000, "input_price": 2, "output_price": 5.4, "fixed_price": 0},
            {"model_name": "null-price", "vendor": "V", "is_free": True,
             "release_ts": 1791397883000, "input_price": None, "output_price": 0, "fixed_price": 0},
        ]
    }
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value=catalog):
        candidates, status = await _fetch_unorouter(None, NOW.isoformat())
    assert status["status"] == "ok", f"source should not fail: {status}"
    assert len(candidates) == 2
    by_name = {c["name"]: c for c in candidates}
    assert by_name["valid-paid"]["free"]["status"] == "paid"
    assert by_name["null-price"]["free"]["status"] == "unknown"


@pytest.mark.asyncio
async def test_unorouter_partition_disclosure_accuracy():
    """Truncation message must reflect the actual selected per-bucket count."""
    catalog = {
        "models": [
            {"model_name": f"m{i}", "vendor": "V", "is_free": i % 3 == 0,
             "release_ts": 1791462849000 - i * 1000,
             "input_price": 0 if i % 3 == 0 else i, "output_price": 0, "fixed_price": 0}
            for i in range(8)  # 3 free-tier (0,3,6), 5 paid (1,2,4,5,7)
        ]
    }
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value=catalog):
        _, status = await _fetch_unorouter(None, NOW.isoformat())
    trunc = status["truncation"]
    # With 3 free-tier and 5 paid, selected=5: all 3 free + 2 paid.
    assert "latest free-tier 3" in trunc
    assert "latest paid 2" in trunc
    assert "latest unknown 0" in trunc


@pytest.mark.asyncio
async def test_unorouter_candidate_free_status_tri_state():
    """Through the adapter: each candidate's free.status matches classification."""
    catalog = _unorouter_catalog_with_statuses("free-tier", "paid", "unknown")
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value=catalog):
        candidates, _ = await _fetch_unorouter(None, NOW.isoformat())
    statuses = {c["name"]: c["free"]["status"] for c in candidates}
    assert statuses.get("free-model") == "free-tier"
    assert statuses.get("paid-model") == "paid"
    assert statuses.get("unknown-model") == "unknown"


@pytest.mark.asyncio
async def test_unorouter_free_tier_candidate_has_free_api_topic():
    catalog = _unorouter_catalog_with_statuses("free-tier")
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value=catalog):
        candidates, _ = await _fetch_unorouter(None, NOW.isoformat())
    assert len(candidates) == 1
    assert "free-api" in candidates[0]["topics"]


@pytest.mark.asyncio
async def test_unorouter_paid_candidate_no_free_api_topic():
    catalog = _unorouter_catalog_with_statuses("paid")
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value=catalog):
        candidates, _ = await _fetch_unorouter(None, NOW.isoformat())
    assert len(candidates) == 1
    assert "free-api" not in candidates[0]["topics"]


# ---------------------------------------------------------------------------
# Part 3 -- GitHub/Skills api status = 'unknown' + not-assessed detail
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_github_candidate_api_status_unknown_not_assessed():
    html = """
    <html><body>
    <article class="Box-row">
      <h2><a href="/morluto/rea">morluto / rea</a></h2>
      <p>Reverse engineer anything with agents, from app behavior down to native binaries.</p>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    </body></html>
    """
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=html), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value={"full_name": "morluto/rea", "license": {"spdx_id": "MIT"}}):
        candidates, _ = await _fetch_github_trending(None, NOW.isoformat())
    assert len(candidates) == 1
    api = candidates[0]["api"]
    assert api["status"] == "unknown"
    assert api["detail"] == "API availability and service costs not assessed"


@pytest.mark.asyncio
async def test_skills_candidate_api_status_unknown_not_assessed():
    html = """
    <html><head><meta name="description" content="Design mobile app screens with Sleek, edit Sleek projects, and implement their designs in React Native or HTML."></meta></head><body>
    <a href="/designed-by-ai/skills/design-mobile-apps"><h3>Design Mobile Apps</h3><p>Build mobile UIs.</p></a>
    </body></html>
    """
    detail_html = html
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               side_effect=[html, detail_html]), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value={}):
        candidates, _ = await _fetch_skills_hot(None, NOW.isoformat())
    assert len(candidates) == 1
    api = candidates[0]["api"]
    assert api["status"] == "unknown"
    assert api["detail"] == "API availability and service costs not assessed"


@pytest.mark.asyncio
async def test_github_license_distinct_from_service_costs():
    """free.detail references the license; api.detail references service costs."""
    html = """
    <html><body>
    <article class="Box-row">
      <h2><a href="/test/repo">test / repo</a></h2>
      <p>A meaningful project description for an autonomous agent toolkit that definitely exceeds the one hundred character text minimum required for the trending adapter to accept the page content without raising a ValueError.</p>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    </body></html>
    """
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=html), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value={"full_name": "test/repo", "license": {"spdx_id": "MIT"}}):
        candidates, _ = await _fetch_github_trending(None, NOW.isoformat())
    cand = candidates[0]
    assert "license" in cand["free"]["detail"].lower() or "mit" in cand["free"]["detail"].lower()
    assert "service costs" in cand["api"]["detail"].lower() or "not assessed" in cand["api"]["detail"].lower()


# ---------------------------------------------------------------------------
# Part 4 -- GitHub metadata: User-Agent + 403 stays local unknown
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_github_metadata_uses_plain_user_agent():
    """Metadata probe sends a plain noncredential User-Agent."""
    captured_headers: list[dict] = []

    async def capture(client, url, *, extra_headers=None):
        captured_headers.append(extra_headers or {})
        return {"full_name": "morluto/rea", "license": {"spdx_id": "MIT"}}

    html = """
    <html><body>
    <article class="Box-row">
      <h2><a href="/morluto/rea">morluto / rea</a></h2>
      <p>Reverse engineer anything with agents, from app behavior down to native binaries.</p>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    </body></html>
    """
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=html), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               side_effect=capture):
        await _fetch_github_trending(None, NOW.isoformat())
    assert captured_headers, "metadata lookup was not called"
    headers = captured_headers[0]
    assert "User-Agent" in headers
    ua = headers["User-Agent"]
    # Noncredential: no token, bearer, or API key in the identity.
    for forbidden in ("token", "bearer", "api-key", "ghp_", "gho_"):
        assert forbidden not in ua.lower(), f"credential leaked in User-Agent: {ua}"
    # Accept header still present.
    assert headers.get("Accept") == "application/vnd.github+json"


@pytest.mark.asyncio
async def test_github_metadata_403_stays_local_unknown():
    """A 403 from the GitHub metadata probe must not propagate as rate-limit.

    The candidate's free status stays 'unknown'; the source status stays 'ok';
    and the status_info error_code remains None (no false assumption).
    """
    html = """
    <html><body>
    <article class="Box-row">
      <h2><a href="/morluto/rea">morluto / rea</a></h2>
      <p>Reverse engineer anything with agents, from app behavior down to native binaries.</p>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    <article class="Box-row">
      <h2><a href="/other/project">other / project</a></h2>
      <p>A plain unrelated cooking recipe book with no relevant keywords anywhere in this description text to keep it from matching.</p>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    </body></html>
    """
    # Build a real HTTPStatusError with 403
    req = httpx.Request("GET", "https://api.github.com/repos/morluto/rea")
    resp = httpx.Response(403, request=req, content=b"Forbidden")
    err = httpx.HTTPStatusError("403", request=req, response=resp)

    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=html), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               side_effect=err):
        candidates, status = await _fetch_github_trending(None, NOW.isoformat())
    # morluto/rea still surfaces with unknown free status (license unknown).
    assert len(candidates) == 1
    assert candidates[0]["free"]["status"] == "unknown"
    # Source itself did not fail (other rows still processed).
    assert status["status"] == "ok"
    assert status["error_code"] is None


@pytest.mark.asyncio
async def test_github_metadata_403_not_assumed_rate_limit():
    """Failure path must never set an error_code implying rate-limit."""
    req = httpx.Request("GET", "https://api.github.com/repos/a/b")
    resp = httpx.Response(403, request=req, content=b"rate limit hit")
    err = httpx.HTTPStatusError("403", request=req, response=resp)

    html = """
    <html><body>
    <article class="Box-row">
      <h2><a href="/a/b">a / b</a></h2>
      <p>An AI agent tool for testing.</p>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    </body></html>
    """
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock,
               return_value=html), \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               side_effect=err):
        candidates, status = await _fetch_github_trending(None, NOW.isoformat())
    assert len(candidates) == 1
    assert status["error_code"] is None
    assert "rate_limit" not in (status.get("error_code") or "").lower()
