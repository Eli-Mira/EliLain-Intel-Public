"""The public boundary must fail closed; inputs remain internal."""
from datetime import datetime, timezone
import importlib.util
from pathlib import Path

import pytest

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
OBSERVED = "2026-09-30T14:04:04.769235+00:00"
FIRST_SEEN = "2026-09-29T13:53:05.402256+00:00"


def boundary():
    assert importlib.util.find_spec("intel_sources.public_report") is not None, "public export boundary is missing"
    from intel_sources import public_report
    return public_report


def public_row(source="openrouter-models", **overrides):
    row = {
        "id": "openrouter:model:org/model", "headline": "Org Model", "source": source,
        "url": "https://openrouter.ai/org/model", "summary": "PRIVATE_SUMMARY_CANARY",
        "checked_at": OBSERVED, "first_seen": FIRST_SEEN,
        "project_types": ["text", "tools", "PRIVATE_TYPE_CANARY"],
        "evidence": {"api_key": "PRIVATE_KEY_CANARY", "probe_evidence": "PRIVATE_PROBE_CANARY"},
        "fingerprint_data": {"endpoint": "http://localhost:3001/v1/models"},
        "account_id": "PRIVATE_ACCOUNT_CANARY", "key_id": "PRIVATE_KEY_ID_CANARY",
    }
    row.update(overrides)
    return row


def state_fixture():
    return {
        "items": [public_row(), public_row("freellmapi-models", headline="PRIVATE_ROUTER_CANARY"),
                  public_row("unorouter-accessible-models", headline="PRIVATE_ACCESS_CANARY")],
        "sources": [
            {"name": "openrouter-models", "status": "ok", "checked_at": OBSERVED,
             "url": "http://localhost:3001/PRIVATE_SOURCE_CANARY", "item_count": 999,
             "error": "PRIVATE_ERROR_CANARY", "coverage": "PRIVATE_COVERAGE_CANARY"},
            {"name": "freellmapi-models", "checked_at": OBSERVED, "error": "PRIVATE_ROUTER_CANARY"},
        ],
        "changes": [public_row(headline="PRIVATE_CHANGE_CANARY")],
        "provider_actions": [{"reason": "PRIVATE_ACTION_CANARY"}],
        "providers": [{"account_id": "PRIVATE_PROVIDER_CANARY"}],
        "runtime_source": "C:\\Users\\private\\PRIVATE_PATH_CANARY",
    }


def test_projection_only_emits_public_fields_not_private_operational_data():
    module = boundary()
    result = module.build_public_payload(state_fixture(), now=NOW, max_items_per_source=2)
    assert set(result) == {"api_items", "news_items", "tool_items", "source_items", "change_items",
                           "action_items", "report_date", "generated_at", "runtime_source",
                           "persona_image_url", "baseline", "inventory_total"}
    assert result["api_items"] == result["news_items"] == result["change_items"] == result["action_items"] == []
    assert result["persona_image_url"] is None
    assert len(result["tool_items"]) == len(result["source_items"]) == 1
    item = result["tool_items"][0]
    assert set(item) == {"id", "headline", "source", "url", "summary", "project_types", "freshness", "evidence", "popularity", "first_observed_at"}
    assert item["id"] == "openrouter:model:org/model"
    assert item["project_types"] == ["text", "tools"]
    assert item["evidence"] == [{"observed_at": OBSERVED, "first_observed_at": FIRST_SEEN}]
    assert item["freshness"] == "fresh"
    source = result["source_items"][0]
    assert set(source) == {"name", "status", "checked_at", "item_count", "url", "coverage"}
    assert source["item_count"] == 1  # exported count, never the private stored count
    assert source["url"] == "https://openrouter.ai/api/v1/models"
    assert "saved observations" in result["runtime_source"]
    assert "not a fresh fetch" in result["runtime_source"]
    assert "PRIVATE_" not in str(result)
    assert "localhost" not in str(result)
    assert "Inference and free entitlement unverified" in item["summary"]


