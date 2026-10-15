# Eli Lain — AI/Tech News Tracker

> A standalone public AI/tool dashboard. Set `PUBLIC_REPORT_URL` to your own Cloudflare Pages production root before publishing; example domains in this repository are not operational deployments.
>
> Hosted release acceptance is separate from local implementation; publishing remains opt-in.
>
> **Licence:** the MIT Licence covers source code only, not third-party artwork. See
> [`THIRD-PARTY-NOTICE.md`](THIRD-PARTY-NOTICE.md) before forking or redistributing.

AI/Tech News Tracker — pulls latest developments from GitHub, Reddit, Hacker News, arXiv, and tech news sites.

## How It Runs

- **GitHub Actions** supports manual scans. The scheduled entry uses 10:00 in `Africa/Cairo`; publish and notification flags remain opt-in.
- Fetches from GitHub releases, Reddit RSS, and tech news sources
- Scores, summarizes, deduplicates
- Generates an HTML report with the "lain" theme
- Pushes notifications to configured channels

## Notification Channels

| Channel | Setup |
|---------|-------|
| Discord | Bot token + channel ID |
| Telegram | Bot token + chat ID |
| Email | SMTP host/port/user/password |
| WhatsApp | Via Hermes Gateway (optional) |

## Local Dev

```bash
python -m venv .venv
# Activate .venv using your platform's shell, then:
python -m pip install -r requirements-lock.txt
python -m pip install -e ./src/tracker-core --no-deps
python -m pip install -e . --no-deps
```

## Usage

```bash
# Public daily digest (no local provider database and no AI summarizer needed)
python main.py daily --data-dir ./data --public-dir ./public
python scripts/gate_daily_nonempty.py --data-dir ./data --public-dir ./public
python scripts/verify_pages_report.py --public-dir ./public --preflight-only

# Run full tracking cycle
python main.py run

# Generate report
python main.py report

# List recent news
python main.py list --days 7

# Quick run + report for today
python main.py today

# Use lain theme
python main.py report --theme lain
```

## Sources

- **GitHub**: Releases from tracked orgs (OpenAI, Anthropic, Google DeepMind, Meta AI, etc.)
- **Reddit**: r/MachineLearning, r/LocalLLaMA, r/artificial, r/OpenAI, r/ChatGPT, r/GenerativeAI
- **Hacker News**: AI/LLM related stories via Algolia API
- **arXiv**: New papers in cs.AI, cs.CL, cs.LG, cs.CV, cs.NE
- **RSS Feeds**: TechCrunch AI, The Verge AI, VentureBeat AI, company blogs

## Output

- Routine internal HTML replaces `reports/latest.html`; sanitized public HTML replaces `public/index.html`.
- Current deduplicated discovery state and bounded first-seen/fingerprint metadata live in `data/intel_state.json`; the working news SQLite database and cooldowns are preserved.
- Public notifications are hosted-link-first by default. An HTML attachment is secondary and explicit (`PUBLIC_REPORT_ATTACHMENT=true` or `scripts/publish_report.py --attach-html`).
- Restart/partial-failure receipts are mode-bound and capped; no exactly-once guarantee or automatic send retry.
- Existing archives are not deleted. `python scripts/cleanup_local_outputs.py --dry-run --json` lists exact candidates/exclusions for separate approval.

See [public publishing](docs/public-publishing.md) for environment variables, deployment verification, delivery receipts and retention boundaries.

## Portable Settings Fallback (Daily Sources)

The system includes built-in portable defaults for the four daily-source adapters, used automatically when no `config/sources.json` is present or valid:

| Source ID | Name | Adapter | Endpoint |
|-----------|------|---------|----------|
| `openrouter-models` | OpenRouter Models | catalog | `https://openrouter.ai/api/v1/models` |
| `github-trending` | GitHub Trending | github | `https://github.com/trending?since=daily` |
| `skills-hot` | Skills Hot | skills | `https://skills.sh/trending` |
| `unorouter-pricing` | UnoRouter Pricing | catalog | `https://api.unorouter.com/api/pricing/catalog` |

**Fallback behavior:**

The validator and the loader have separate contracts. Portable built-in defaults live only in the loader; the validator is schema-only.

- `validate_settings({})` returns `sources: []` whenever `sources` is absent from the input dict (schema-only semantics). It never supplies the four portable defaults.
- `default_settings(config_dir=None)`: auto-discovers `<project>/config/sources.json`, or loads the explicit `config_dir` passed in. A missing directory, missing file, malformed JSON, schema-invalid object, invalid root type, or invalid source entry all fall back to the four portable defaults with the unchanged default non-source policy. A valid file **overrides** the portable defaults exactly:
  - Bare/legacy list of source objects — preserved verbatim; the four built-ins are **not** appended.
  - Explicit `{"sources": []}` — disables all daily sources.
  - All-disabled list — preserved exactly.
  - Full object policy (all fields) — preserved exactly, not merged.
  - Partial object without `sources` — `sources: []` plus the provided fields (`topics`, `max_items`, ...) and the original remaining defaults.
- `load_settings(None)`, a missing file, or unreadable/malformed JSON **delegates to `default_settings()`**, which auto-discovers `<project>/config/sources.json`. A valid discovered configuration **wins**; otherwise the portable four-source defaults and the unchanged default non-source policy apply. A file that is syntactically valid but schema-invalid (wrong `schema_version`, bad `sources` shape, out-of-bounds values, unknown keys, invalid timezone) **raises** `ValueError` — schema validation is never silently bypassed.
- Bare/legacy source lists (including `[]` and all-disabled lists) are accepted only by `default_settings()` when reading `sources.json`. A successfully parsed file passed directly to `load_settings()` must contain a full/partial object policy; a bare list raises `ValueError`. For object policies, provided fields win, missing non-source fields receive schema defaults, and absent `sources` validates to `[]`; only the loader's fallback supplies the four portable sources.
- Each call to `default_settings()` returns fresh copies — no shared mutable state; mutating one result does not affect another.
- Broader catalog/provider/local-FreeLLMAPI/news/discovery systems are separate and not covered by these four daily adapters

## Secrets

Credentials are stored in `.env` (local) and GitHub Secrets (Actions). See `.env.example` for required variables.

This repository is an allowlisted source release with fresh Git history. Operator configuration, state, logs, credentials and private development/evidence history are not distributed. Project-fit profiles and test identities are generic examples, not an operator's portfolio or live provider configuration.

## Verification

```bash
python -m playwright install --with-deps chromium
python -m pytest tests -q
python -m pytest tests/test_daily_producer_pipeline.py -q
```

Generate `public/index.html` first: the browser/consumer controls require a real current export. Do not substitute an empty fixture for a live-source acceptance run.

---

*Built by Eli + Lain — 2026*
