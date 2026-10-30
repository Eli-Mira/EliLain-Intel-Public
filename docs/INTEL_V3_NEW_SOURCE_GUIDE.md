# Adding a new source to EliLain Intel (V3 guide)

Read `docs/INTEL_V3_SOURCE_REGISTRY.md` and `src/intel_sources/source_registry.py`
first. The registry is the state of record; this guide is the workflow.

## Rules

1. **A source is never enabled by writing code.** Activation is a per-source
   owner decision (ToS/robots/rate/legal), reflected in the registry row and
   the daily gate. `validate_registry_entry` fails closed: only
   `ACTIVE_EXISTING` rows may be `enabled`.
2. **Respect the approved boundaries.** Reuse `_safe_get`/`_fetch_json` from
   `daily_sources` (guarded transport: SSRF/private-IP guards, byte caps,
   deadlines, WAF detection). Never fetch with raw httpx calls.
3. **Everything fetched is untrusted data.** Run text through
   `expanded_sources._summary_text` (sanitize + bound). Never execute
   fetched scripts, `SKILL.md` instructions, or repo content. Never
   auto-install skills.
4. **Identity over headlines.** Give candidates a stable `id`
   (`family:identifier`), a canonical `source_url`, a truthful
   `published_at` (or none — `UNKNOWN_FRESHNESS` is honest), and an
   `evidence_kind` from the tier ladder (official > editorial > community >
   directory).
5. **No policy changes.** Do not touch `min_score`, `_APPROVED_DAILY_SOURCES`,
   `default_settings()` semantics, or the public export allowlist. New source
   ids are rejected by the daily gate until separately approved and added.

## Workflow

1. **Research & register.** Verify the endpoint/permission status; add a
   registry row with honest `status` (`CANDIDATE` / `PENDING_PERMISSION` /
   `READY_OFFLINE`), provenance, and reason. Unverified rows say
   `not_revalidated`.
2. **Implement the adapter** in `src/intel_sources/expanded_sources.py`
   following the existing three patterns (feed / releases / reference).
   Return `(candidates, status_info)`; errors use `_error_status` with a
   specific `error_code` (`rate_limited`, `malformed_feed`, `bad_config`,
   `source_failed`). Honor 429/Retry-After; stop fan-out on rate limits.
3. **Fixtures, not live calls.** Add offline tests in
   `tests/test_v3_m5_expanded_sources.py` (or a new file): a normalizing
   happy path from committed fixture bytes, at least one failure case
   (malformed / rate-limited / oversized), and a sanitization assertion for
   untrusted text. `pytest tests/ -q` must stay green.
4. **Wire into evidence, not the daily gate.** Candidates flow through
   `evidence.group_findings` / `novelty` automatically once they appear in
   `verify_candidates` output. The daily report keeps its approval boundary.
5. **Update the board.** Registry row, `docs/INTEL_V3_CHECKLIST.md`,
   `docs/INTEL_V3_EVIDENCE_INDEX.md` (test counts + commit).
6. **Request activation.** Live enablement needs an explicit owner decision
   recorded in the registry row and `INTEL_V3_BLOCKERS.md` resolution.