@pytest.mark.parametrize("url", [
    "javascript:alert(1)", "file:///C:/private/state.json", "data:text/html,x", "//example.org/x",
    "https://user:password@example.org/a", "https://127.0.0.1/a", "https://10.0.0.2/a",
    "http://169.254.169.254/latest", "https://192.168.1.9/a", "https://[::1]/a",
    "https://[fe80::1]/a", "https://[fc00::1]/a", "https://localhost/a", "https://a.localhost/a",
    "https://router.local/a", "https://service.internal/a", "https://gateway.lan/a",
    "https://router.home.arpa/a", "https://intranet/a", "https://local.example.test/a",
    "https://2130706433/a", "https://0x7f000001/a", "https://0177.0.0.1/a",
    "https://127.1/a", "https://127%2e0%2e0%2e1/a", "https://example.org\\evil/a",
    "https://example.org/a\n", "https://example.org:bad/a", "https://example.org:3001/a",
    "https://example.org/a?api_key=canary", "https://example.org/a?%74oken=canary",
    "https://example.org/a#access_token=canary", "https://example.org/sk-canarycredential123456",
    "https://example.org/a%3Ftoken%3Dcanary", "https://example.org/a%252Ftoken%253Dcanary",
    "https://example.org/%2e%2e/private", "https://example.org./a", "https://[broken/a", None,
])
def test_public_urls_reject_private_hosts_credentials_ambiguity_and_secrets(url):
    module = boundary()
    assert hasattr(module, "public_http_url"), "public URL validator is missing"
    assert module.public_http_url(url) == ""


@pytest.mark.parametrize("url", ["https://openrouter.ai/org/model:free", "https://github.com/Org/Repo",
                                 "https://skills.sh/org/repo/skill", "http://example.org/article"])
def test_public_urls_preserve_valid_public_urls_exactly(url):
    module = boundary()
    assert hasattr(module, "public_http_url"), "public URL validator is missing"
    assert module.public_http_url(url) == url


@pytest.mark.parametrize("overrides", [
    {"url": "http://localhost:3001/private"},
    {"url": "https://openrouter.ai/org/model?token=URL_SECRET_CANARY"},
    {"url": "https://evil.example/org/model"},
    {"id": "freellmapi:model:PRIVATE_ID_CANARY"},
    {"id": "openrouter:model:org/different"},
    {"url": "https://openrouter.ai/api/v1/models"},
    {"url": "https://openrouter.ai/org/sk-privatecredential123456"},
    {"id": None},
])
def test_projection_rejects_wrong_source_identity_or_private_and_credential_links(overrides):
    state = state_fixture()
    state["items"] = [public_row(**overrides)]
    assert boundary().build_public_payload(state, now=NOW)["tool_items"] == []


@pytest.mark.parametrize("headline", [
    '<img src="x" onerror="window.pwned=1"><script>window.pwned=1</script>',
    "Bearer TITLE_SECRET_CANARY", "api_key=TITLE_SECRET_CANARY", "C:\\Users\\private\\TITLE_PATH_CANARY",
    "Traceback: TITLE_TRACE_CANARY http://localhost:3001/v1/models", "account_id=TITLE_ACCOUNT_CANARY",
    "Connection refused at 127.0.0.1:3001", "Host router.internal failed", "file:///var/private/TITLE_PATH_CANARY",
    "/var/private/TITLE_PATH_CANARY", "~/.config/TITLE_PATH_CANARY", "token TITLE_TOKEN_CANARY",
    {"secret": "TITLE_OBJECT_CANARY"},
])
def test_projection_replaces_unsafe_titles_with_validated_public_identifier(headline):
    state = state_fixture()
    state["items"] = [public_row(headline=headline)]
    result = boundary().build_public_payload(state, now=NOW)
    assert result["tool_items"][0]["headline"] == "org/model"
    assert "CANARY" not in str(result)
    assert "window.pwned" not in str(result)


