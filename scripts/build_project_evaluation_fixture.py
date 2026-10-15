"""Build the committed project-shortlist fixture from the saved inventory fixture.

Recomputes the evaluation rather than copying the live artifact, so the replay
test's `evaluate(snapshot) == result` guarantee holds by construction.
"""

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intel_sources.recommendations import evaluate_projects  # noqa: E402

INVENTORY = ROOT / "tests" / "fixtures" / "intel_state_sample.json"
OUT = ROOT / "tests" / "fixtures" / "project_evaluations_sample.json"


def main():
    if not INVENTORY.exists():
        print("run scripts/build_test_fixture.py first", file=sys.stderr)
        return 1
    raw = INVENTORY.read_bytes()
    state = json.loads(raw.decode("utf-8"))
    snapshot = state["items"]
    now = datetime.now(timezone.utc).replace(microsecond=0)
    result = evaluate_projects(snapshot, now=now)

    artifact = {
        "fixture_of": "data/project_evaluations.json",
        "result": result,
        "discovery_snapshot": snapshot,
        "provenance": {
            "source_inventory_count": state.get("source_item_count", len(snapshot)),
            "input_state_sha256": hashlib.sha256(raw).hexdigest(),
            "generated_by": "scripts/build_project_evaluation_fixture.py",
            "note": "Bounded sanitized sample; regenerate with the fixture builder.",
        },
    }
    OUT.write_text(json.dumps(artifact, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    body = OUT.read_text(encoding="utf-8")
    for probe in ("C:/", "C:\\\\", "localhost", "sk-", "ghp_", "traceback",
                  '"description"', '"error": "'):
        if probe in body:
            print(f"fixture rejected: contains {probe!r}", file=sys.stderr)
            return 1
    # The evaluator emits `"summary": null` as an explicit "not evaluated" marker.
    # A null is fine; fetched third-party text is not.
    artifact_data = json.loads(body)
    for item in artifact_data["discovery_snapshot"]:
        if item.get("summary"):
            print(f"fixture rejected: {item.get('id')} carries summary text", file=sys.stderr)
            return 1
    for group in artifact_data["result"]["groups"].values():
        for category in ("shortlist", "deferred", "excluded"):
            for entry in group[category]:
                if entry.get("summary"):
                    print(f"fixture rejected: {entry.get('id')} carries summary text", file=sys.stderr)
                    return 1

    groups = {name: {category: len(group[category]) for category in
                     ("shortlist", "deferred", "excluded")}
              for name, group in result["groups"].items()}
    print(json.dumps({"path": str(OUT.relative_to(ROOT)), "bytes": OUT.stat().st_size,
                      "snapshot": len(snapshot), "groups": groups}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())