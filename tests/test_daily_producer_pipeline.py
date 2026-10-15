"""TDD tests for the producer runtime repair: missing-why and HTTPS sanitizer.

Fixtures are synthetic/construction-classified. Adapters are exercised through
their actual constructors with injected JSON/HTML response bodies at the parser
seam (_fetch_html / _fetch_json), never through a mocked successful application.
"""
import asyncio
import json
import os
import socket
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from intel_sources.daily_digest import (
    _sanitize_text, _check_gates, build_digest,
)
from intel_sources.daily_sources import (
    _fetch_github_trending, _fetch_skills_hot, _fetch_unorouter, _fetch_openrouter,
)

# ---- Fixed clock for deterministic date/score assertions ----
NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def _settings():
    return {
        "schema_version": 1,
        "sources": [],
        "categories": ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"],
        "max_items": 5,
        "max_age_days": 7,
        "min_score": 60,
        "priorities": {"free-api": 5, "models": 4, "agents": 3, "mcp": 2, "skills": 2, "devtools": 1, "news": 1},
        "topics": ["free-api", "models", "agents"],
    }


def _collection(cands):
    return {"candidates": cands, "sources": []}


def _GENERIC_WHYS(w):
    """Helper: detect placeholder/generic why strings."""
    if not w:
        return True
    wl = w.strip().lower()
    for g in ("relevant", "important", "useful", "interesting", "good", "bad", "new",
              "old", "updated", "related", "trending", "popular", "top", "best",
              "recommended", "none", "n/a", "unknown", "placeholder", "generic",
              "vague", "short", "brief", "minimal", "not applicable"):
        if wl == g:
            return True
    return False


# ---- Synthetic upstream payloads (fixture: synthetic; not real fetch) ----

def _github_html():
    return """
    <html><body>
    <article class="Box-row">
      <h2><a href="/morluto/rea">morluto / rea</a></h2>
      <p>Reverse engineer anything with agents, from app behavior down to native binaries.</p>
      <span itemprop="programmingLanguage">Python</span>
    </article>
    <article class="Box-row">
      <h2><a href="/storytold/artcraft">storytold / artcraft</a></h2>
      <p>ArtCraft is an intentional crafting engine for artists, designers, and filmmakers</p>
      <span itemprop="programmingLanguage">TypeScript</span>
    </article>
    </body></html>
    """


def _unorouter_json():
    return {"models": [
        {"model_name": "step-5-preview", "vendor": "StepFun", "is_free": False,
         "release_ts": 1791462849000, "input_price": 2, "output_price": 5.4, "fixed_price": 0},
        {"model_name": "claude-haiku-5-5", "vendor": "Anthropic", "is_free": False,
         "release_ts": 1791397883000, "input_price": 0.0432, "output_price": 0.216, "fixed_price": 0},
        {"model_name": "gemini-nano-banana-2.1", "vendor": "Google", "is_free": False,
         "release_ts": 1791244800000, "input_price": 0, "output_price": 0, "fixed_price": 0.02384},
    ]}


def _skills_html():
    return """
    <html><head><meta name="description" content="Design mobile app screens with Sleek, edit Sleek projects, and implement their designs in React Native or HTML."></meta></head><body>
    <a href="/designed-by-ai/skills/design-mobile-apps"><h3>Design Mobile Apps</h3><p>Build mobile UIs.</p></a>
    <a href="/vercel-labs/skills/find-skills"><h3>Find Skills</h3><p>Search skills.</p></a>
    </body></html>
    """


def _openrouter_json():
    return {"data": [
        {"id": "openai/gpt-6.1-sol", "name": "OpenAI GPT", "description": "A model.", "created": 1791300825, "pricing": {"prompt": 0}},
        {"id": "google/gemini-nano-banana-2.1", "name": "Google Nano Banana", "description": "An image model.", "created": 1791300825, "pricing": {"prompt": 0}},
    ]}


# ===========================================================================
# PART 1 -- Missing why (actual adapter constructors)
# ===========================================================================

@pytest.mark.asyncio
async def test_github_trending_includes_factual_why():
    """GitHub candidates must carry a truthful, source-backed why (not None)."""
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock) as mock_html, \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as mock_json:
        mock_html.return_value = _github_html()
        mock_json.return_value = {"full_name": "morluto/rea", "license": {"spdx_id": "MIT"}}
        cands, status = await _fetch_github_trending(None, NOW.isoformat())
    assert len(cands) > 0
    for c in cands:
        assert c["why"], f"GitHub candidate {c['id']} missing why"
        assert not _GENERIC_WHYS(c["why"])


