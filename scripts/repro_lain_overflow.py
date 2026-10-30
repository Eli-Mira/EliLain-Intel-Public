"""Offline reproduction: lain report horizontal overflow at 320px/200% zoom.

Renders the real lain template through the real HTMLReporter with
representative fixture data, then measures document and element overflow in
actual Chromium at narrow widths and 200% zoom. No network: the browser
context is offline and the artifact is loaded from file://.

Usage: python scripts/repro_lain_overflow.py [out_html] [width]
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src/tracker-core"))
sys.path.insert(0, str(ROOT / "src"))

from tracker_core.reporter_html import LainReporter  # noqa: E402

NOW = "2026-10-26"


def build_report(out_path: Path) -> Path:
    reporter = LainReporter()
    api_items = [
        {"name": "provider-a", "status": "LIVE", "description": "Free tier verified with documented limits.",
         "actual_status": 200, "freshness": "today", "checked_at": f"{NOW}T10:00:00+00:00",
         "probe_source": "catalog", "evidence": [{"attempted_models": "model-a", "response_category": "ok"}]},
        {"name": "provider-b", "status": "EXHAUSTED", "description": "Quota reported exhausted by upstream.",
         "actual_status": 429, "freshness": "today", "checked_at": f"{NOW}T10:00:00+00:00",
         "probe_source": "probe", "evidence": []},
    ]
    news_items = [
        {"headline": "A fresh model release with a long descriptive headline for layout stress",
         "source": "openrouter", "summary": "Documented release with verified zero pricing and a REST API.",
         "url": "https://openrouter.ai/example-model"},
        {"headline": "Second story with a very long unbroken token supercalifragilisticexpialidocious",
         "source": "github", "summary": "Trending repository description that stresses word wrapping.",
         "url": "https://github.com/example/repo"},
    ]
    tool_items = [
        {"headline": "skill-cli", "source": "skills.sh", "summary": "A trending CLI skill with real description.",
         "url": "https://skills.sh/owner/repo/skill", "id": "skills:owner/repo/skill",
         "project_types": ["skills"], "freshness": "today", "change_type": "new"},
    ]
    change_items = [{"headline": "provider-a", "source": "probe", "summary": "Status changed UNKNOWN->LIVE",
                     "url": "", "change_type": "status"}]
    action_items = [{"title": "Rotate token", "priority": "high", "reason": "hygiene",
                     "action": "Owner action", "url": ""}]
    source_items = [{"name": "openrouter-models", "status": "ok", "checked_at": f"{NOW}T10:00:00+00:00",
                     "item_count": 10, "error": None, "url": "https://openrouter.ai/api/v1/models",
                     "coverage": "10/10"}]
    news_health_items = [{"source": "openrouter", "status": "ok", "consecutive_failures": 0,
                          "last_success": f"{NOW}T10:00:00+00:00"}]
    recommendation_groups = {
        "shortlist": [{"name": "example-project", "why": "Verified license and API evidence.",
                       "url": "https://github.com/example/project", "score": 82}],
        "deferred": [], "excluded": [],
    }
    recommendation_coverage = {"shortlist": 1, "deferred": 0, "excluded": 0}
    html = reporter.render(
        api_items=api_items, news_items=news_items, tool_items=tool_items,
        report_date=NOW, generated_at="10:00 UTC",
        change_items=change_items, baseline=False, action_items=action_items,
        source_items=source_items, news_health_items=news_health_items,
        runtime_source="local-offline-fixture",
        recommendation_groups=recommendation_groups,
        recommendation_coverage=recommendation_coverage,
        public_export=False,
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    return out_path


async def measure(artifact: Path, width: int) -> dict:
    from playwright.async_api import async_playwright

    import os
    executable = os.environ.get("PLAYWRIGHT_EXECUTABLE_PATH") or None
    offenders = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=executable)
        for zoom in (100, 200):
            context = await browser.new_context(
                viewport={"width": width, "height": 844}, offline=True)
            page = await context.new_page()
            await page.goto(artifact.as_uri())
            await page.evaluate("z => document.body.style.zoom = z + '%'", zoom)
            doc = await page.evaluate(
                "() => ({scrollWidth: document.documentElement.scrollWidth,"
                " innerWidth: window.innerWidth})")
            els = await page.evaluate("""() => Array.from(document.querySelectorAll('body *')).map(el => {
                const r = el.getBoundingClientRect(), s = getComputedStyle(el);
                return {tag: el.tagName, id: el.id, cls: String(el.className).slice(0,60),
                        right: Math.round(r.right), width: Math.round(r.width),
                        scrollWidth: el.scrollWidth, clientWidth: el.clientWidth,
                        gridColumns: s.gridTemplateColumns, text: (el.textContent||'').slice(0,40)};
            }).filter(r => r.right > r._iw || r.scrollWidth > r.clientWidth + 1)""".replace("r._iw", "(document.documentElement.clientWidth + 0.5)"))
            offenders[zoom] = {"doc": doc, "offenders": els[:12]}
            await context.close()
        await browser.close()
    return offenders


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "public-test-run" / "lain-repro.html"
    width = int(sys.argv[2]) if len(sys.argv) > 2 else 320
    artifact = build_report(out)
    print(f"artifact: {artifact} ({artifact.stat().st_size} bytes)")
    result = asyncio.run(measure(artifact, width))
    for zoom, data in result.items():
        doc = data["doc"]
        status = "OVERFLOW" if doc["scrollWidth"] > doc["innerWidth"] else "ok"
        print(f"\n=== {width}px @ {zoom}%: doc scrollWidth={doc['scrollWidth']} innerWidth={doc['innerWidth']} -> {status}")
        for o in data["offenders"]:
            print(f"  <{o['tag']} id={o['id']!r} class={o['cls']!r}> right={o['right']} w={o['width']} sw={o['scrollWidth']} cw={o['clientWidth']} grid={o['gridColumns'][:40]!r} text={o['text']!r}")
