"""Report contract, original design, safety and real-browser regression tests."""
from pathlib import Path
import hashlib
import re

from bs4 import BeautifulSoup
import pytest

from tracker_core.reporter_html import HTMLReporter

ROOT = Path(__file__).resolve().parents[1]
ORIGINAL_STYLE_SHA256 = "347adbd0fc894df1675f565ca9d93d591a320ac2b63934b0d7615fa3554e76b8"


def render(theme="lain", **kwargs):
    return HTMLReporter(theme).render(
        api_items=kwargs.pop("api_items", []),
        report_date="2026-09-30", generated_at="08:00 UTC", **kwargs
    )


def soup(theme="lain", **kwargs):
    return BeautifulSoup(render(theme, **kwargs), "html.parser")


def test_lain_preserves_exact_original_locked_style():
    for folder in (ROOT / "src/templates", ROOT / "src/tracker-core/templates"):
        actual = (folder / "report_template_lain.html").read_text(encoding="utf-8")
        locked = re.search(r"^<style>\n.*?</style>", actual, re.S | re.M).group()
        assert hashlib.sha256(locked.encode()).hexdigest() == ORIGINAL_STYLE_SHA256
    html = soup()
    assert html.title.text == "lain — tracker report — 2026-09-30"
    assert html.select_one("header.report-head .portrait")
    assert html.select_one("header.report-head h1").text == "lain // tracker report"


@pytest.mark.parametrize("theme", ["lain", "default"])
def test_changes_render_before_inventory_and_save_propagates(theme, tmp_path):
    changes = [{"headline": "Quota changed", "source": "official", "summary": "Now lower",
                "url": "https://example.org/change", "change_type": "updated"}]
    kwargs = dict(change_items=changes, baseline=True,
                  api_items=[{"name": "API", "status": "stable", "description": "Inventory"}])
    html = soup(theme, **kwargs)
    sections = html.select("section.tracker")
    assert sections[0]["id"] == "changes"
    assert "Quota changed" in sections[0].text
    assert "updated" in sections[0].text
    assert "official" in sections[0].text
    assert "First run" in sections[0].text
    assert "API" in html.text
    out = HTMLReporter(theme).save(tmp_path / "nested/report.html", **kwargs,
                                    report_date="2026-09-30", generated_at="08:00 UTC")
    assert out.read_text(encoding="utf-8") == render(theme, **kwargs)
    assert "No material changes" in soup(theme).select_one("#changes").text
    baseline = soup(theme, baseline=True).select_one("#changes").text
    assert "First run" in baseline
    assert "No material changes" not in baseline


@pytest.mark.parametrize("theme", ["lain", "default"])
def test_operational_digest_reports_actions_source_health_and_actual_runtime(theme, tmp_path):
    kwargs = dict(
        action_items=[{"title": "Refresh key", "priority": "high", "reason": "HTTP 401",
                       "action": "Rotate credentials locally", "url": "https://example.org/keys"}],
        source_items=[{"name": "GitHub", "status": "error", "checked_at": "2026-09-29T00:00Z",
                       "item_count": 0, "error": "HTTP 429", "url": "https://github.com",
                       "coverage": "partial"}],
        runtime_source="GitHub Actions / CI")
    html = soup(theme, **kwargs)
    actions = html.select_one("#actions").get_text(" ")
    for value in ("Refresh key", "high", "HTTP 401", "Rotate credentials locally"):
        assert value in actions
    health = html.select_one("#source-health").get_text(" ")
    for value in ("GitHub", "error", "2026-09-29T00:00Z", "0 items", "HTTP 429", "partial"):
        assert value in health
    assert "GitHub Actions / CI" in html.footer.text
    assert "generated on local host" not in html.footer.text
    assert "runtime not supplied" in soup(theme).footer.text
    empty = soup(theme)
    assert "No actions supplied" in empty.select_one("#actions").text
    assert "Source health not supplied" in empty.select_one("#source-health").text
    assert "not a probe timestamp" in html.footer.text
    path = HTMLReporter(theme).save(tmp_path / "operational.html", [], **kwargs,
                                    report_date="2026-09-30", generated_at="08:00 UTC")
    assert path.read_text(encoding="utf-8") == render(theme, **kwargs)


