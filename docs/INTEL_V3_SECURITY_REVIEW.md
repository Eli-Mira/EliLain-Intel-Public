# INTEL V3 security review notes

- Transport: GuardedAsyncTransport + _safe_get/_safe_head unchanged; GET fallback (a41e3fb) reuses identical guards; WAF/401/403 never re-probed.
- Privacy: whole-path redaction + unanchored export validator (b5aae30); export re-check with planted username/path/token clean.
- Untrusted content policy: SKILL.md/README/feed text is data only; no execution, no auto-install (tests in M8 batch).
- Parsers: existing stdlib/feedparser/bs4 only; no new deps; byte caps + deadlines from _safe_get apply to new adapters.
- New-path negatives added in M8 batch (forged dates, 429 Retry-After, hostile feed, skill-text injection).
