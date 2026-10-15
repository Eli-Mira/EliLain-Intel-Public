"""Project report UI and fail-closed public evaluation contract."""
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup

from intel_sources.public_report import PUBLIC_SOURCES
from intel_sources.recommendations import evaluate_projects
from tracker_core.reporter_html import LainReporter

from verification_fixtures import load_verification_inventory

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
PUBLIC_DISCOVERY_SOURCES = tuple(PUBLIC_SOURCES)
CHECKED = "2026-10-01T11:00:00+00:00"
TDD = "skills:obra/superpowers/test-driven-development"


def discovery(identifier=TDD):
    kind, slug = identifier.split(":", 1)
    return {"id": identifier, "headline": slug, "source": "skills-sh" if kind == "skills" else "github-trending",
            "url": ("https://skills.sh/" if kind == "skills" else "https://github.com/") + slug,
            "checked_at": CHECKED, "evidence": {}, "summary": "Original inventory retained"}


def test_lain_render_accepts_evaluated_groups_and_preserves_inventory(tmp_path):
    items = [discovery(), discovery("github:pytorch/pytorch")]
    result = evaluate_projects(items, now=NOW)
    html = LainReporter().render(api_items=[], tool_items=items,
        recommendation_groups=result["groups"], recommendation_coverage={
            **result["coverage"], "inventory_count": result["inventory_count"], "reviewed_count": 2})
    soup = BeautifulSoup(html, "html.parser")
    assert len(soup.select("#discoveries .discovery-item")) == 2
    assert len(soup.select("#project-shortlists .project-group")) == 3
    assert "2 reviewed / 2 discovery candidates" in soup.select_one("#recommendation-coverage").text
    card = soup.select_one(".recommendation-item[data-id='" + TDD + "']")
    for expected in ("Reason:", "Constraints:", "Confidence: low", "Discovery: fresh", "Official metadata: unverified",
                     "Cost: unknown", "License: unknown", "Support: unknown", "Next validation:", "metadata-only", "not integration-tested"):
        assert expected in card.text
    assert "Decimal totals" in soup.select_one(".project-group[data-project='desktop-app']").text
    assert "No installed integration tests" in soup.select_one("#project-shortlists").text
    saved = LainReporter().save(tmp_path / "report.html", api_items=[], tool_items=items,
        recommendation_groups=result["groups"], recommendation_coverage={"reviewed_count": 2, "discovery_count": 2})
    assert "Decimal totals" in saved.read_text(encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    assert (root / "src/templates/report_template_lain.html").read_bytes() == (root / "src/tracker-core/templates/report_template_lain.html").read_bytes()


def test_public_inventory_has_no_personal_review_and_keeps_cap_disclosure():
    import copy
    from intel_sources.public_report import build_public_payload
    first = discovery("skills:aaa/public/unreviewed")
    reviewed = discovery()
    reviewed.update(summary="PRIVATE_DESCRIPTION_CANARY", constraints=["PRIVATE_CONSTRAINT_CANARY"],
                    source_urls=["http://localhost/PRIVATE_URL_CANARY"],
                    evidence={"description": "PRIVATE_EVIDENCE_CANARY", "error": "PRIVATE_ERROR_CANARY"})
    private = discovery("github:example-org/private-repository")
    state = {"items": [first, reviewed, private], "sources": [{"name": "skills-sh", "status": "ok", "checked_at": CHECKED}]}
    before = copy.deepcopy(state)
    payload = build_public_payload(state, now=NOW, max_items_per_source=1)
    assert [item["id"] for item in payload["tool_items"]] == [first["id"]]
    assert "recommendation_groups" not in payload
    assert "recommendation_coverage" not in payload
    assert payload['inventory_total'] == 2
    assert "PRIVATE_" not in str(payload) and "localhost" not in str(payload)
    assert "example-org/private-repository" not in str(payload)
    assert 'omitted by caller cap: 1' in payload['source_items'][0]['coverage']
    assert state == before
    soup = BeautifulSoup(LainReporter().render(**payload, public_export=True), "html.parser")
    assert soup.select_one('#project-shortlists') is None
    assert 'Decimal totals' not in soup.text
    assert len(soup.select("#discoveries .discovery-item")) == 1
    for anchor in soup.select("#discoveries a[href]"):
        assert anchor["href"].startswith("https://skills.sh/") or anchor["href"].startswith("https://github.com/")


def test_real_saved_inventory_renders_advisory_shortlists_in_internal_and_public_chromium(tmp_path):
    import json
    import os
    from collections import Counter
    from playwright.sync_api import sync_playwright
    from intel_sources.public_report import build_public_payload
    from intel_sources.recommendations import evaluate_projects
    root = Path(__file__).resolve().parents[1]
    state = json.loads(load_verification_inventory(root).read_text(encoding="utf-8"))
    evaluated = evaluate_projects(state["items"], now=NOW)
    groups = evaluated["groups"]
    coverage = {**evaluated["coverage"], "inventory_count": evaluated["inventory_count"],
        "reviewed_count": len({entry["id"] for group in groups.values()
                               for category in ("shortlist", "deferred", "excluded")
                               for entry in group[category]})}
    internal = tmp_path / "internal" / "report.html"
    LainReporter().save(internal, api_items=[], tool_items=state["items"], report_date="2026-10-01",
        generated_at="12:00 UTC", runtime_source="Local verification render from saved inventory",
        recommendation_groups=groups, recommendation_coverage=coverage)
    payload = build_public_payload(state, now=NOW, max_items_per_source=25)
    public = tmp_path / "public" / "index.html"
    public.parent.mkdir()
    public.write_text(LainReporter().render(**payload, public_export=True), encoding="utf-8")
    shortlists = {project: len(group["shortlist"]) for project, group in groups.items()}
    assert sum(shortlists.values()) > 0, "real inventory produced no reviewed shortlist candidates"
    assert "arcade-game" not in shortlists, "paused project leaked into active recommendations"
    assert shortlists["desktop-app"] > 0, "Desktop application shortlist is empty from real saved data"
    # Scale-relative: this asserts coverage is disclosed and that the export cap
    # is real, not that the inventory happens to exceed some live count.
    assert coverage["reviewed_count"] > 0
    assert 0 < coverage["discovery_count"] <= len(state["items"])
    assert 'recommendation_groups' not in payload
    assert len(payload['tool_items']) <= 25 * len(PUBLIC_DISCOVERY_SOURCES)
    assert len(payload['tool_items']) <= payload['inventory_total']
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH"))
        try:
            for path, exported, expected, collapsed in (
                    (internal, len(state["items"]), coverage["reviewed_count"], False),
                    (public, len(payload["tool_items"]), 0, True)):
                context = browser.new_context(viewport={"width": 1440, "height": 900}, offline=True)
                page = context.new_page()
                requests, errors = [], []
                page.on("request", lambda request: requests.append(request.url))
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(path.as_uri(), wait_until="load")
                rows = page.locator("#discoveries .discovery-item")
                assert rows.count() == exported, "a row was dropped from the report"
                if collapsed:
                    # Public consumer: Explore first, no personalized decision view.
                    assert page.locator("#discoveries .discovery-item:visible").count() == 0
                    assert page.locator('#project-shortlists').count() == 0
                    assert page.locator('#discoveries').evaluate('(x)=>x.getBoundingClientRect().top') < 300
                    page.locator("#discovery-inventory > summary").click()
                assert page.locator("#discoveries .discovery-item:visible").count() == exported
                if collapsed:
                    assert page.locator('#project-shortlists').count() == 0
                    assert 'Desktop application' not in page.locator('body').inner_text()
                else:
                    assert page.locator("#project-shortlists .project-group").count() == 3
                    assert page.locator("#recommendation-coverage").text_content().startswith(f"{expected} reviewed / ")
                    cards = page.locator("#project-shortlists .recommendation-item")
                    assert cards.count() > 0
                    assert cards.first.get_attribute("data-id")
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                assert requests == [path.as_uri()] and not errors
                if exported != len(state["items"]):
                    assert "PRIVATE" not in page.locator("body").inner_text()
                context.close()
        finally:
            browser.close()


