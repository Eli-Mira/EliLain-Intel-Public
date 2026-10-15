"""Public URL policy: real secret shapes only, without dropping valid identities.

The secret-shape heuristic ran over whole URLs, so any identifier containing
`sk-` (ask-matt, task-skill) or the word `password` was discarded. Those are
legitimate public catalog rows, and dropping them silently removed real
inventory from the report.
"""
from __future__ import annotations

import pytest

from intel_sources.public_report import _public_identity, public_http_url

KEPT = [
    "https://skills.sh/mattpocock/skills/ask-matt",
    "https://skills.sh/emilkowalski/skills/ask-sonner",
    "https://skills.sh/better-auth/skills/email-and-password-best-practices",
    "https://skills.sh/tasks/tidy-tasklist",
    "https://openrouter.ai/openai/gpt-4o",
    "https://github.com/example/ask-secretary",
]
REJECTED = [
    "https://example.org/sk-abc123",
    "https://example.org/«redacted:sk-…»",
    "https://example.org/x/ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
    "https://example.org/x/github_pat_11ABCDEFG0abcdefghijklmnop",
    "https://example.org/x/Bearer abcdefghijklmnopqrstuvwxyz",
    "https://example.org/x/api_key=SECRETVALUE",
    "https://example.org/x/password: hunter2secret",
    "https://example.org/x/access_token=SECRETVALUE",
    "https://example.org/x/secret=SECRETVALUE",
    "https://example.org/x?api_key=canary",
    "https://example.org:3001/a",
    "http://user:pw@example.org/a",
]


@pytest.mark.parametrize("url", KEPT)
def test_legitimate_public_identities_are_not_mistaken_for_secrets(url):
    assert public_http_url(url) == url


@pytest.mark.parametrize("url", REJECTED)
def test_real_secret_and_ambiguity_shapes_are_still_refused(url):
    assert public_http_url(url) == ""


@pytest.mark.parametrize("identifier,url", [
    ("skills:mattpocock/skills/ask-matt", "https://skills.sh/mattpocock/skills/ask-matt"),
    ("openrouter:model:~vendor/alias-model", "https://openrouter.ai/~vendor/alias-model"),
])
def test_validated_identities_reach_the_public_projection(identifier, url):
    source = "skills-sh" if identifier.startswith("skills:") else "openrouter-models"
    assert _public_identity({"id": identifier, "url": url}, source) == (identifier, url)
