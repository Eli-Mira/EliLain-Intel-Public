"""Provider probe reality check.

Provider-attempt counters are not confirmed delivery. This script reports the
observed status counts without assuming a configured provider set.
The provider service runs locally; CI may leave probe results unknown.
"""

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
state = json.loads((ROOT / "data" / "provider_state.json").read_text(encoding="utf-8"))
raw = state["providers"]
rows = list(raw.values()) if isinstance(raw, dict) else raw
now = datetime.now(timezone.utc)

print(f"providers: {len(rows)}")
print(f"last_updated: {state.get('last_updated')}")
print()
by_status = Counter(r.get("status") for r in rows)
for status, count in by_status.most_common():
    print(f"  {status:10} {count}")

print()
print("=== the 6 live ones — do they carry real evidence? ===")
for row in rows:
    if row.get("status") != "live":
        continue
    evidence = row.get("probe_evidence") or {}
    checked = row.get("checked_at")
    age = (now - datetime.fromisoformat(checked)).total_seconds() / 3600 if checked else None
    print(f"\n{row.get('name')}")
    print(f"  status={row.get('status')} reason={row.get('status_reason')}")
    print(f"  checked_at={checked}" + (f" ({age:.1f}h ago)" if age else " (NEVER)"))
    print(f"  probe_source={row.get('probe_source')} model={row.get('probe_model')}")
    print(f"  evidence keys={sorted(map(str, evidence))[:8]}")
    if not evidence:
        print("  !! live with NO probe evidence")

print()
print("=== unknown rows: is the reason stated? ===")
silent = 0
for row in rows:
    if row.get("status") != "unknown":
        continue
    reason = row.get("status_reason")
    if not reason:
        silent += 1
    print(f"  {row.get('name'):22} reason={reason!r}")
print()
print(f"unknown WITHOUT a stated reason: {silent}")

print()
print("=== actionability ===")
no_action = [r for r in rows if not (r.get("freellmapi_action") or "").strip()]
print(f"providers with no freellmapi_action: {len(no_action)}/{len(rows)}")