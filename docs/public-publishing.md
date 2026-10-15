# Public publishing

## Run locally

Install the exact lock and both local packages as described in README. Copy `.env.example` to `.env` and keep it untracked. The public daily producer needs no provider database, inference key or paid summarizer. Optional AI summarization is off unless explicitly enabled.

Run `python main.py daily --data-dir ./data --public-dir ./public`, then `python scripts/gate_daily_nonempty.py --data-dir ./data --public-dir ./public` and `python scripts/verify_pages_report.py --public-dir ./public --preflight-only`.

Portable daily defaults use four public adapters, the fixed selection cutoff of 60 and category priorities documented in `settings.py`. Empty output is a failed nonempty release gate, not permission to lower the cutoff. Configure your own validated policy through the local settings dashboard or `scripts/run_daily.py`; never commit operational configuration.

## Configure publishing explicitly

Set `PUBLIC_REPORT_URL` to your own production root, for example `https://example.pages.dev/` (illustrative only). Only an HTTPS single-project Pages origin with a root path is admitted: credentials, ports, redirects, queries and fragments are rejected. Standalone hosted verification reads this variable too. Put private owner/repository exclusions in `PRIVATE_REPOSITORIES`, comma separated; both export and artifact preflight enforce them. No private repository identity is embedded in the public source.

Use encrypted repository secrets for `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`, `PRIVATE_REPOSITORIES`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID`. Put only non-secret configuration in repository variables: `CLOUDFLARE_PAGES_PROJECT`, `PUBLIC_REPORT_URL` and opt-in flags. Do not store credential values in plaintext repository variables.

`FREELLMAPI_DB_PATH` and `FREELLMAPI_URL` configure an optional local integration; absent a database override, it reads `data/freeapi.db`. The release includes no database, operator key-ID mapping, saved tier/quota annotations or credentials.

## Deploy, read back, then notify

1. Build the standalone `public/index.html`, run tests, and pass artifact preflight. The directory must contain only this file.
2. With Wrangler authenticated normally, deploy from outside that artifact directory: `wrangler pages deploy ./public --project-name="$CLOUDFLARE_PAGES_PROJECT" --branch=main`.
3. Run `python scripts/verify_pages_report.py --public-dir ./public`. Require an unauthenticated exact-byte SHA256 match on the configured production URL before sending anything.
4. Set `NOTIFICATION_CHANNEL=telegram,discord` and run `python main.py publish --public-dir ./public --no-export`. Link-first is the default; `PUBLIC_REPORT_ATTACHMENT=true` explicitly adds the exact HTML attachment.

Telegram send-response confirmation and Discord independent message readback have different strengths. Neither is proof that a person read a message. WhatsApp is an optional operator-side Hermes integration, not a bundled credential or a substitute for a verified message receipt.

## Workflow safety and retention

The workflow has no push-triggered publication. Manual scan and notify phases are separate. Automatic scheduled jobs require `DAILY_RUN_ENABLED=true`; notification additionally requires `PUBLISH_ENABLED=true`, host deployment requires `CLOUDFLARE_PUBLISH_ENABLED=true`, and the manual publish input must be selected. Keep them false until your own end-to-end acceptance passes.

Only the sanitized `public/index.html` is uploaded as a workflow artifact. Private raw evidence, local configuration and channel destination/message receipts are not uploaded. State cache steps are disabled by default. Local `data/delivery_receipts.json` binds mode and report hash for bounded restart/replay protection; disabling or discarding that state loses cross-run deduplication. There is no exactly-once guarantee or automatic send retry.

Routine reports replace `reports/latest.html`; routine public output replaces `public/index.html`. Do not commit generated files, local configuration, caches, logs or evidence. `scripts/cleanup_local_outputs.py --dry-run --json` inventories existing output before any separately authorized deletion.

## Source and licensing boundary

This is a fresh-history allowlisted source export, not the authoritative private development/evidence history. MIT covers source code; dependencies and third-party media retain their own terms. Read `THIRD-PARTY-NOTICE.md` before redistribution. Generic example project profiles are not a personal project shortlist. Public rendering ignores internal provider/project groups regardless of the supplied payload.