def test_projection_bounds_deduplicates_and_sorts_without_mutating_state():
    import copy
    state = state_fixture()
    state["items"] = [public_row(id=f"openrouter:model:org/{name}", url=f"https://openrouter.ai/org/{name}")
                      for name in ("z", "a", "a", "b")]
    before = copy.deepcopy(state)
    result = boundary().build_public_payload(state, now=NOW, max_items_per_source=2)
    assert [r["id"] for r in result["tool_items"]] == ["openrouter:model:org/a", "openrouter:model:org/b"]
    assert state == before


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2", 201])
def test_projection_rejects_invalid_or_unbounded_limits(limit):
    with pytest.raises(ValueError, match="limit"):
        boundary().build_public_payload(state_fixture(), now=NOW, max_items_per_source=limit)


def test_projection_malformed_rows_are_ignored_and_naive_render_time_rejected():
    state = state_fixture()
    state["items"] += [None, [], {"source": []}, public_row(project_types=[{}, None, "text"])]
    state["sources"] += [None, [], {"name": []}]
    result = boundary().build_public_payload(state, now=NOW)
    assert len(result["tool_items"]) == 1
    with pytest.raises(ValueError, match="timezone"):
        boundary().build_public_payload(state_fixture(), now=datetime(2026, 10, 1))


def test_export_reads_saved_inputs_without_mutation_and_writes_only_index(tmp_path):
    import hashlib
    import json
    import sqlite3
    from bs4 import BeautifulSoup
    module = boundary()
    assert hasattr(module, "export_public_report"), "standalone HTML export is missing"
    state_path = tmp_path / "internal" / "intel_state.json"
    state_path.parent.mkdir()
    state_path.write_text(json.dumps(state_fixture()), encoding="utf-8")
    db_path = state_path.parent / "news.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY, source TEXT, title TEXT, url TEXT, published_date TEXT, fetched_date TEXT, summary TEXT)")
        conn.execute("INSERT INTO news_items VALUES (1, 'hackernews', 'PRIVATE_NEWS_CANARY', 'http://localhost/private', NULL, '2026-09-30 00:00:00', 'PRIVATE_NEWS_SUMMARY_CANARY')")
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (state_path, db_path)}
    out = module.export_public_report(state_path, db_path, tmp_path / "public", now=NOW, max_items_per_source=2)
    assert out == tmp_path / "public" / "index.html"
    assert sorted(p.name for p in out.parent.iterdir()) == ["index.html"]
    html = out.read_text(encoding="utf-8")
    assert "PRIVATE_" not in html
    assert "localhost" not in html
    soup = BeautifulSoup(html, "html.parser")
    assert soup.select_one("header.report-head h1").text == "lain // tracker report"
    assert len(soup.select("#discoveries .discovery-item")) == 1
    assert len(soup.select("script")) == 1  # the existing safe local filter
    assert not soup.select("script[src], link[rel=stylesheet], iframe, form")
    from tracker_core.branding import avatar_data_uri
    assert [image['src'] for image in soup.select('img')] == [avatar_data_uri()]
    assert "not a fresh fetch" in soup.footer.text
    assert OBSERVED in soup.text
    assert NOW.isoformat() in soup.footer.text
    assert before == {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
    assert sorted(p.name for p in state_path.parent.iterdir()) == ["intel_state.json", "news.db"]


def test_saved_input_reader_is_bounded_read_only_and_missing_db_is_not_created(tmp_path):
    import json
    import sqlite3
    module = boundary()
    assert hasattr(module, "read_saved_inputs"), "read-only input reader is missing"
    state_path = tmp_path / "intel_state.json"
    state_path.write_text(json.dumps(state_fixture()), encoding="utf-8")
    missing = tmp_path / "missing.db"
    state, news = module.read_saved_inputs(state_path, missing, news_limit=1)
    assert state == state_fixture() and news == [] and not missing.exists()
    db_path = tmp_path / "news.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE news_items (id INTEGER PRIMARY KEY, source TEXT, title TEXT, url TEXT, published_date TEXT, fetched_date TEXT, summary TEXT, raw_data TEXT)")
        conn.executemany("INSERT INTO news_items VALUES (?, 'hackernews', 'Saved title', 'https://example.org/news', NULL, '2026-09-30 00:00:00', 'Saved summary', 'PRIVATE_RAW_DATA')", [(i,) for i in range(4)])
    state, news = module.read_saved_inputs(state_path, db_path, news_limit=1)
    assert len(news) == 1
    assert set(news[0]) == {"source", "title", "url", "published_date", "fetched_date", "summary"}
    assert news[0]["title"] == "Saved title" and "raw_data" not in news[0]


