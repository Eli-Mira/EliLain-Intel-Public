# Verification fixtures

These two files are **generated, not hand-written**. Rebuild them with:

```bash
.venv/Scripts/python.exe scripts/build_test_fixture.py
.venv/Scripts/python.exe scripts/build_project_evaluation_fixture.py
```

They exist because the render/shortlist verification tests must run in CI,
where `data/` (gitignored, never present) cannot be relied on. `tests/verification_fixtures.py`
prefers real saved state when it exists and falls back to these files otherwise,
so local runs still verify live data while CI verifies the same code path.

- `intel_state_sample.json` — bounded slice of `data/intel_state.json`: public
  `https` discovery rows only, third-party summary/description/evidence text
  stripped, `fingerprint_data` reduced to numeric counters.
- `project_evaluations_sample.json` — recomputed from the inventory fixture, so
  `evaluate(snapshot) == result` holds by construction.

Both builders **fail closed**: they refuse to write if the output contains
loopback URLs, credential shapes, tracebacks, or any non-null summary text.
The inventory guard already caught fetched GitHub README text hiding inside
`fingerprint_data`, which is why that field is reduced rather than copied.

Scale-relative assertions, not hardcoded counts: the fixture is a sample, so
tests assert disclosed coverage and honoured export caps, not that the
inventory exceeds some live row count.