"""Is the delivered report actually USEFUL, or just structurally valid?

Structural checks already pass everywhere. This asks content questions a human
would ask: are the shortlists relevant to the four example projects, is the discovery
data current, are the provider verdicts actionable, and is anything stale,
empty, or placeholder-flavoured leaking through.
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
state = json.loads((ROOT / "data" / "intel_state.json").read_text(encoding="utf-8"))
evaluations = json.loads((ROOT / "data" / "project_evaluations.json").read_text(encoding="utf-8"))
providers = json.loads((ROOT / "data" / "provider_state.json").read_text(encoding="utf-8"))
now = datetime.now(timezone.utc)

print("=" * 70)
print("1. DISCOVERY CURRENCY")
print("=" * 70)
stamps = [i.get("last_seen") or i.get("first_seen") for i in state["items"]]
stamps = [s for s in stamps if s]
ages = [(now - datetime.fromisoformat(s)).total_seconds() / 3600 for s in stamps]
print(f"items={len(state['items'])} newest={min(ages):.1f}h oldest={max(ages):.1f}h ago")
fresh = sum(1 for a in ages if a < 48)
print(f"updated within 48h: {fresh}/{len(ages)} ({100*fresh//len(ages)}%)")

print()
print("=" * 70)
print("2. PROJECT SHORTLIST RELEVANCE (do the shortlists match the example project domains?)")
print("=" * 70)
# Example stack keywords, per tracked project domain.
RELEVANT = {
    "desktop-app": ("restaurant", "pos", "point of sale", "inventory", "menu", "billing",
                    "receipt", "payment", "orders", "kitchen", "hospitality"),
    "vision-app": ("gesture", "hand tracking", "mediapipe", "opencv", "handpose",
                   "landmark", "webcam", "camera", "vision", "tracking", "motion"),
    "arcade-game": ("pygame", "game", "sprite", "arcade", "collision", "canvas",
                    "animation", "score", "player", "2d"),
    "data-ai": ("llm", "rag", "embedding", "agent", "pipeline", "sql", "pandas",
                "transformer", "fine-tun", "vector", "etl", "ml", "inference"),
}
groups = evaluations["result"]["groups"]
for project, group in groups.items():
    words = RELEVANT[project]
    hits = 0
    print(f"\n--- {project} (shortlist {len(group['shortlist'])}) ---")
    for entry in group["shortlist"]:
        blob = f"{entry.get('headline','')} {entry.get('reason','')}".lower()
        matched = [w for w in words if w in blob]
        hits += bool(matched)
        print(f"  [{'HIT ' if matched else 'weak'}] {entry['id'][:62]}")
        print(f"         {entry.get('reason','')[:96]}")
    if group["shortlist"]:
        print(f"  relevance: {hits}/{len(group['shortlist'])} match the example stack")

print()
print("=" * 70)
print("3. PROVIDER VERDICTS — actionable or noise?")
print("=" * 70)
rows = providers if isinstance(providers, list) else providers.get("providers", [])
verdicts = Counter(r.get("status") or r.get("verdict") for r in rows if isinstance(r, dict))
print("verdict spread:", dict(verdicts))
no_action = [r for r in rows if isinstance(r, dict) and not (r.get("action") or r.get("next_step"))]
print(f"providers with NO actionable next step: {len(no_action)}/{len(rows)}")
unknown = [r for r in rows if isinstance(r, dict) and (r.get("status") == "unknown")]
print(f"UNKNOWN (never probed): {len(unknown)}")

print()
print("=" * 70)
print("4. WHAT THE PUBLIC LINK ACTUALLY SHOWS")
print("=" * 70)
public = ROOT / "public" / "index.html"
if public.exists():
    body = public.read_text(encoding="utf-8")
    print(f"bytes={len(body):,}")
    for probe, label in (("recommendation-item", "shortlist cards"),
                         ("discovery-item", "discovery rows"),
                         ("api-item", "provider rows"),
                         ("news-item", "news rows")):
        print(f"  {label}: {body.count(probe)}")
    # Placeholder / filler leakage.
    for bad, label in (("TODO", "TODO"), ("FIXME", "FIXME"), ("undefined", "undefined"),
                       ("[object Object]", "[object Object]"), ("None", "literal None")):
        hits = len(re.findall(rf"\b{bad}\b", body))
        if hits:
            print(f"  !! {label} appears {hits}x")
    empty = body.count("No evaluated project shortlists")
    print(f"  empty-state placeholders: {empty}")
else:
    print("public/index.html not generated locally")