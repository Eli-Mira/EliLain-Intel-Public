"""Shared test helpers: prefer real saved state, fall back to committed fixtures.

The verification tests must exercise the real renderer in CI, where `data/` is
never present. These helpers pick real saved state when it exists (so local runs
verify live data) and a sanitized committed fixture otherwise (so CI verifies
the same code path on a bounded public-safe inventory).
"""

import json
from pathlib import Path

FIXTURES = "tests/fixtures"


def load_verification_inventory(root: Path) -> Path:
    """Path to a discovery state file usable for render verification."""
    real = root / "data" / "intel_state.json"
    if real.exists():
        return real
    fixture = root / FIXTURES / "intel_state_sample.json"
    if not fixture.exists():
        raise AssertionError(
            "No data/intel_state.json and no committed fixture; run "
            "scripts/build_test_fixture.py")
    return fixture


def load_inventory(root: Path) -> dict:
    return json.loads(load_verification_inventory(root).read_text(encoding="utf-8"))