@pytest.mark.asyncio
async def test_skills_hot_includes_factual_why():
    """Skills.sh candidates must carry a truthful, source-backed why."""
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock) as mock_html, \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as mock_json:
        mock_html.return_value = _skills_html()
        # License lookup fails (mock returns default dict) → skill free status
        # becomes 'unknown', but the detail-page description still yields a
        # factual why, which is what this test validates.
        mock_json.return_value = {}
        cands, status = await _fetch_skills_hot(None, NOW.isoformat())
    assert len(cands) > 0
    for c in cands:
        assert c["why"], f"Skills candidate {c['id']} missing why"
        assert not _GENERIC_WHYS(c["why"])


@pytest.mark.asyncio
async def test_unorouter_includes_factual_why():
    """UnoRouter candidates must carry a truthful, source-backed why."""
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as mock_json:
        mock_json.return_value = _unorouter_json()
        cands, status = await _fetch_unorouter(None, NOW.isoformat())
    assert len(cands) > 0
    for c in cands:
        assert c["why"], f"UnoRouter candidate {c['id']} missing why"
        assert not _GENERIC_WHYS(c["why"])


@pytest.mark.asyncio
async def test_openrouter_why_remains_factual():
    """OpenRouter already has why; verify it stays factual after the repair."""
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as mock_json:
        mock_json.return_value = _openrouter_json()
        cands, status = await _fetch_openrouter(None, NOW.isoformat())
    assert len(cands) > 0
    for c in cands:
        assert c["why"]
        assert not _GENERIC_WHYS(c["why"])


# ===========================================================================
# PART 2 -- Missing why gate integration
# ===========================================================================

def _gated_candidate(source_id="github-trending", url="https://github.com/morluto/rea"):
    name = url.replace("https://github.com/", "")
    return {
        "id": f"test:{name}",
        "name": name,
        "what": "Reverse engineer anything with agents, from app behavior down to native binaries.",
        "category": "devtools",
        "source_id": source_id,
        "source_url": url,
        "published_at": (NOW - timedelta(days=1)).isoformat(),
        "date_kind": "trending",
        "observed_at": NOW.isoformat(),
        "free": {"status": "open-source", "detail": "GitHub public repo", "evidence_url": url},
        "api": {"status": "none", "detail": "", "evidence_url": ""},
        "topics": ["devtools"],
        "link_verification": {"status": "verified", "checked_at": NOW.isoformat(), "url": url, "http_status": 200},
    }


def test_github_candidate_with_none_why_rejected_by_gate():
    """A GitHub candidate with None why must be rejected by the gate (defect)."""
    cand = _gated_candidate()
    cand["why"] = None
    reason = _check_gates(cand, _settings(), NOW)
    assert reason == "generic why"


def test_github_candidate_with_repaired_why_passes_gates():
    """Same candidate, now with a source-backed why, must pass the gate."""
    cand = _gated_candidate()
    cand["why"] = "GitHub Trending AI repository: Reverse engineer anything with agents, from app behavior down to native binaries."
    reason = _check_gates(cand, _settings(), NOW)
    assert reason is None


# ===========================================================================
# PART 3 -- HTTPS sanitizer: legitimate HTTPS URLs survive unchanged
# ===========================================================================

@pytest.mark.parametrize("url", [
    "https://openrouter.ai/stepfun/step-5-preview",
    "https://openrouter.ai/anthropic/claude-haiku-5.5:batch",
    "https://github.com/morluto/rea",
    "https://github.com/boykopovar/AnyPS5",
    "https://skills.sh/trending",
    "https://api.unorouter.com/api/pricing/catalog",
    "https://example.com/var/log",
    "https://example.com/tmp/test",
    "https://example.com/home/page",
    "https://example.com/usr/local",
    "https://example.com/etc/config",
    "https://example.com/root/x",
    "https://example.com",
    "https://example.com/",
    "https://example.com/path?q=1&r=2",
    "https://example.com/path#frag",
    "https://example.com:8443/x",
    "http://example.com",
])
def test_sanitize_preserves_https_urls(url):
    """Valid HTTPS (and HTTP) URLs must survive _sanitize_text unchanged."""
    assert _sanitize_text(url) == url


