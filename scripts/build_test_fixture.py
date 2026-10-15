"""Build a committed, sanitized fixture from real saved tracker state.

The verification tests must run in CI, where data/ is never present. This
reduces the live discovery inventory to a bounded, metadata-only fixture:
no summaries, no evidence, no paths, no credentials. Discovery ids and public
URLs are kept because the identifier/URL agreement checks are the contract
under test.
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intel_sources.recommendations import evaluate_projects  # noqa: E402

MAX_ITEMS = 90
PROJECTS = ("desktop-app", "vision-app", "data-ai")


def slim(item):
    """Keep only the identity + provenance fields the checks need.

    `fingerprint_data` is deliberately reduced to counters and structural
    markers: it can carry fetched third-party summary text, which must never
    reach a committed file.
    """
    fingerprint = item.get("fingerprint_data") or {}
    if not isinstance(fingerprint, dict):
        fingerprint = {}
    safe_fingerprint = {key: value for key, value in fingerprint.items()
                        if isinstance(value, (int, float, bool)) or value is None}
    return {
        "id": item["id"],
        "headline": str(item.get("headline", ""))[:120],
        "source": item.get("source"),
        "url": item.get("url"),
        "project_types": item.get("project_types", []),
        "first_seen": item.get("first_seen"),
        "last_seen": item.get("last_seen"),
        "fingerprint_data": safe_fingerprint,
    }


def main():
    state_path = ROOT / "data" / "intel_state.json"
    if not state_path.exists():
        print("no saved state; nothing to build", file=sys.stderr)
        return 1
    raw = state_path.read_bytes()
    state = json.loads(raw.decode("utf-8"))
    # Local-provider rows carry loopback URLs; they are already excluded from the
    # public projection. Drop them so the committed fixture is public-safe by
    # construction rather than by a later redaction pass.
    items = [slim(item) for item in state["items"]
             if str(item.get("url", "")).startswith("https://")]

    # Bound the fixture: keep every item that any project profile scores well,
    # then fill up to MAX_ITEMS with a stable spread across both sources.
    evaluated = evaluate_projects(items)
    keep, seen_sources = set(), {}
    for group in evaluated["groups"].values():
        for category in ("shortlist", "deferred"):
            for entry in group[category]:
                keep.add(entry["id"])
    for item in items:
        seen_sources.setdefault(item["source"], []).append(item["id"])
    remainder = [item["id"] for item in items
                 if item["id"] not in keep and item["source"] in seen_sources]
    per_source = max(1, (MAX_ITEMS - len(keep)) // max(1, len(seen_sources)))
    for source, ids in sorted(seen_sources.items()):
        keep.update(ids[:per_source])

    fixture_items = [item for item in items if item["id"] in keep]
    fixture_items.sort(key=lambda item: item["id"])
    if not fixture_items:
        print("fixture selection produced no items", file=sys.stderr)
        return 1

    # Source health rows describe local endpoints; the fixture only needs the
    # discovery sources it actually carries items for. Saved state stores them
    # as a list of rows, keyed by name.
    source_rows = state.get("sources") or []
    if isinstance(source_rows, dict):
        source_rows = [dict(row, name=name) for name, row in source_rows.items()]
    status_by_source = {row.get("name"): row for row in source_rows if isinstance(row, dict)}
    kept_sources = sorted({item["source"] for item in fixture_items})
    fixture = {
        "fixture_of": "data/intel_state.json",
        "source_state_sha256": hashlib.sha256(raw).hexdigest(),
        "source_item_count": len(state["items"]),
        "item_count": len(fixture_items),
        "items": fixture_items,
        "sources": [{"name": name,
                     "status": (status_by_source.get(name) or {}).get("status"),
                     "checked_at": (status_by_source.get(name) or {}).get("checked_at"),
                     "item_count": (status_by_source.get(name) or {}).get("item_count")}
                    for name in kept_sources],
        "baseline": {},
    }
    out = ROOT / "tests" / "fixtures" / "intel_state_sample.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # Never let raw text or credentials reach a committed fixture. Key names are
    # fine (a null `error` is meaningful); values are what must be absent.
    body = out.read_text(encoding="utf-8")
    for probe in ("C:/", "C:\\\\", "localhost", "sk-", "ghp_", "github_pat_",
                  "traceback", '"summary"', '"description"', '"evidence"'):
        if probe in body:
            print(f"fixture rejected: contains {probe!r}", file=sys.stderr)
            return 1
    for item in fixture_items:
        if item["fingerprint_data"].get("error"):
            print(f"fixture rejected: {item['id']} carries error text", file=sys.stderr)
            return 1

    print(json.dumps({"path": str(out.relative_to(ROOT)), "items": len(fixture_items),
                      "bytes": out.stat().st_size,
                      "sources": {s: len(v) for s, v in sorted(seen_sources.items())}},
                     sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())