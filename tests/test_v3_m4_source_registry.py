"""M4 tests: source registry schema, fail-closed enablement, immutable copies."""
import pytest

from intel_sources.source_registry import (
    SOURCE_CATALOG,
    registry_entry,
    registry_sources,
    registry_summary,
    validate_registry,
    validate_registry_entry,
)
from intel_sources.settings import _PORTABLE_DEFAULT_SOURCES


def test_catalog_validates_and_meets_research_targets():
    validated = validate_registry(registry_sources())
    assert len(validated) >= 12  # research documentation target (not live count)
    families = {e["family"] for e in validated}
    assert len(families) >= 4  # distinct source families


def test_only_active_existing_sources_enabled():
    for entry in registry_sources():
        if entry["status"] == "ACTIVE_EXISTING":
            assert entry["enabled"] is True
        else:
            assert entry["enabled"] is False, entry["id"]


def test_portable_defaults_are_active_existing_and_ids_match():
    active = {e["id"] for e in registry_sources() if e["status"] == "ACTIVE_EXISTING"
              and e["family"] in ("provider-catalog", "trending", "skills-directory")}
    defaults = {s["id"] for s in _PORTABLE_DEFAULT_SOURCES}
    assert defaults <= active
    assert active >= {"openrouter-models", "github-trending", "skills-hot", "unorouter-pricing"}


def test_registry_sources_returns_fresh_copies():
    first = registry_sources()
    second = registry_sources()
    assert first == second
    assert first is not second
    target = next(e for e in first if e["id"] == "anthropic-news")
    target["enabled"] = True  # mutation must not leak into the catalog
    target["content_kinds"].append("bogus")
    fresh = registry_entry("anthropic-news")
    assert fresh["enabled"] is False
    assert "bogus" not in fresh["content_kinds"]


def test_registry_entry_lookup():
    entry = registry_entry("github-releases-tracked")
    assert entry is not None and entry["method"] == "repo_releases"
    assert registry_entry("no-such-source") is None


def test_registry_summary_counts():
    summary = registry_summary()
    assert summary["total"] == len(SOURCE_CATALOG)
    assert sum(summary["by_status"].values()) == summary["total"]
    assert sum(summary["by_family"].values()) == summary["total"]
    assert summary["by_status"].get("PENDING_PERMISSION", 0) >= 1


def test_enablement_mismatch_rejected():
    entry = dict(registry_entry("anthropic-news"))
    entry["enabled"] = True  # PENDING_PERMISSION must never enable
    with pytest.raises(ValueError, match="only ACTIVE_EXISTING"):
        validate_registry_entry(entry)
    active = dict(registry_entry("openrouter-models"))
    active["enabled"] = False  # ACTIVE_EXISTING must stay enabled
    with pytest.raises(ValueError, match="must be enabled"):
        validate_registry_entry(active)


def test_unknown_field_and_family_rejected():
    entry = dict(registry_entry("simonwillison-atom"))
    entry["surprise"] = 1
    with pytest.raises(ValueError, match="unknown field"):
        validate_registry_entry(entry)
    entry2 = dict(registry_entry("simonwillison-atom"))
    entry2["family"] = "not-a-family"
    with pytest.raises(ValueError, match="unknown family"):
        validate_registry_entry(entry2)


def test_duplicate_ids_rejected():
    entries = registry_sources()
    with pytest.raises(ValueError, match="duplicate registry id"):
        validate_registry(entries + [dict(entries[0])])


def test_every_candidate_documents_access_and_provenance():
    for entry in registry_sources():
        assert entry["check_provenance"], entry["id"]
        assert entry["reason"], entry["id"]
        assert entry["rate_notes"], entry["id"]
        assert entry["method"] in ("documented_api", "official_feed", "repo_releases",
                                   "manual_reference", "permitted_html_research",
                                   "existing_adapter")
        # Research-guide rows are dated; reported-only rows say so honestly.
        if entry["status"] in ("CANDIDATE", "PENDING_PERMISSION") and \
                entry["last_checked"] != "2026-10-26":
            assert entry["last_checked"] == "not_revalidated", entry["id"]