@pytest.mark.parametrize("text", [
    "C:/Users/admin/secrets",
    "/home/user/.ssh/id_rsa",
    "/tmp/secret",
    "/var/log/auth.log",
    "/etc/passwd",
    "/usr/local/bin/tool",
    "/root/.bashrc",
    "config at /etc/nginx/nginx.conf here",
])
def test_sanitize_redacts_windows_and_posix_private_paths(text):
    """Embedded Windows/POSIX private paths must still be redacted."""
    assert "[PATH REDACTED]" in _sanitize_text(text)


@pytest.mark.parametrize("url", [
    "https://example.com/s:/foo",
    "https://x/s:/y",
    "https://openrouter.ai/path/s:/weird",
])
def test_sanitize_does_not_corrupt_url_with_s_colon_slash(url):
    """The s:/ substring of https:// must never trigger path redaction inside a URL."""
    assert _sanitize_text(url) == url


def test_build_digest_preserves_source_url_https():
    """build_digest projection must keep a verified HTTPS source_url intact."""
    cand = _gated_candidate(url="https://github.com/morluto/rea")
    cand["topics"] = ["free-api", "models", "agents"]  # ensure topic_match qualifies for min_score
    cand["why"] = "GitHub Trending AI repository: Reverse engineer anything with agents, from app behavior down to native binaries."
    digest = build_digest(_collection([cand]), _settings(), now=NOW)
    assert digest["counts"]["selected"] == 1
    item = digest["items"][0]
    assert item["source_url"] == "https://github.com/morluto/rea"
    assert "[PATH REDACTED]" not in item["source_url"]


@pytest.mark.parametrize("text", [
    'path="C:/Users/canary/private.json"',
    r'path="C:\Users\canary\private.json"',
    'path=/home/canary/private.json',
    'path="/var/canary/private.json"',
    '(/tmp/canary/private.json)',
    'file:///etc/canary.conf',
])
def test_sanitize_redacts_private_paths_after_punctuation(text):
    assert "[PATH REDACTED]" in _sanitize_text(text)


@pytest.mark.asyncio
async def test_adapter_why_does_not_infer_ai_or_licensing():
    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock) as upstream, \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as json_upstream:
        upstream.return_value = _github_html()
        # License lookup returns MIT so free-status is 'open-source'; the
        # assertion checks WHY, not FREE — the why line must still be factual.
        json_upstream.return_value = {"full_name": "morluto/rea", "license": {"spdx_id": "MIT"}}
        github, _ = await _fetch_github_trending(None, NOW.isoformat())
    with patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as upstream:
        upstream.return_value = {"models": [{"vendor": "Acme", "model_name": "Widget", "is_free": True,
                                              "release_ts": 1791462849000, "input_price": 0, "output_price": 0, "fixed_price": 0}]}
        models, _ = await _fetch_unorouter(None, NOW.isoformat())
    assert github and models
    assert all("open-source" not in item["why"] for item in github + models)
    assert all("Trending AI" not in item["why"] for item in github)


@pytest.mark.asyncio
async def test_actual_adapter_digest_export_and_consumer_keep_original_link(tmp_path):
    from intel_sources.daily_public import export_digest
    from intel_sources.report_delivery import load_artifact

    with patch("intel_sources.daily_sources._fetch_html", new_callable=AsyncMock) as upstream, \
         patch("intel_sources.daily_sources._fetch_json", new_callable=AsyncMock) as json_upstream:
        upstream.return_value = _github_html()
        json_upstream.return_value = {"full_name": "morluto/rea", "license": {"spdx_id": "MIT"}}
        candidates, _ = await _fetch_github_trending(None, NOW.isoformat())
    candidate = candidates[0]
    candidate["link_verification"] = {
        "status": "verified", "checked_at": NOW.isoformat(),
        "url": candidate["source_url"], "http_status": 200,
    }  # Synthetic upstream/link evidence; not live-source acceptance.
    settings = _settings()
    # morluto/rea classifies as 'agents' (desc has 'agents'), priority 3 -> catpri 6.
    settings["topics"] = ["agents"]
    settings["priorities"]["agents"] = 5
    digest = build_digest(_collection([candidate]), settings, now=NOW)
    assert digest["counts"]["selected"] == 1
    output = export_digest(digest, tmp_path / "public")
    artifact = load_artifact(output.parent)
    assert artifact.data == output.read_bytes()
    assert candidate["source_url"].encode() in artifact.data
    assert "[PATH REDACTED]".encode() not in artifact.data
    assert dict(artifact.source_counts) == {"github-trending": 1}


