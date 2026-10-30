# INTEL V3 source registry

Authoritative machine-readable catalog: `src/intel_sources/source_registry.py` (schema-tested, all candidates disabled by default). This doc summarizes; the module is the source of truth.

Existing ACTIVE portable defaults (unchanged semantics): openrouter-models, github-trending, skills-hot, unorouter-pricing. Candidate rows: see module SOURCE_CATALOG — states CANDIDATE / PENDING_PERMISSION / READY_OFFLINE with per-row reason, method, tier, last-checked. No source is auto-enabled; live activation is a separate owner decision per source.