@pytest.mark.parametrize("theme", ["lain", "default"])
def test_inventory_freshness_is_explicit_with_expandable_escaped_evidence(theme):
    evidence = [{"source": "local probe", "checked_at": "2026-09-29T01:00Z",
                 "result": "<script>alert(1)</script>", "url": "https://example.org/evidence"}]
    apis = [{"name": freshness, "status": "updated", "description": "Legacy description",
             "actual_status": "live", "freshness": freshness,
             "checked_at": "2026-09-29T01:00Z", "probe_source": "local probe",
             "evidence": evidence} for freshness in ("fresh", "stale", "unverified")]
    apis.append({"name": "legacy", "status": "stable", "description": "Old API contract"})
    tools = [{"id": "model:one", "headline": "Model One", "source": "Model hub", "summary": "Local model",
              "url": "https://example.org/model", "project_types": ["local-llm", "automation"],
              "freshness": "stale", "change_type": "updated", "evidence": evidence}]
    html = soup(theme, api_items=apis, tool_items=tools)
    rows = html.select("#providers li.item")
    assert len(rows) == 4
    for row, freshness in zip(rows, ("fresh", "stale", "unverified", "unverified")):
        assert row.select_one(".freshness").text.strip() == freshness
    assert "Actual status: live" in rows[0].text
    assert "local probe" in rows[0].text
    assert "2026-09-29T01:00Z" in rows[0].text
    assert "not supplied" in rows[-1].text
    assert "Old API contract" in rows[-1].text
    details = rows[0].select_one("details.evidence")
    assert details is not None and "open" not in details.attrs
    assert "Evidence" in details.summary.text
    assert "<script>alert(1)</script>" in details.text
    assert not details.find("script")
    discovery = html.select_one("#discoveries li.item")
    for value in ("Model One", "Model hub", "Local model", "local-llm", "automation", "stale", "updated", "model:one"):
        assert value in discovery.text
    assert discovery.select_one("details.evidence")
    empty = soup(theme)
    assert "No providers supplied" in empty.select_one("#providers").text
    assert "No discoveries supplied" in empty.select_one("#discoveries").text
    assert "No stories supplied" in empty.select_one("#news").text


@pytest.mark.parametrize("theme", ["lain", "default"])
@pytest.mark.parametrize("url", ["javascript:alert(1)", "JaVaScRiPt:alert(1)", "java\nscript:alert(1)",
    "data:text/html,<script>alert(1)</script>", "//evil.example/x", "/relative", "https:///missing-host",
    "https://example.org\\evil", "https://example.org/\x00x", "https://[broken", None])
def test_unsafe_links_are_inert_and_all_untrusted_fields_are_escaped(theme, url):
    malicious = '\"><img src=x onerror="window.pwned=1"><script>window.pwned=1</script>'
    standard = dict(headline=malicious, source=malicious, summary=malicious, url=url)
    html = soup(theme, api_items=[dict(name=malicious, status=malicious, description=malicious,
                    actual_status=malicious, probe_source=malicious, evidence=[{"result": malicious, "url": url}])],
                tool_items=[dict(**standard, id=malicious, project_types=[malicious], evidence=[{"url": url}])],
                news_items=[standard], change_items=[dict(**standard, change_type=malicious)],
                action_items=[dict(title=malicious, priority=malicious, reason=malicious, action=malicious, url=url)],
                source_items=[dict(name=malicious, status=malicious, error=malicious, coverage=malicious, url=url)],
                persona_image_url=url, runtime_source=malicious)
    # Trusted dashboard navigation/branding is separate from untrusted links.
    assert all(a['href'] in {'#discoveries', '#source-health'} for a in html.select("a[href]"))
    from tracker_core.branding import avatar_data_uri
    assert all(image['src'] == avatar_data_uri() for image in html.select('img'))
    assert not html.select('#providers img, #discoveries img, #news img')
    assert not html.select("[onerror], [onclick]")
    assert all("window.pwned" not in script.text for script in html.select("script"))
    assert malicious in html.select_one("#providers").text


@pytest.mark.parametrize("theme", ["lain", "default"])
def test_http_https_links_remain_clickable(theme):
    urls = ["http://example.org/a", "https://example.org/a?x=1&y=2"]
    html = soup(theme, news_items=[dict(headline=url, source="test", summary="ok", url=url) for url in urls],
                persona_image_url=urls[1])
    assert [a["href"] for a in html.select("#news a")] == urls
    if theme == "lain":
        assert html.select_one(".portrait img")["src"] == urls[1]