def _normalize_windows_path(raw):
    """Normalize MSYS bash-mangled Windows paths like ``\\c\\Users\\...``.

    The bash shell mangles the leading drive letter into a backslash prefix;
    revert that so pathlib/Path().is_file() resolves correctly.
    """
    import re as _re
    m = _re.match(r"^\\([A-Za-z])\\(.*)$", raw)
    return m.group(1).upper() + ":" + os.sep + m.group(2) if m else raw


def _resolve_browser_executable(playwright):
    """Return the Chromium executable path honoring PLAYWRIGHT_EXECUTABLE_PATH.

    When the env var is set it is used verbatim and must exist on disk; a
    missing or invalid path is surfaced as a launch failure, never a skip.
    When it is unset, the managed Chromium bundled with the installed
    Playwright package is used.
    """
    explicit = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
    if explicit:
        path = Path(_normalize_windows_path(explicit))
        if not path.is_file():
            raise RuntimeError(
                f"Chromium executable does not exist at {explicit!r}"
            )
        return str(path)
    managed = _normalize_windows_path(playwright.chromium.executable_path)
    if not Path(managed).is_file():
        raise RuntimeError(
            f"Managed Chromium executable does not exist at {managed!r}"
        )
    return managed


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}])
@pytest.mark.parametrize("javascript_enabled", [True, False])
def test_genuine_generated_daily_artifact_in_chromium(tmp_path, viewport, javascript_enabled):
    import os
    from pathlib import Path
    from playwright.sync_api import sync_playwright

    work = Path(__file__).resolve().parents[1]
    index = work / "public/index.html"
    if not index.exists():
        pytest.skip("requires parent-bound genuine generated daily artifact")
    errors, requests = [], []
    with sync_playwright() as playwright:
        executable = _resolve_browser_executable(playwright)
        browser = playwright.chromium.launch(executable_path=executable)
        try:
            context = browser.new_context(viewport=viewport, java_script_enabled=javascript_enabled)
            context.set_offline(True)
            context.on("request", lambda request: requests.append(request.url))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(error))
            page.on("console", lambda message: errors.append(message.text) if message.type in {"error", "warning"} else None)
            page.goto(index.as_uri())
            assert page.locator('meta[name="elilain-report-kind"]').get_attribute("content") == "daily-digest-v1"
            assert page.get_by_role("heading", name="Source coverage health").is_visible()
            cards = page.locator("#daily-digest article")
            if cards.count():
                assert page.locator("#daily-digest .original-source").count() == cards.count()
            else:
                assert page.locator("#daily-digest .empty").inner_text() == "No items selected for this digest."
            for zoom in ("100%", "200%"):
                page.evaluate("zoom => document.body.style.zoom = zoom", zoom)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), page.evaluate("""() => ({
                    zoom:document.body.style.zoom, width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
                    elements:Array.from(document.querySelectorAll('body *')).map(el => {
                        const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                        return {tag:el.tagName,id:el.id,cls:el.className,right:r.right,width:r.width,
                            scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,font:s.font,
                            gridColumns:s.gridTemplateColumns,overflowWrap:s.overflowWrap};
                    }).filter(r => r.right > innerWidth + 0.5 || r.scrollWidth > r.clientWidth + 1).slice(0,20)
                })""")
            page.evaluate("document.body.style.zoom = '100%'")
            page.keyboard.press("Tab")
            assert page.locator(":focus").get_attribute("href") == "#main"
            assert page.locator("main#main").count() == 1
            page.screenshot(path=str(tmp_path / "genuine-daily.png"), full_page=True)
            if javascript_enabled:
                page.locator("#discovery-search").focus()
                page.keyboard.type("no-matching-public-item")
                assert page.locator("#discovery-empty").is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), page.evaluate("Array.from(document.querySelectorAll('body *')).filter(e => e.getBoundingClientRect().right > innerWidth + 1).slice(0, 12).map(e => ({tag:e.tagName,id:e.id,cls:e.className,width:e.getBoundingClientRect().width,right:e.getBoundingClientRect().right}))")
            else:
                assert page.locator("noscript").is_visible()
            assert not errors
            assert requests and all(url.startswith(("file:", "data:")) for url in requests)
            context.close()
        finally:
            browser.close()