@pytest.mark.parametrize("dirty_entry", ["intel_state.json", "news.db", "assets", "index.html"])
def test_export_refuses_dirty_output_directory_or_index_directory(tmp_path, dirty_entry):
    import json
    state_path = tmp_path / "intel_state.json"
    state_path.write_text(json.dumps(state_fixture()), encoding="utf-8")
    out = tmp_path / "public"
    out.mkdir()
    forbidden = out / dirty_entry
    if dirty_entry in {"assets", "index.html"}:
        forbidden.mkdir()
    else:
        forbidden.write_text("PRIVATE_DESTINATION_CANARY", encoding="utf-8")
    with pytest.raises(ValueError, match="directory|index"):
        boundary().export_public_report(state_path, tmp_path / "missing.db", out, now=NOW)
    assert sorted(p.name for p in out.iterdir()) == [dirty_entry]


def test_export_has_offline_csp_and_no_referrer_with_unchanged_original_style(tmp_path):
    import json
    import re
    from bs4 import BeautifulSoup
    state_path = tmp_path / "intel_state.json"
    state_path.write_text(json.dumps(state_fixture()), encoding="utf-8")
    module = boundary()
    out = module.export_public_report(state_path, tmp_path / "missing.db", tmp_path / "public", now=NOW)
    html = out.read_text(encoding="utf-8")
    soup = BeautifulSoup(html, "html.parser")
    csp = soup.find("meta", attrs={"http-equiv": "Content-Security-Policy"})
    assert csp is not None, "standalone public export needs an offline CSP"
    for required in ("default-src 'none'", "connect-src 'none'", "base-uri 'none'", "form-action 'none'", "script-src 'sha256-"):
        assert required in csp["content"]
    assert "script-src 'unsafe-inline'" not in csp["content"]
    assert soup.find("meta", attrs={"name": "referrer"})["content"] == "no-referrer"
    template = Path(__file__).resolve().parents[1] / "src/templates/report_template_lain.html"
    original_style = re.search(r"^<style>\n.*?</style>", template.read_text(encoding="utf-8"), re.S | re.M).group()
    assert original_style in html
    assert "Provider statuses, changes, actions and news are excluded" in soup.text


@pytest.mark.parametrize("markup", ['<script src="https://example.org/exfil.js"></script>',
                                    '<img src="https://example.org/track">',
                                    '<style>@import url(https://example.org/style.css);</style>'])
def test_export_fails_closed_if_template_introduces_remote_dependencies(tmp_path, monkeypatch, markup):
    import json
    from tracker_core.reporter_html import LainReporter
    state_path = tmp_path / "intel_state.json"
    state_path.write_text(json.dumps(state_fixture()), encoding="utf-8")
    original = LainReporter.render
    monkeypatch.setattr(LainReporter, "render", lambda self, **kwargs: original(self, **kwargs).replace("</head>", markup + "</head>"))
    with pytest.raises(ValueError, match="dependency"):
        boundary().export_public_report(state_path, tmp_path / "missing.db", tmp_path / "public", now=NOW)
    assert not (tmp_path / "public/index.html").exists()


