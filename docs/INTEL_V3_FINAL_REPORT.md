# INTEL V3 final report — internal source-expansion release candidate

Status: **INTERNAL_RC (local only)** — not published, not enabled live, no notifications.

## Acceptance matrix (directive section L)

| Gate | State | Evidence |
|---|---|---|
| Development authority identified; changes on authorized branch | PASS | worktree news-tracker-v2fix, branch fix/daily-selection-320px, commits 970805a..(final) on top of b5aae30; nothing pushed |
| Verification/metadata defects resolved; threshold policy untouched | PASS | GET fallback + same-run GET evidence + license evidence states; min_score still 60 (tests assert) |
| Deterministic root-cause diagnostics, no fabricated output | PASS | rejections + findings + novelty in private raw_evidence; frozen-now tests |
| Registry ≥12 candidates across ≥4 families, states transparent | PASS | 18 rows, 8 families (10 registry tests) |
| Offline adapters cover official/news, tools/skills, research, editorial w/o breaking existing | PASS | expanded_sources (feeds/releases/reference); 4 portable defaults unchanged (full suite green) |
| Replace.so accurate; Skills.sh and GitHub upstream skills separately modeled | PASS | manual_reference connector (zero network); skills.sh hot page PENDING_PERMISSION; github-releases adapter separate |
| Cross-source duplicate/novelty tests pass; explainable scores | PASS | 8 evidence tests; score components unchanged and visible |
| Report distinguishes sections; useful quiet states | PASS | existing sections/filters/empty states verified on current bytes; run-level novelty counts exposed |
| Guarded HTTP + malicious feed/skill/URL negatives pass | PASS | M8 negatives + existing security suites |
| No scans/alerts/publication/paid calls/policy flips | PASS | all tests offline/mocked; no outbound runs |
| Test claims with commands/exit/counts | PASS | full suite 1539P/6S/0F exit 0 on final bytes (Windows, venv python) |
| Local commits + durable checkpoints | PASS | 5 V3 commits; checkpoint in memory + board docs |
| RC docs with blockers + owner decisions | PASS | INTEL_V3_BLOCKERS.md + this file |

## Remaining owner gates (unchanged)

Per-source live activation (robots/ToS/rate), scoring calibration for 51/49 records, min_score policy, live scans, notifications, publication, token rotation, history remediation, Phase 0 cockpit.

## Honest limitations

- New adapters are OFFLINE_FIXTURE_ONLY: no live endpoint has been fetched by this project's V3 code paths; registry rows carry exact verification provenance. Live activation is a per-source owner gate.
- Visible demonstration: `python scripts/build_v3_demo.py` reproduces demo-output/ (busy + sparse day with metrics) fully offline.
- Clean-install proxy: all 51 src modules byte-compile and import cleanly (no privileged install performed).
- Independent SPEC/QUALITY/SECURITY review (directive M9.6): REVIEW_BLOCKED — no authorized independent reviewer was available in this session; the parent performed its own security pass (M8 negatives, privacy plant-test, guarded-transport reuse) and did not self-certify independence.
- 6 pre-existing skips and 409 deprecation warnings unchanged (no source touched).
- Lain report route measured clean at 320px/200% on current bytes (V2 session); no redesign made.