@pytest.mark.parametrize("viewport", [{"width": 1440, "height": 1000}, {"width": 390, "height": 844}, {"width": 320, "height": 844}])
@pytest.mark.parametrize("javascript_enabled", [True, False])
def test_generated_digest_no_horizontal_overflow_at_200pct_zoom(tmp_path, viewport, javascript_enabled):
    """Synthetic layout stress through the real sanitized export, not Jinja rewrites."""
    from intel_sources.daily_public import export_digest
    from playwright.sync_api import sync_playwright

    timestamp = "2026-10-15T12:34:56.123456+00:00"
    item = _gated_candidate()
    item.update(name="Synthetic layout " + "longword" * 12,
                why="Synthetic regression checks public card wrapping, not live source acceptance.",
                score=60, observed_at=timestamp)
    item["free"] = {"status": "unknown", "detail": ""}
    item["api"] = {"status": "unknown", "detail": ""}
    item["link_verification"]["checked_at"] = timestamp
    artifact = export_digest({
        "schema_version": 1, "report_date": "2026-10-15", "generated_at": timestamp,
        "items": [item],
        "sources": [{"id": "github-trending", "name": "GitHub Trending", "adapter": "html", "enabled": True}],
        "history": [{"report_date": "2026-10-15", "generated_at": timestamp, "selected_count": 1}],
        "counts": {"fetched": 1, "eligible": 1, "rejected": 0, "selected": 1},
    }, tmp_path / "public")
    errors, requests = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=_resolve_browser_executable(playwright))
        try:
            context = browser.new_context(viewport=viewport, java_script_enabled=javascript_enabled)
            context.set_offline(True)
            context.on("request", lambda request: requests.append(request.url))
            page = context.new_page()
            page.on("pageerror", lambda error: errors.append(error))
            page.on("console", lambda message: errors.append(message.text) if message.type in {"error", "warning"} else None)
            page.goto(artifact.as_uri())
            assert page.locator("#daily-digest article").count() == 1
            assert page.locator("#daily-digest .original-source").count() == 1
            for zoom in ("100%", "200%"):
                page.evaluate("zoom => document.body.style.zoom = zoom", zoom)
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), page.evaluate("""() => ({
                    zoom:document.body.style.zoom, width:innerWidth, scrollWidth:document.documentElement.scrollWidth,
                    elements:Array.from(document.querySelectorAll('body *')).map(el => {
                        const r=el.getBoundingClientRect(), s=getComputedStyle(el);
                        return {tag:el.tagName,id:el.id,cls:el.className,right:r.right,width:r.width,
                            scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,font:s.font,
                            gridColumns:s.gridTemplateColumns,overflowWrap:s.overflowWrap};
                    }).filter(r => r.right > innerWidth + 0.5 || r.scrollWidth > r.clientWidth + 1).slice(0,20)
                })""")
                assert page.evaluate("Array.from(document.querySelectorAll('.meta-bar, footer, .grid')).every(el => el.scrollWidth <= el.clientWidth + 1)")
            assert not errors
            assert requests and all(url.startswith(("file:", "data:")) for url in requests)
            context.close()
        finally:
            browser.close()


def test_browser_selector_explicit_path_must_exist(monkeypatch):
    """An explicitly configured but missing browser must FAIL, not skip."""
    from playwright.sync_api import sync_playwright

    monkeypatch.setenv("PLAYWRIGHT_EXECUTABLE_PATH", "C:/nonexistent/missing-chrome.exe")
    with sync_playwright() as playwright:
        with pytest.raises(RuntimeError, match="does not exist"):
            _resolve_browser_executable(playwright)


def test_browser_selector_unset_uses_managed_chromium(monkeypatch, tmp_path):
    """Selector unit test; real Chromium launches are covered above."""
    from types import SimpleNamespace

    managed = tmp_path / "managed-chrome.exe"
    managed.touch()
    playwright = SimpleNamespace(chromium=SimpleNamespace(executable_path=str(managed)))
    monkeypatch.delenv("PLAYWRIGHT_EXECUTABLE_PATH", raising=False)
    assert _resolve_browser_executable(playwright) == str(managed)


def test_browser_selector_explicit_path_is_used_when_set(monkeypatch, tmp_path):
    """A valid explicit path wins without requiring a managed installation."""
    from types import SimpleNamespace

    explicit = tmp_path / "configured-chrome.exe"
    explicit.touch()
    playwright = SimpleNamespace(chromium=SimpleNamespace(executable_path="missing-managed-chrome"))
    monkeypatch.setenv("PLAYWRIGHT_EXECUTABLE_PATH", str(explicit))
    assert _resolve_browser_executable(playwright) == str(explicit)