def test_cli_exports_real_html_and_reports_hash_from_readback(tmp_path):
    import hashlib
    import json
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[1] / "scripts/export_public_report.py"
    assert script.is_file(), "public export CLI is missing"
    state_path = tmp_path / "intel_state.json"
    state_path.write_text(json.dumps(state_fixture()), encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), "--state", str(state_path),
                             "--news-db", str(tmp_path / "missing.db"), "--output-dir", str(tmp_path / "public"),
                             "--max-items-per-source", "2"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    metadata = json.loads(result.stdout)
    artifact = Path(metadata["artifact"])
    assert artifact == tmp_path / "public/index.html"
    assert metadata["sha256"] == hashlib.sha256(artifact.read_bytes()).hexdigest()
    assert metadata["bytes"] == artifact.stat().st_size
    assert metadata["source_counts"] == {"openrouter-models": 1}
    assert "PRIVATE_" not in artifact.read_text(encoding="utf-8")


def test_cli_failure_is_generic_and_does_not_publish_private_traces(tmp_path):
    import subprocess
    import sys
    script = Path(__file__).resolve().parents[1] / "scripts/export_public_report.py"
    assert script.is_file(), "public export CLI is missing"
    state_path = tmp_path / "intel_state.json"
    state_path.write_text('{"PRIVATE_INVALID_JSON_SECRET_CANARY"', encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), "--state", str(state_path),
                             "--news-db", str(tmp_path / "missing.db"), "--output-dir", str(tmp_path / "public")],
                            capture_output=True, text=True)
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "Public export failed; inputs or destination were rejected."
    assert not (tmp_path / "public/index.html").exists()


@pytest.mark.parametrize("bad_time", ["2026-09-30x14:04:04+00:00", "2026-09-30T14:04:04",
                                      "2099-01-01T00:00:00Z", "DATE_SECRET_CANARY"])
def test_provenance_accepts_only_aware_iso_observations_not_render_or_future_time(bad_time):
    state = state_fixture()
    state["items"] = [public_row(checked_at=bad_time, first_seen=bad_time)]
    state["sources"][0]["checked_at"] = bad_time
    result = boundary().build_public_payload(state, now=NOW)
    assert result["tool_items"][0]["freshness"] == "unverified"
    assert result["tool_items"][0]["evidence"] == [{"observed_at": "not supplied", "first_observed_at": "not supplied"}]
    assert result["source_items"][0]["checked_at"] is None
    assert "DATE_SECRET_CANARY" not in str(result)


@pytest.mark.parametrize("status", [None, [], {}, "PRIVATE_STATUS_CANARY"])
def test_invalid_source_status_never_escapes_or_crashes_export(status):
    state = state_fixture()
    state["sources"][0]["status"] = status
    result = boundary().build_public_payload(state, now=NOW)
    assert result["source_items"][0]["status"] == "unverified"
    assert result["tool_items"][0]["freshness"] == "stale"


@pytest.mark.parametrize("state", [[], None, {"items": None}, {"items": []}, {"items": [], "sources": "private"}])
def test_invalid_saved_state_shapes_are_rejected_as_generic_validation_errors(state):
    with pytest.raises(ValueError, match="state"):
        boundary().build_public_payload(state, now=NOW)


def test_saved_state_reader_enforces_byte_limit_before_parsing(tmp_path, monkeypatch):
    module = boundary()
    assert hasattr(module, "MAX_STATE_BYTES"), "saved-state byte bound is missing"
    monkeypatch.setattr(module, "MAX_STATE_BYTES", 32)
    state_path = tmp_path / "intel_state.json"
    state_path.write_bytes(b"PRIVATE_JSON_CANARY" * 10)
    with pytest.raises(ValueError, match="size"):
        module.read_saved_inputs(state_path, tmp_path / "missing.db")


def test_known_private_repository_is_not_publishable_even_if_forged_as_public_trending():
    state = state_fixture()
    state["items"] = [public_row(source="github-trending", id="github:example-org/private-repository",
                                 url="https://github.com/example-org/private-repository", headline="Private example repo")]
    assert boundary().build_public_payload(state, now=NOW)["tool_items"] == []


