"""V3 internal RC demonstration: offline replay producing visible digests.

Generates, entirely offline from committed fixtures (no network, no live
scan, no delivery):

- demo-output/busy/index.html    — a source-rich day: 4 approved portable
  source families, corroboration across sources, novelty vs an empty
  baseline, traceable exclusions.
- demo-output/sparse/index.html  — a legitimately quiet day: every candidate
  stale or unverified; the report honestly selects nothing.
- demo-output/metrics.json       — reproducible M6/M9 metrics: per-source
  contribution, source diversity, exclusion causes, duplicate suppression,
  novelty counts.

Reproduce with:
    PLAYWRIGHT not required.  python scripts/build_v3_demo.py
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "src/tracker-core"))

from intel_sources.daily_digest import build_digest
from intel_sources.evidence import group_findings
from intel_sources.settings import default_settings

NOW = datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc)
OUT = ROOT / "demo-output"


def cand(cid, source_id, url, category="models", published=None, verified=True,
         why=None, what=None):
    return {
        "id": cid, "name": cid.split(":", 1)[-1],
        "what": what or "A documented release with real capabilities and evidence links.",
        "category": category, "source_id": source_id, "source_url": url,
        "published_at": published or (NOW - timedelta(days=1)).isoformat(),
        "date_kind": "catalog-created" if source_id == "openrouter-models" else
                     ("trending" if source_id in ("github-trending", "skills-hot") else "release"),
        "observed_at": NOW.isoformat(),
        "free": {"status": "free-tier" if source_id in ("openrouter-models", "unorouter-pricing")
                 else "unknown",
                 "detail": "Verified zero pricing from the source catalog."
                 if source_id in ("openrouter-models", "unorouter-pricing") else "",
                 "evidence_url": url},
        "api": {"status": "available" if source_id in ("openrouter-models", "unorouter-pricing")
                else "unknown", "detail": "REST API" if verified else "", "evidence_url": url},
        "topics": [category, "free-api"] if source_id in ("openrouter-models", "unorouter-pricing")
                  else [category],
        "why": why or "Documented release with verifiable upstream evidence links.",
        "link_verification": {"status": "verified" if verified else "unverified",
                              "checked_at": NOW.isoformat(), "url": url,
                              "http_status": 200 if verified else 404},
    }


def busy_day_candidates():
    """Source-rich day mirroring real recorded shapes: corroboration across
    sources for one project, plus independent fresh findings."""
    fresh = (NOW - timedelta(days=1)).isoformat()
    return [
        # one project corroborated by two sources (release + trending listing)
        cand("github-release:ollama/ollama@v9", "github-trending",
             "https://github.com/ollama/ollama", category="devtools", published=fresh,
             why="Listed on GitHub's daily trending page: local inference runtime release."),
        cand("replace-so:ollama/ollama", "github-trending",
             "https://github.com/ollama/ollama", category="devtools", published=fresh,
             why="Second independent listing of the same upstream repository."),
        # independent fresh findings per family
        cand("openrouter:org/free-model-a", "openrouter-models",
             "https://openrouter.ai/org/free-model-a", published=fresh),
        cand("openrouter:org/free-model-b", "openrouter-models",
             "https://openrouter.ai/org/free-model-b", published=fresh),
        cand("openrouter:org/free-model-c", "openrouter-models",
             "https://openrouter.ai/org/free-model-c", published=fresh),
        cand("skills:owner/repo/skill", "skills-hot",
             "https://skills.sh/owner/repo/skill", category="skills", published=fresh),
        cand("unorouter:free-model:free", "unorouter-pricing",
             "https://api.unorouter.com/api/pricing/catalog", published=fresh),
        # negatives: stale, dead link, duplicate
        cand("openrouter:stale-model", "openrouter-models",
             "https://openrouter.ai/stale-model",
             published="2026-08-01T00:00:00+00:00"),
        cand("openrouter:dead-model", "openrouter-models",
             "https://openrouter.ai/dead-model", verified=False),
        cand("openrouter:free-model-a-dup", "openrouter-models",
             "https://openrouter.ai/org/free-model-a", published=fresh),
    ]


def sparse_day_candidates():
    """Legitimately quiet day: stale rows and one dead link; nothing qualifies."""
    return [
        cand("openrouter:old-model-1", "openrouter-models",
             "https://openrouter.ai/old-model-1", published="2026-09-20T00:00:00+00:00"),
        cand("openrouter:old-model-2", "openrouter-models",
             "https://openrouter.ai/old-model-2", published="2026-09-25T00:00:00+00:00"),
        cand("unorouter:dead-row:free", "unorouter-pricing",
             "https://unorouter.com/removed/row", verified=False),
    ]


def run_case(name, candidates, baseline):
    cfg = default_settings()
    collection = {"candidates": candidates, "sources": []}
    digest = build_digest(collection, cfg, NOW, include_rejections=True)
    rejections = digest.pop("rejections", [])
    findings = group_findings(candidates)

    from intel_sources.daily_public import export_digest
    export_dir = OUT / name
    export_digest(digest, export_dir)

    by_source = {}
    for c in candidates:
        by_source[c["source_id"]] = by_source.get(c["source_id"], 0) + 1
    selected_sources = sorted({i["source_id"] for i in digest["items"]})
    reasons = {}
    for r in rejections:
        reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
    corroborated = sum(1 for f in findings if f["corroborations"])
    metrics = {
        "case": name,
        "fetched": digest["counts"]["fetched"],
        "selected": digest["counts"]["selected"],
        "per_source_fetched": by_source,
        "source_diversity_selected": len(selected_sources),
        "selected_source_ids": selected_sources,
        "duplicate_suppressed": reasons.get("duplicate source url", 0),
        "exclusion_causes": reasons,
        "corroborated_findings": corroborated,
        "novelty_first_seen": sum(1 for f in group_findings(candidates)
                                  if f["entity_key"] not in baseline),
        "scores": {i["id"]: i["score"] for i in digest["items"]},
    }
    return metrics


def main():
    OUT.mkdir(exist_ok=True)
    empty_baseline: set = set()
    prior = set()
    metrics = []
    metrics.append(run_case("busy", busy_day_candidates(), empty_baseline))
    # prior baseline = busy day entities, so a repeat run shows novelty working
    prior = {f["entity_key"] for f in group_findings(busy_day_candidates())}
    metrics.append(run_case("sparse", sparse_day_candidates(), prior))
    (OUT / "metrics.json").write_text(
        json.dumps(metrics, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(metrics, indent=2, sort_keys=True))
    print(f"\ndemo artifacts written under {OUT}")


if __name__ == "__main__":
    main()
