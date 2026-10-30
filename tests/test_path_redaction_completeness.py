"""Regression tests: path redaction must remove the whole personal path.

Recorded defect (found by planting real content and exporting): the digest
sanitizer redacted only the drive prefix, so "[LOCAL-PATH-REDACTED]" became
"[PATH REDACTED]Users/jdoe/notes.md" — leaking the username and personal
segments — and the public export validator was ^-anchored, so the remnant
passed validation and reached the exported HTML.
"""
import pytest

from intel_sources.daily_digest import _sanitize_text
from intel_sources.daily_public import _reject_local_path


@pytest.mark.parametrize("text, leaked", [
    ("Contact C:/Users/jdoe/personal/notes.md for details.", "jdoe"),
    ("Contact C:\\Users\\jdoe\\personal\\notes.md for details.", "jdoe"),
    ("see /home/jdoe/notes.md now", "jdoe"),
    ("file:///etc/canary.conf", "canary.conf"),
    ("config at /var/canary/private.json here", "canary"),
])
def test_sanitize_redacts_full_personal_path(text, leaked):
    """No personal path segment may survive sanitization."""
    out = _sanitize_text(text)
    assert "[PATH REDACTED]" in out
    assert leaked not in out


@pytest.mark.parametrize("url", [
    "https://example.com/s:/foo",
    "https://x/s:/y",
    "https://openrouter.ai/path/s:/weird",
])
def test_sanitize_still_keeps_urls_intact(url):
    assert _sanitize_text(url) == url


@pytest.mark.parametrize("value", [
    "leading C:/Users/canary/x",
    "mid-string /home/canary/x path",
])
def test_public_validator_rejects_paths_anywhere(value):
    """The export validator must fail closed on paths anywhere in a field.

    (The "[PATH REDACTED]Users/..." remnant class cannot be generated anymore
    once the sanitizer redacts the whole token; historical digests are never
    re-exported.)
    """
    with pytest.raises(ValueError, match="local path"):
        _reject_local_path(value)


@pytest.mark.parametrize("value", [
    "https://openrouter.ai/models",
    "plain prose with no path",
])
def test_public_validator_allows_clean_values(value):
    _reject_local_path(value)  # must not raise