@pytest.fixture(scope="module")
def public_browser():
    import os
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH")
        # Verification is required: startup failure must never turn into a skip.
        instance = playwright.chromium.launch(headless=True, executable_path=executable)
        yield instance
        instance.close()


@pytest.mark.parametrize("width", [360, 390, 768, 1440])
def test_real_chromium_public_html_is_offline_safe_responsive_and_filters_work(public_browser, tmp_path, width):
    import json
    from playwright.sync_api import expect
    state = state_fixture()
    state["items"] = [public_row(headline='<script>window.pwned=1</script>'),
                      public_row(source="github-trending", id="github:org/repo", url="https://github.com/Org/Repo", headline="Public Repo", project_types=["agents"])]
    state["sources"].append({"name": "github-trending", "status": "ok", "checked_at": OBSERVED})
    state_path = tmp_path / "intel_state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")
    artifact = boundary().export_public_report(state_path, tmp_path / "missing.db", tmp_path / "public", now=NOW)
    context = public_browser.new_context(viewport={"width": width, "height": 900}, offline=True, color_scheme="dark")
    page = context.new_page()
    requests, errors, violations = [], [], []
    page.on("request", lambda request: requests.append(request.url))
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: violations.append(message.text) if message.type == "error" else None)
    try:
        page.goto(artifact.as_uri(), wait_until="load")
        assert page.locator('.discovery-item').count() == 2
        assert page.locator('.discovery-item:visible').count() == 0
        page.locator('#discovery-inventory > summary').click()
        assert page.locator('.discovery-item:visible').count() == 2
        # Native toggles update .open before the queued callback rewrites status.
        # Keep the exact expected value, but wait for that callback to complete.
        expect(page.locator('#discovery-result-count')).to_have_text("2 discoveries, no filter applied")
        assert page.evaluate("window.pwned || false") is False
        assert page.locator("[onerror], iframe, script[src], link[rel=stylesheet]").count() == 0
        assert page.locator('img').count() == 1
        assert page.locator('img').get_attribute('src').startswith('data:image/jpeg;base64,')
        assert "PRIVATE_" not in page.locator("body").inner_text()
        assert OBSERVED in page.locator("body").inner_text()
        assert page.locator("body").evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(5, 7, 10)"
        assert page.locator("h1").evaluate("el => getComputedStyle(el).color") == "rgb(111, 255, 233)"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_label("Search discoveries", exact=True).fill("public repo")
        assert page.locator(".discovery-item:visible").count() == 1
        page.get_by_label("Discovery source", exact=True).select_option("openrouter-models")
        assert page.locator(".discovery-item:visible").count() == 0
        assert page.locator("#discovery-empty").is_visible()
        page.get_by_label("Search discoveries", exact=True).fill("")
        assert page.locator(".discovery-item:visible").count() == 1
        page.get_by_label("Discovery source", exact=True).select_option("")
        assert page.locator(".discovery-item:visible").count() == 2
        page.locator("details.evidence summary").first.click()
        assert page.locator("details.evidence").first.get_attribute("open") is not None
        assert requests == [artifact.as_uri()]
        assert not errors and not violations
    finally:
        context.close()


def test_real_chromium_public_html_preserves_no_javascript_fallback(public_browser, tmp_path):
    import json
    state_path = tmp_path / "intel_state.json"
    state_path.write_text(json.dumps(state_fixture()), encoding="utf-8")
    artifact = boundary().export_public_report(state_path, tmp_path / "missing.db", tmp_path / "public", now=NOW)
    context = public_browser.new_context(java_script_enabled=False, offline=True)
    try:
        page = context.new_page()
        page.goto(artifact.as_uri(), wait_until="load")
        assert page.locator(".discovery-item:visible").count() == 0
        page.locator("#discovery-inventory > summary").click()
        assert page.locator(".discovery-item:visible").count() == 1
        assert page.get_by_text("Filters require JavaScript; every collected row is still reachable below, in the collapsible inventory archive.", exact=True).is_visible()
    finally:
        context.close()
