"""M5 tests: expanded source adapters against offline fixtures (no network).

Covers four new source families end-to-end at the normalization layer:
official vendor RSS, GitHub tracked releases, Replace.so manual references,
and arXiv-style research feeds — all disabled by default and never wired
into the daily scan's approved source gate.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from intel_sources.expanded_sources import (
    fetch_github_releases,
    fetch_official_feed,
    fetch_replace_so_reference,
    normalize_feed_entry,
)
from intel_sources.daily_digest import _check_gates
from intel_sources.settings import default_settings

NOW = "2026-10-26T10:00:00+00:00"

VENDOR_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel>
<title>Vendor News</title>
<item>
  <title>Model X launch with documented capabilities</title>
  <link>https://openai.example.com/news/model-x?utm_source=newsletter&amp;fbclid=abc</link>
  <guid>tag:openai.example.com,2026:model-x</guid>
  <pubDate>Mon, 26 Oct 2026 08:00:00 GMT</pubDate>
  <description><![CDATA[Documented launch <script>alert(1)</script>with evidence links.]]></description>
</item>
<item>
  <title>Undated but real announcement entry</title>
  <link>https://openai.example.com/news/undated</link>
  <description>No publication timestamp supplied by the feed.</description>
</item>
</channel></rss>"""

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
<title>cs.AI</title>
<entry>
  <id>http://arxiv.org/abs/2610.12345v1</id>
  <title>Reproducible evaluation of local inference tooling</title>
  <link href="http://arxiv.org/abs/2610.12345v1"/>
  <published>2026-10-25T12:00:00Z</published>
  <summary>Abstract-only evidence: an evaluation of tooling for local inference.</summary>
