# INTEL V3 evidence index

| When | Evidence | Commit/Path |
|---|---|---|
| 2026-10-26 (V2 session) | Real-evidence replay 24->5->5 recorded-time vs 0 today; 3 mechanisms | docs/session-20261026-daily-selection-and-privacy.md |
| 2026-10-26 (V2 session) | Full suite 1493P/6S/0F exit 0 at b5aae30 (Windows, venv python) | git b5aae30 |
| 2026-10-26 (V2 session) | 320px/200% browser matrix 9/9 (Chromium 1248, offline) | tests/test_daily_producer_pipeline.py |
| V3 M3 | 11 tests: license states, GET-evidence reuse, exclusion codes | 970805a |
| V3 M4 | 10 tests: registry schema/fail-closed enablement (18 rows, 8 families) | 4da263b |
| V3 M5+M6 | 17 tests: feed/release/reference adapters + evidence grouping/novelty | 9f31d48 |
| V3 M7+M8 | 6 tests: run-level baseline persistence + security negatives | 1c2a8c2 |
| V3 M9 | Full suite at 1c2a8c2: 1537P/6S/0F exit 0 | 1c2a8c2 |
| V3 final | Full suite on true final bytes (incl. HF adapter): 1539P/6S/0F, exit 0, 215s (Windows, venv python, Chromium 1248 via PLAYWRIGHT_EXECUTABLE_PATH) | git HEAD |
| V3 M10 | Offline demo replay: busy-day (4 selected, corroborated, traceable exclusions) + sparse-day (0 selected, honest empty state) + metrics.json; 51/51 src modules byte-compile + import clean | scripts/build_v3_demo.py, demo-output/ |