@pytest.fixture(scope="module")
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        import os
        executable = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
        local_browser_missing = not Path(playwright.chromium.executable_path).is_file()
        if not executable and os.getenv("GITHUB_ACTIONS") != "true" and local_browser_missing:
            pytest.skip("Chromium is not installed locally; install it or set PLAYWRIGHT_EXECUTABLE_PATH")
        instance = playwright.chromium.launch(headless=True, executable_path=executable)
        yield instance
        instance.close()


@pytest.mark.parametrize("required_mode", ["ci", "explicit", "installed"])
def test_browser_launch_failure_is_not_hidden_as_skip(monkeypatch, tmp_path, required_mode):
    from contextlib import nullcontext
    from types import SimpleNamespace
    import playwright.sync_api

    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("PLAYWRIGHT_EXECUTABLE_PATH", raising=False)
    executable = tmp_path / "installed-browser.exe"
    if required_mode == "ci":
        monkeypatch.setenv("GITHUB_ACTIONS", "true")
    elif required_mode == "explicit":
        monkeypatch.setenv("PLAYWRIGHT_EXECUTABLE_PATH", str(executable))
    else:
        executable.touch()

    def failed_launch(**kwargs):
        raise RuntimeError("Browser startup failed")

    chromium = SimpleNamespace(executable_path=str(executable), launch=failed_launch)
    monkeypatch.setattr(playwright.sync_api, "sync_playwright", lambda: nullcontext(SimpleNamespace(chromium=chromium)))
    iterator = browser.__wrapped__()
    try:
        next(iterator)
    except BaseException as exc:
        assert isinstance(exc, RuntimeError), "Browser failure was silently converted to a skip"
        assert str(exc) == "Browser startup failed"
    else:
        pytest.fail("A failed browser launch must propagate")


@pytest.mark.parametrize("theme", ["lain", "default"])
@pytest.mark.parametrize("width", [390, 1024])
def test_browser_filters_intersect_reset_and_evidence_is_safe(browser, theme, width):
    malicious = '<img src=x onerror="window.pwned=1">'
    tools = [
        dict(headline="Alpha DB", source="GitHub", summary="ETL pipelines", url="https://example.org/a",
             project_types=["data-engineering", "automation"], evidence=[{"result": "HTTP 200"}]),
        dict(headline="Beta Model", source="Model Hub", summary="Local inference", url="https://example.org/b",
             project_types=["local-llm"]),
        dict(headline=malicious, source=malicious, summary="Untrusted row", url="javascript:alert(1)",
             project_types=[malicious], evidence=[{"result": malicious}]),
    ]
    page = browser.new_page(viewport={"width": width, "height": 900}, color_scheme="dark")
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.set_content(render(theme, tool_items=tools), wait_until="load")
        search = page.get_by_label("Search discoveries", exact=True)
        assert search.count() == 1
        project = page.get_by_label("Project type", exact=True)
        source = page.get_by_label("Discovery source", exact=True)
        rows = page.locator("#discoveries .discovery-item:visible")
        assert rows.count() == 3
        assert page.locator("#discovery-result-count").text_content() == (
            "3 discoveries, no filter applied" if theme == "lain" else RESULT_COUNT["default"].format(3, 3))
        search.fill("etl")
        assert rows.count() == 1
        project.select_option("local-llm")
        assert rows.count() == 0
        assert page.get_by_text("No discoveries match these filters.", exact=True).is_visible()
        search.fill("")
        assert rows.count() == 1
        source.select_option("GitHub")
        assert rows.count() == 0
        project.select_option("")
        assert rows.count() == 1
        source.select_option("")
        assert rows.count() == 3
        project.select_option(malicious)
        assert rows.count() == 1
        assert page.evaluate("window.pwned || false") is False
        assert page.locator("#discoveries img").count() == 0
        project.select_option("")
        page.locator("#discoveries details.evidence summary").first.click()
        assert page.locator("#discoveries details.evidence").first.get_attribute("open") is not None
        assert page.get_by_text("HTTP 200", exact=False).is_visible()
        if theme == "lain":
            assert page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(5, 7, 10)"
            assert page.locator("h1").evaluate("el => getComputedStyle(el).color") == "rgb(111, 255, 233)"
            assert page.locator(".portrait").evaluate("el => getComputedStyle(el).width") == "64px"
            assert page.locator(".wrap").evaluate("el => getComputedStyle(el).maxWidth") == "1120px"
            assert page.locator("body").evaluate("el => getComputedStyle(el, '::before').pointerEvents") == "none"
            assert page.locator("h1").evaluate("el => getComputedStyle(el).animationDuration") == "0.9s"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        assert not errors
    finally:
        page.close()