</entry>
</feed>"""


def _feed_response(text):
    response = httpx.Response(200, headers={}, text=text,
                              request=httpx.Request("GET", "https://feeds.example.com/feed"))
    return response


@pytest.mark.asyncio
async def test_official_feed_normalizes_entries_with_sanitized_text():
    cfg = {"id": "official-openai-news", "name": "OpenAI News", "url": "https://openai.com/news/rss.xml"}
    with patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = (_feed_response(VENDOR_RSS), None)
        candidates, status = await fetch_official_feed(None, cfg, NOW)
    assert status["status"] == "ok"
    assert len(candidates) == 2
    first = candidates[0]
    assert first["source_id"] == "official-openai-news"
    assert first["evidence_kind"] == "official_feed"
    assert first["published_at"] is not None and first["date_kind"] == "published_at"
    # untrusted HTML in the summary is sanitized, never executed
    assert "<script>" not in first["what"] and "alert(1)" not in first["what"]
    # tracking parameters stripped from the canonical link
    assert "utm_source" not in first["source_url"] and "fbclid" not in first["source_url"]


@pytest.mark.asyncio
async def test_undated_feed_entry_keeps_unknown_freshness():
    cfg = {"id": "official-openai-news", "name": "OpenAI News", "url": "https://openai.com/news/rss.xml"}
    with patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = (_feed_response(VENDOR_RSS), None)
        candidates, _ = await fetch_official_feed(None, cfg, NOW)
    undated = [c for c in candidates if c["published_at"] is None]
    assert len(undated) == 1
    assert undated[0]["date_kind"] == "observed"
    from datetime import datetime as _dt, timezone as _tz
    gate_now = _dt(2026, 10, 26, 10, tzinfo=_tz.utc)
    # Satisfy earlier gates so the approval boundary itself is what's tested:
    # a not-yet-approved family can never enter the daily report regardless of
    # other fields.
    undated[0]["link_verification"] = {"status": "verified", "checked_at": NOW,
                                       "url": undated[0]["source_url"], "http_status": 200}
    assert _check_gates(undated[0], default_settings(), gate_now) == "unapproved source id"
    # With an approved source id AND an approved host, the missing date is
    # treated honestly: 'missing published_at', never "just launched".
    approved_variant = dict(undated[0], source_id="openrouter-models",
                            source_url="https://openrouter.ai/news/undated")
    approved_variant["link_verification"] = {"status": "verified", "checked_at": NOW,
                                             "url": approved_variant["source_url"],
                                             "http_status": 200}
    assert _check_gates(approved_variant, default_settings(), gate_now) == "missing published_at"


@pytest.mark.asyncio
async def test_research_feed_atom_supported():
    cfg = {"id": "arxiv-cs-ai", "name": "arXiv cs.AI", "url": "https://export.arxiv.org/rss/cs.AI"}
    with patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = (_feed_response(ARXIV_ATOM), None)
        candidates, status = await fetch_official_feed(None, cfg, NOW)
    assert status["status"] == "ok" and len(candidates) == 1
    assert candidates[0]["evidence_kind"] == "official_feed"
    assert "Abstract-only" in candidates[0]["what"]


@pytest.mark.asyncio
async def test_malformed_feed_reports_error_not_crash():
    cfg = {"id": "official-x", "name": "X", "url": "https://x.example.com/feed"}
    with patch("intel_sources.daily_sources._safe_get", new_callable=AsyncMock) as mock_get:
        mock_get.return_value = (_feed_response("this is not xml <<<>>>"), None)
        candidates, status = await fetch_official_feed(None, cfg, NOW)
    assert candidates == []
    assert status["status"] == "error" and status["error_code"] == "malformed_feed"


@pytest.mark.asyncio
async def test_github_releases_normalizes_and_bounds():
    cfg = {"id": "github-releases-tracked", "name": "Tracked releases",
           "repos": ["ollama/ollama", "ggml-org/llama.cpp", "not-a-repo", "a/b/c"]}
    release = {"tag_name": "v0.9.1", "name": "v0.9.1 — faster local inference",
               "body": "Release notes with <b>markup</b> and details.",
               "html_url": "https://github.com/ollama/ollama/releases/tag/v0.9.1",
               "published_at": "2026-10-25T00:00:00Z"}

    async def fake_json(client, url, extra_headers=None):
        if "ollama/ollama" in url:
            return dict(release)
        raise Exception("no releases")

    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock, side_effect=fake_json):
        candidates, status = await fetch_github_releases(None, cfg, NOW)
    assert status["status"] == "ok"
    assert len(candidates) == 1
    cand = candidates[0]
    assert cand["id"] == "github-release:ollama/ollama@v0.9.1"
    assert cand["version"] == "v0.9.1"
    assert cand["evidence_kind"] == "repo_releases"
    assert "<b>" not in cand["what"]  # untrusted release body sanitized
    assert cand["published_at"] == "2026-10-25T00:00:00Z"


@pytest.mark.asyncio
async def test_github_releases_honors_rate_limit_and_stops():
    cfg = {"id": "github-releases-tracked", "name": "Tracked releases",
           "repos": ["owner/one", "owner/two", "owner/three"]}
    calls = []

    async def fake_json(client, url, extra_headers=None):
        calls.append(url)
        if "owner/one" in url:
            return {"tag_name": "v1", "html_url": "https://github.com/owner/one/releases/v1",
                    "name": "v1", "body": "ok"}
        response = httpx.Response(429, headers={"Retry-After": "60"},
                                  request=httpx.Request("GET", url))
        raise httpx.HTTPStatusError("rate limited", request=response.request, response=response)

    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock, side_effect=fake_json):
        candidates, status = await fetch_github_releases(None, cfg, NOW)
    assert status["status"] == "error" and status["error_code"] == "rate_limited"
    assert "60" in status["detail"]  # Retry-After recorded, not ignored
    assert len(calls) == 2  # remaining repos NOT fanned out after the 429
    assert len(candidates) == 1  # first release kept


def test_replace_so_reference_zero_network_and_validated_rows():
    cfg = {"id": "replace-so-reference", "name": "Replace.so references",
           "references": [
               {"tool": "OpenSource Chat", "note": "Self-hostable chat UI alternative with documented deployment.",
                "upstream_repo_url": "https://github.com/example/chat-ui?utm_source=dir"},
               {"tool": "Bad row", "note": "short", "upstream_repo_url": "https://github.com/x/y"},
               {"tool": "Bad URL", "note": "This note is long enough for the gate.",
                "upstream_repo_url": "javascript:alert(1)"},
           ]}
    candidates, status = fetch_replace_so_reference(cfg, NOW)
    assert status["status"] == "ok" and status["returned"] == 3
    assert len(candidates) == 1  # invalid/short rows skipped
    cand = candidates[0]
    assert cand["id"] == "replace-so:example/chat-ui"
    assert cand["evidence_kind"] == "manual_reference"
    assert cand["source_url"] == "https://github.com/example/chat-ui"
    assert cand["free"]["status"] == "unknown"  # directory listing is never license evidence


def test_normalize_feed_entry_skips_missing_link_or_title():
    assert normalize_feed_entry({"title": "no link"}, "s", "S", NOW, "u") is None
    assert normalize_feed_entry({"link": "https://x.example.com/a"}, "s", "S", NOW, "u") is None


# ---------------------------------------------------------------------------
# Hugging Face Hub models (documented API, metadata only, no inference)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_hf_models_normalizes_bounded_metadata():
    from intel_sources.expanded_sources import fetch_hf_models
    rows = [
        {"modelId": "org/model-a", "lastModified": "2026-10-25T00:00:00.000Z",
         "likes": 12, "downloads": 3400},
        {"id": "org/model-b", "lastModified": None},
        {"modelId": ""},
    ]
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               return_value=rows):
        candidates, status = await fetch_hf_models(
            None, {"id": "hf-models-hub", "name": "HF models", "limit": 10}, NOW)
    assert status["status"] == "ok"
    assert len(candidates) == 2
    first = candidates[0]
    assert first["id"] == "hf:org/model-a"
    assert first["evidence_kind"] == "documented_api"
    assert first["date_kind"] == "updated_at"
    assert first["published_at"] == "2026-10-25T00:00:00.000Z"
    assert "not a benchmark" in first["what"]
    assert first["free"]["status"] == "unknown"  # metadata is never license evidence
    # bounded API request carries the documented limit
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as mock_json:
        mock_json.return_value = []
        await fetch_hf_models(None, {"id": "hf-models-hub", "limit": 99}, NOW)
    requested = mock_json.await_args.args[1]
    assert "limit=30" in requested  # clamped to the documented bound


@pytest.mark.asyncio
async def test_hf_models_failure_is_honest_source_error():
    from intel_sources.expanded_sources import fetch_hf_models
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock,
               side_effect=TimeoutError("timed out")):
        candidates, status = await fetch_hf_models(None, {"id": "hf-models-hub"}, NOW)
    assert candidates == []
    assert status["status"] == "error" and status["error_code"] == "source_failed"
