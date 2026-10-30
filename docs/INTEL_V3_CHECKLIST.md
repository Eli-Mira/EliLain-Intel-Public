# INTEL V3 checklist — source expansion to internal RC

States: TODO / IN_PROGRESS / PASS / FAIL / BLOCKED / DEFERRED. PASS needs verified bytes, command+exit code, test counts, provenance, no unauthorized side effects, local commit where available.

| ID | Task | Deps | State |
|---|---|---|---|
| M3.1 | Per-candidate exclusion codes in private evidence; frozen-now determinism | - | PASS (970805a) |
| M3.2 | UnoRouter GET-vs-HEAD reuse (fetch-time GET evidence, no duplicate call) | - | PASS (a41e3fb + 970805a) |
| M3.3 | License evidence states (none-declared vs lookup-failure vs verified); policy untouched | - | PASS (970805a) |
| M4 | Source registry module + schema tests + registry doc, disabled-by-default | M3 | PASS (4da263b) |
| M5 | Offline fixture adapters: vendor RSS, GitHub releases, Replace.so reference, research/HF | M4 | PASS (9f31d48 + HF Hub adapter; 5 families: official-news, github-releases, open-source-directory, research, model-catalog) |
| M6 | Evidence module: canonical URL/entity, corroboration grouping, novelty vs baseline | M4 | PASS (9f31d48 + run-level baseline in next commit) |
| M7 | Report sections + honest quiet states verified on current bytes | M6 | PASS (sections/filters/empty states pre-existing and verified; run-level novelty persisted; 320px fixed in ab44585) |
| M8 | Security negative fixtures for new code paths | M5 | PASS (injection inert at render, forged dates, oversized feed, rate-limit stop, enablement fail-closed) |
| M9 | Focused suites + full regression on final bytes | M8 | PASS (1537P at 1c2a8c2; final-bytes rerun recorded in evidence index) |
| M10 | Internal RC pack: registry, demo, status, owner decision sheet | M9 | PASS (demo-output busy+sparse with metrics, INTEL_V3_NEW_SOURCE_GUIDE.md, 51/51 import smoke, INTERNAL_RC status) |

Prior landed (V2 session, 2026-10-26): a41e3fb GET-fallback verification, ab44585 320px daily layout repair, b5aae30 path-redaction completeness. Full suite at b5aae30: 1493P/6S/0F, exit 0 (Windows).