# The default theme keeps its original wording; the Lain report states the
# filter status truthfully, since its catalog starts collapsed.
RESULT_COUNT = {"default": "{} of {} discoveries shown"}
NO_JS_NOTICE = {
    "lain": "Filters require JavaScript; every collected row is still reachable below, in the collapsible inventory archive.",
    "default": "Filters require JavaScript; all discoveries are shown below.",
}


def test_template_syntax_errors_are_not_hidden_by_default_fallback(tmp_path, monkeypatch):
    from jinja2 import TemplateSyntaxError
    monkeypatch.setattr(HTMLReporter, "TEMPLATE_DIR", tmp_path)
    (tmp_path / "report_template_lain.html").write_text("{% if %}", encoding="utf-8")
    (tmp_path / "report_template.html").write_text("fallback", encoding="utf-8")
    with pytest.raises(TemplateSyntaxError):
        HTMLReporter("lain").render([])
    (tmp_path / "report_template_lain.html").unlink()
    assert HTMLReporter("lain").render([]) == "fallback"


@pytest.mark.parametrize("name,style_hash", [
    ("report_template_lain.html", "347adbd0fc894df1675f565ca9d93d591a320ac2b63934b0d7615fa3554e76b8"),
    ("report_template.html", "658269321258ed6bd5230d489a3c75d0e5f76c19a1d1913b8df28548a4ad51e1"),
])
def test_active_and_packaged_templates_never_drift_and_styles_are_locked(name, style_hash):
    active = ROOT / "src/templates" / name
    packaged = ROOT / "src/tracker-core/templates" / name
    assert active.read_bytes() == packaged.read_bytes()
    style = re.search(r"^<style>\n.*?</style>", active.read_text(encoding="utf-8"), re.S | re.M).group()
    assert hashlib.sha256(style.encode("utf-8")).hexdigest() == style_hash
    assert HTMLReporter.TEMPLATE_DIR.resolve() == active.parent.resolve()


def test_original_lain_edit_rules_are_literal_comments_not_jinja_programs():
    text = (ROOT / "src/templates/report_template_lain.html").read_text(encoding="utf-8")
    assert text.startswith('{% raw %}<!--\n  REPORT TEMPLATE — "lain" theme — read before editing')
    assert "LOCKED   — <style> block, section/markup structure." in text
    html = render()
    assert "the two {% for %} loops" in html
    assert "$ free-api-tracker" in html
    assert "$ ai-news-tracker" in html


@pytest.mark.parametrize("theme", ["lain", "default"])
def test_legacy_positional_render_arguments_and_missing_optional_fields(theme):
    reporter = HTMLReporter(theme)
    api = [{"name": "Legacy", "status": "new", "description": "Original contract"}]
    tools = [{"headline": "Legacy tool", "source": "GitHub", "summary": "Original tool", "url": "https://example.org"}]
    html = BeautifulSoup(reporter.render(api, None, tools, "2026-09-30", "08:00 UTC", None), "html.parser")
    assert "Legacy" in html.text and "Original contract" in html.text
    assert "Legacy tool" in html.text
    assert html.select_one("#discoveries .freshness").text == "unverified"


@pytest.mark.parametrize("theme", ["lain", "default"])
def test_browser_empty_inventory_and_javascript_disabled_fallback(browser, theme):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.set_content(render(theme), wait_until="load")
        assert page.get_by_text("No discoveries supplied.", exact=True).is_visible()
        assert page.locator("#discovery-result-count").text_content() == (
            "0 discoveries, no filter applied"
            if theme == "lain" else RESULT_COUNT["default"].format(0, 0))
        assert not page.locator("#discovery-empty").is_visible()
        assert page.locator("script[src], link[rel=stylesheet]").count() == 0
        assert not errors
    finally:
        page.close()
    context = browser.new_context(java_script_enabled=False)
    try:
        page = context.new_page()
        page.set_content(render(theme, tool_items=[dict(headline="Visible without JS", source="GitHub",
                            summary="Static fallback", url="https://example.org")]), wait_until="load")
        assert page.get_by_text("Visible without JS", exact=True).is_visible()
        assert page.get_by_text(NO_JS_NOTICE[theme], exact=True).is_visible()
    finally:
        context.close()
