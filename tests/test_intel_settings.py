"""Tests for intel_sources.settings module - strict TDD."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

@pytest.fixture(autouse=True)
def isolated_settings_discovery(tmp_path, monkeypatch):
    """Keep automatic discovery inside each test's temporary project."""
    import intel_sources.settings as mod

    synthetic = tmp_path / "src" / "intel_sources" / "settings.py"
    monkeypatch.setattr(mod, "__file__", str(synthetic))
    assert Path(mod.__file__).parent.parent.parent == tmp_path
    return tmp_path / "config"


# Independent expected literals for the portable default contract.
# Deliberately NOT imported from the production module: the oracle is
# the literal policy below, so a swapped name/adapter/endpoint/URL or a
# changed default value fails this correction instead of passing silently.
EXPECTED_PORTABLE = {
    "schema_version": 1,
    "sources": [
        {"id": "openrouter-models", "name": "OpenRouter Models", "adapter": "catalog", "enabled": True,
         "url": "https://openrouter.ai/api/v1/models"},
        {"id": "github-trending", "name": "GitHub Trending", "adapter": "github", "enabled": True,
         "url": "https://github.com/trending?since=daily"},
        {"id": "skills-hot", "name": "Skills Hot", "adapter": "skills", "enabled": True,
         "url": "https://skills.sh/trending"},
        {"id": "unorouter-pricing", "name": "UnoRouter Pricing", "adapter": "catalog", "enabled": True,
         "url": "https://api.unorouter.com/api/pricing/catalog"},
    ],
    "topics": ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"],
    "priorities": {
        "free-api": 5, "models": 4, "agents": 3, "mcp": 2, "skills": 2,
        "devtools": 1, "news": 1,
    },
    "categories": ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"],
    "max_items": 5,
    "max_age_days": 7,
    "min_score": 60,
    "schedule": {"timezone": "Africa/Cairo", "hour": 10, "minute": 0},
}

EXPECTED_PORTABLE_POLICY = {k: v for k, v in EXPECTED_PORTABLE.items() if k != "sources"}
# Expected result of validate_settings() applied to a partial object whose
# only non-sources fields are topics=["agents"] and max_items=7.
EXPECTED_PARTIAL_POLICY = {
    "schema_version": 1,
    "sources": [],
    "topics": ["agents"],
    "priorities": {"free-api": 0, "models": 0, "agents": 0, "mcp": 0, "skills": 0,
                  "devtools": 0, "news": 0},
    "categories": ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"],
    "max_items": 7,
    "max_age_days": 7,
    "min_score": 60,
    "schedule": {"timezone": "Africa/Cairo", "hour": 10, "minute": 0},
}


class TestDefaultSettings:
    """Tests for default_settings() function."""

    def test_config_object_preserves_selection_settings(self, tmp_path):
        from intel_sources.settings import default_settings, validate_settings

        configured = {
            "schema_version": 1,
            "sources": [{"id": "openrouter-models", "name": "OpenRouter Models",
                         "adapter": "catalog", "enabled": True}],
            "topics": ["models", "agents"],
            "priorities": {"models": 5, "agents": 4},
            "categories": ["models", "agents"],
            "max_items": 3,
            "max_age_days": 6,
            "min_score": 70,
            "schedule": {"timezone": "Africa/Cairo", "hour": 10, "minute": 15},
        }
        (tmp_path / "sources.json").write_text(json.dumps(configured), encoding="utf-8")

        assert default_settings(str(tmp_path)) == validate_settings(configured)

    def test_default_settings_portable_fallback_four_daily_adapters(self, tmp_path):
        """Portable fallback matches every source field and the complete policy."""
        from intel_sources.settings import default_settings, validate_settings

        settings = default_settings(config_dir=str(tmp_path / "missing-config"))
        assert settings == EXPECTED_PORTABLE
        assert all(source["enabled"] is True for source in settings["sources"])
        assert validate_settings(settings) == EXPECTED_PORTABLE

    def test_default_settings_returns_dict(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert isinstance(settings, dict)

    def test_default_settings_has_schema_version(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert settings.get("schema_version") == 1

    def test_default_settings_has_sources_list(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "sources" in settings
        assert isinstance(settings["sources"], list)

    def test_default_settings_has_topics_list(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "topics" in settings
        assert isinstance(settings["topics"], list)

    def test_default_settings_has_priorities_dict(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "priorities" in settings
        assert isinstance(settings["priorities"], dict)

    def test_default_settings_priorities_bounded_0_5(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        for category, priority in settings["priorities"].items():
            assert isinstance(priority, int)
            assert 0 <= priority <= 5

    def test_default_settings_has_categories_list(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "categories" in settings
        expected = ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"]
        assert settings["categories"] == expected

    def test_default_settings_max_items_bounded(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "max_items" in settings
        assert isinstance(settings["max_items"], int)
        assert 1 <= settings["max_items"] <= 10
        assert settings["max_items"] == 5

    def test_default_settings_max_age_days_bounded(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "max_age_days" in settings
        assert isinstance(settings["max_age_days"], int)
        assert 1 <= settings["max_age_days"] <= 30
        assert settings["max_age_days"] == 7

    def test_default_settings_min_score_bounded(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "min_score" in settings
        assert isinstance(settings["min_score"], int)
        assert 0 <= settings["min_score"] <= 100
        assert settings["min_score"] == 60

    def test_default_settings_has_schedule(self):
        from intel_sources.settings import default_settings
        settings = default_settings()
        assert "schedule" in settings
        assert isinstance(settings["schedule"], dict)
        assert settings["schedule"]["timezone"] == "Africa/Cairo"
        assert settings["schedule"]["hour"] == 10
        assert settings["schedule"]["minute"] == 0


class TestLoadSettings:
    """Tests for load_settings(path) function."""

    def test_load_settings_reads_valid_json(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_data = {"schema_version": 1, "max_items": 3, "max_age_days": 14}
        test_file.write_text(json.dumps(test_data))
        result = load_settings(test_file)
        # Should validate and fill in defaults for missing keys
        assert result["schema_version"] == 1
        assert result["max_items"] == 3
        assert result["max_age_days"] == 14
        # Should have all required keys with defaults
        assert "sources" in result
        assert "topics" in result
        assert "priorities" in result
        assert "categories" in result
        assert "min_score" in result
        assert "schedule" in result

    def test_load_settings_returns_defaults_when_file_missing(self, tmp_path):
        from intel_sources.settings import load_settings, default_settings
        missing_file = tmp_path / "nonexistent.json"
        result = load_settings(missing_file)
        expected = default_settings()
        assert result == expected

    def test_load_settings_validates_schema_version(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({"schema_version": 2}))
        with pytest.raises(ValueError, match="schema_version"):
            load_settings(test_file)

    def test_load_settings_validates_sources_structure(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({"schema_version": 1, "sources": "not a list"}))
        with pytest.raises(ValueError, match="sources"):
            load_settings(test_file)

    def test_load_settings_validates_source_fields(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({
            "schema_version": 1,
            "sources": [{"id": "test"}]  # missing required fields
        }))
        with pytest.raises(ValueError, match="source"):
            load_settings(test_file)

    def test_load_settings_validates_priorities_bounds(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({
            "schema_version": 1,
            "priorities": {"free-api": 10}  # out of bounds
        }))
        with pytest.raises(ValueError, match="priority"):
            load_settings(test_file)

    def test_load_settings_validates_max_items_bounds(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({
            "schema_version": 1,
            "max_items": 0  # out of bounds
        }))
        with pytest.raises(ValueError, match="max_items"):
            load_settings(test_file)

    def test_load_settings_validates_max_age_days_bounds(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({
            "schema_version": 1,
            "max_age_days": 0  # out of bounds
        }))
        with pytest.raises(ValueError, match="max_age_days"):
            load_settings(test_file)

    def test_load_settings_validates_min_score_bounds(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({
            "schema_version": 1,
            "min_score": -1  # out of bounds
        }))
        with pytest.raises(ValueError, match="min_score"):
            load_settings(test_file)

    def test_load_settings_validates_schedule_structure(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({
            "schema_version": 1,
            "schedule": {"timezone": "Invalid/Zone"}  # invalid timezone
        }))
        with pytest.raises(ValueError, match="timezone"):
            load_settings(test_file)

    def test_load_settings_rejects_unknown_keys(self, tmp_path):
        from intel_sources.settings import load_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({
            "schema_version": 1,
            "unknown_key": "not allowed"
        }))
        with pytest.raises(ValueError, match="unknown"):
            load_settings(test_file)


class TestSaveSettings:
    """Tests for save_settings(path, value) function."""

    def test_save_settings_writes_validated_dict(self, tmp_path):
        from intel_sources.settings import save_settings, load_settings
        test_file = tmp_path / "settings.json"
        test_data = {"schema_version": 1, "max_items": 3}
        result = save_settings(test_file, test_data)
        # Should return fully validated dict with defaults filled in
        assert result["schema_version"] == 1
        assert result["max_items"] == 3
        assert "sources" in result
        assert "topics" in result
        assert "priorities" in result
        assert "categories" in result
        assert "max_age_days" in result
        assert "min_score" in result
        assert "schedule" in result
        loaded = load_settings(test_file)
        assert loaded == result

    def test_save_settings_validates_before_write(self, tmp_path):
        from intel_sources.settings import save_settings
        test_file = tmp_path / "settings.json"
        with pytest.raises(ValueError):
            save_settings(test_file, {"schema_version": 1, "max_items": 0})

    def test_save_settings_atomic_write(self, tmp_path):
        from intel_sources.settings import save_settings
        test_file = tmp_path / "settings.json"
        test_file.write_text(json.dumps({"schema_version": 1, "max_items": 5}))
        original = test_file.read_bytes()
        save_settings(test_file, {"schema_version": 1, "max_items": 7})
        new = test_file.read_bytes()
        assert new != original

    def test_save_settings_preserves_all_fields(self, tmp_path):
        from intel_sources.settings import save_settings, load_settings, default_settings
        test_file = tmp_path / "settings.json"
        defaults = default_settings()
        save_settings(test_file, defaults)
        loaded = load_settings(test_file)
        assert loaded == defaults


class TestSettingsIntegration:
    """Integration tests for settings module."""

    def test_roundtrip_default_settings(self, tmp_path):
        from intel_sources.settings import default_settings, save_settings, load_settings
        test_file = tmp_path / "settings.json"
        defaults = default_settings()
        save_settings(test_file, defaults)
        loaded = load_settings(test_file)
        assert loaded == defaults

    def test_sources_loaded_from_config_file(self, tmp_path):
        from intel_sources.settings import load_settings, default_settings
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        sources_file = config_dir / "sources.json"
        sources_data = [
            {"id": "github-trending", "name": "GitHub Trending", "adapter": "github", "enabled": True, "url": "https://github.com/trending"},
            {"id": "skills-sh", "name": "Skills.sh", "adapter": "skills", "enabled": False}
        ]
        sources_file.write_text(json.dumps(sources_data))
        # The default_settings should load sources from config/sources.json
        settings = default_settings(config_dir=str(config_dir))
        assert settings["sources"] == sources_data


class TestPortableOverrideBehaviors:
    """Tests for portable default override behaviors - validate_settings({}) semantics."""

    def test_validate_settings_empty_dict_contract(self):
        """Validator-empty contract: validate_settings({}) returns sources[] (schema-only).

        Current control (not a recreated RED): the schema-only validator returns an empty
        sources list when sources is absent; the four portable defaults are supplied only by
        the loader (default_settings / load_settings), never by validate_settings.
        """
        from intel_sources.settings import validate_settings
        result = validate_settings({})
        assert result["sources"] == []
        partial = {"schema_version": 1, "topics": ["models"]}
        partial_result = validate_settings(partial)
        assert partial_result["sources"] == []

    def test_validate_settings_empty_dict_preserves_portable_defaults(self):
        """validate_settings({}) returns empty sources; portable fallback lives in the loader."""
        from intel_sources.settings import validate_settings
        result = validate_settings({})
        assert result["sources"] == []

    def test_validate_settings_explicit_empty_sources_list_overrides_portable(self):
        """Explicit empty list [] in settings overrides portable defaults."""
        from intel_sources.settings import validate_settings
        result = validate_settings({"sources": []})
        assert result["sources"] == []

    def test_validate_settings_explicit_disabled_sources_overrides_portable(self):
        """Explicit disabled sources list overrides portable defaults."""
        from intel_sources.settings import validate_settings
        custom_sources = [{"id": "custom", "name": "Custom", "adapter": "catalog", "enabled": False}]
        result = validate_settings({"sources": custom_sources})
        assert result["sources"] == custom_sources

    def test_validate_settings_full_object_policy_preserved(self):
        """Full object policy with all fields preserved, not merged with portable."""
        from intel_sources.settings import validate_settings
        full_policy = {
            "schema_version": 1,
            "sources": [{"id": "test", "name": "Test", "adapter": "catalog", "enabled": True, "url": "https://test.com"}],
            "topics": ["custom-topic"],
            "priorities": {"models": 5, "agents": 4},
            "categories": ["models"],
            "max_items": 3,
            "max_age_days": 14,
            "min_score": 80,
            "schedule": {"timezone": "UTC", "hour": 12, "minute": 30},
        }
        result = validate_settings(full_policy)
        assert result == full_policy

    def test_validate_settings_partial_object_merges_with_defaults(self):
        """Partial object policy merges provided fields with defaults for missing ones."""
        from intel_sources.settings import validate_settings
        partial = {
            "schema_version": 1,
            "topics": ["models"],
            "max_items": 7,
        }
        result = validate_settings(partial)
        # Provided fields preserved
        assert result["topics"] == ["models"]
        assert result["max_items"] == 7
        # Missing fields get defaults
        assert result["schema_version"] == 1
        assert result["categories"] == ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"]
        assert result["schedule"]["timezone"] == "Africa/Cairo"
        # Sources get empty list from the schema-only validator
        assert result["sources"] == []

    def test_default_settings_per_call_independence(self, tmp_path):
        """Every mutable field/source is independent, including a later third call."""
        from intel_sources.settings import default_settings
        cfg = str(tmp_path / "nope")
        s1 = default_settings(config_dir=cfg)
        s2 = default_settings(config_dir=cfg)
        assert s1 == s2 == EXPECTED_PORTABLE
        assert s1 is not s2
        mutable_fields = ("sources", "topics", "priorities", "categories", "schedule")
        for field in mutable_fields:
            assert s1[field] is not s2[field]
        for first, second in zip(s1["sources"], s2["sources"]):
            assert first is not second
        for source in s1["sources"]:
            source["enabled"] = False
            source["name"] = "Changed"
        s1["max_items"] = 99
        s1["schedule"]["hour"] = 0
        s1["priorities"]["models"] = 5
        s1["topics"].append("changed")
        s1["categories"].clear()
        assert s2 == EXPECTED_PORTABLE
        s3 = default_settings(config_dir=cfg)
        assert s3 == EXPECTED_PORTABLE
        assert len({id(s1), id(s2), id(s3)}) == 3
        for field in mutable_fields:
            assert len({id(s1[field]), id(s2[field]), id(s3[field])}) == 3
        for first, second, third in zip(s1["sources"], s2["sources"], s3["sources"]):
            assert len({id(first), id(second), id(third)}) == 3

    def test_load_settings_missing_file_returns_portable_defaults(self, tmp_path):
        """load_settings(missing_file) returns the exact portable defaults."""
        from intel_sources.settings import load_settings
        missing = tmp_path / "missing.json"
        result = load_settings(missing)
        assert result == EXPECTED_PORTABLE

    def test_default_settings_malformed_json_falls_back_to_portable(self, tmp_path):
        """malformed sources.json falls back to portable defaults exactly."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "sources.json").write_text("{ not valid")
        settings = default_settings(config_dir=str(config_dir))
        assert settings == EXPECTED_PORTABLE

    def test_default_settings_schema_invalid_object_falls_back(self, tmp_path):
        """schema-invalid object in sources.json falls back to portable defaults exactly."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "sources.json").write_text(json.dumps({"schema_version": 99}))
        settings = default_settings(config_dir=str(config_dir))
        assert settings == EXPECTED_PORTABLE

    def test_default_settings_invalid_root_type_falls_back(self, tmp_path):
        """invalid root type (not dict/list) falls back to portable defaults exactly."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "sources.json").write_text(json.dumps(42))
        settings = default_settings(config_dir=str(config_dir))
        assert settings == EXPECTED_PORTABLE

    def test_default_settings_invalid_source_entry_falls_back(self, tmp_path):
        """invalid source entry in bare list falls back to portable defaults exactly."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "sources.json").write_text(json.dumps([{"id": "x"}]))
        settings = default_settings(config_dir=str(config_dir))
        assert settings == EXPECTED_PORTABLE

    def test_default_settings_valid_legacy_list_preserved_exactly(self, tmp_path):
        """valid legacy list overrides portable defaults exactly."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        legacy = [
            {"id": "my-custom", "name": "My Custom", "adapter": "catalog", "enabled": True, "url": "https://example.com"},
        ]
        (config_dir / "sources.json").write_text(json.dumps(legacy))
        settings = default_settings(config_dir=str(config_dir))
        assert settings["sources"] == legacy
        # verify no built-ins appended
        ids = {s["id"] for s in settings["sources"]}
        assert "openrouter-models" not in ids

    def test_default_settings_explicit_empty_list_overrides_portable(self, tmp_path):
        """explicit empty list [] overrides portable defaults."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        (config_dir / "sources.json").write_text(json.dumps([]))
        settings = default_settings(config_dir=str(config_dir))
        assert settings["sources"] == []

    def test_default_settings_all_disabled_list_preserved(self, tmp_path):
        """all-disabled list preserved exactly."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        disabled = [
            {"id": "x", "name": "X", "adapter": "catalog", "enabled": False},
        ]
        (config_dir / "sources.json").write_text(json.dumps(disabled))
        settings = default_settings(config_dir=str(config_dir))
        assert settings["sources"] == disabled

    def test_default_settings_full_object_preserves_exact_fields(self, tmp_path):
        """full object file preserves exact custom sources+all policy fields."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        full = {
            "schema_version": 1,
            "sources": [{"id": "custom", "name": "Custom", "adapter": "catalog", "enabled": True, "url": "https://custom.com"}],
            "topics": ["models"],
            "priorities": {"models": 5},
            "categories": ["models"],
            "max_items": 3,
            "max_age_days": 14,
            "min_score": 80,
            "schedule": {"timezone": "UTC", "hour": 12, "minute": 30},
        }
        (config_dir / "sources.json").write_text(json.dumps(full))
        settings = default_settings(config_dir=str(config_dir))
        assert settings == full

    def test_default_settings_partial_object_without_sources_empty(self, tmp_path):
        """partial object without sources: sources[] plus provided fields."""
        from intel_sources.settings import default_settings
        config_dir = tmp_path / "cfg"
        config_dir.mkdir()
        partial = {
            "schema_version": 1,
            "topics": ["agents"],
            "max_items": 7,
        }
        (config_dir / "sources.json").write_text(json.dumps(partial))
        settings = default_settings(config_dir=str(config_dir))
        assert settings["sources"] == []
        assert settings["topics"] == ["agents"]
        assert settings["max_items"] == 7
        # original remaining defaults preserved
        assert settings == EXPECTED_PARTIAL_POLICY

    def test_default_settings_missing_config_dir_returns_four_adapters(self, tmp_path):
        """Missing config/sources.json via explicit temp config_dir returns exact portable mapping."""
        from intel_sources.settings import default_settings
        settings = default_settings(config_dir=str(tmp_path / "nope"))
        assert settings == EXPECTED_PORTABLE
        # Independent per-source literal assertions (not just an ID set).
        by_id = {s["id"]: s for s in settings["sources"]}
        assert by_id["openrouter-models"]["url"] == "https://openrouter.ai/api/v1/models"
        assert by_id["github-trending"]["url"] == "https://github.com/trending?since=daily"
        assert by_id["skills-hot"]["url"] == "https://skills.sh/trending"
        assert by_id["unorouter-pricing"]["url"] == "https://api.unorouter.com/api/pricing/catalog"
        for s in settings["sources"]:
            assert s["enabled"] is True
        assert settings["topics"] == ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"]
        assert settings["max_items"] == 5
        assert settings["schedule"]["timezone"] == "Africa/Cairo"

    def test_load_settings_isolated_from_local_config(self, tmp_path, monkeypatch):
        """load_settings(None)/missing/malformed are isolated from project-local config.

        The actual settings module __file__ is monkeypatched to a synthetic
        tmp_path/src/intel_sources/settings.py. Its three-parent traversal
        resolves exactly to tmp_path, so the discovered config dir is
        tmp_path/config. No source copy/import replacement/global mock.
        """
        import intel_sources.settings as mod
        synthetic = tmp_path / "src" / "intel_sources" / "settings.py"
        monkeypatch.setattr(mod, "__file__", str(synthetic))
        # Three-parent traversal resolves exactly to tmp_path -> tmp_path/config.
        assert Path(mod.__file__).parent.parent.parent == tmp_path
        # No discovered config: exact portable mapping via every fallback route.
        assert mod.default_settings() == EXPECTED_PORTABLE
        assert mod.load_settings(None) == EXPECTED_PORTABLE
        assert mod.load_settings(tmp_path / "missing.json") == EXPECTED_PORTABLE
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json")
        assert mod.load_settings(bad) == EXPECTED_PORTABLE
        # Syntactically valid but schema-invalid input still raises.
        invalid = tmp_path / "invalid.json"
        invalid.write_text(json.dumps({"schema_version": 2}))
        with pytest.raises(ValueError, match="schema_version"):
            mod.load_settings(invalid)
        # A valid discovered configuration at the synthetic project config dir wins,
        # proving the traversal resolves to tmp_path/config (README precedence).
        override = [{"id": "custom", "name": "Custom", "adapter": "catalog",
                     "enabled": True, "url": "https://example.com"}]
        cfg = tmp_path / "config"
        cfg.mkdir()
        (cfg / "sources.json").write_text(json.dumps(override))
        discovered = mod.default_settings()
        expected = {**EXPECTED_PORTABLE_POLICY, "sources": override}
        assert discovered == expected
        for fallback_path in (None, tmp_path / "missing.json", bad):
            assert mod.load_settings(fallback_path) == expected

    def test_load_settings_malformed_json_returns_portable_defaults(self, tmp_path):
        """load_settings(malformed_json) returns the exact portable defaults."""
        from intel_sources.settings import load_settings
        bad = tmp_path / "bad.json"
        bad.write_text("{ not valid json")
        result = load_settings(bad)
        assert result == EXPECTED_PORTABLE

    def test_load_settings_invalid_schema_raises_not_returns_defaults(self, tmp_path):
        """load_settings(invalid_schema) raises ValueError, doesn't silently return defaults."""
        from intel_sources.settings import load_settings
        bad = tmp_path / "bad.json"
        bad.write_text(json.dumps({"schema_version": 2}))
        import pytest
        with pytest.raises(ValueError, match="schema_version"):
            load_settings(bad)





class TestSourceValidation:
    """Tests for source config validation."""

    def test_source_requires_id_name_adapter_enabled(self):
        from intel_sources.settings import validate_source
        valid_source = {"id": "test", "name": "Test", "adapter": "test", "enabled": True}
        assert validate_source(valid_source) == valid_source

    def test_source_optional_url_repo_query(self):
        from intel_sources.settings import validate_source
        source = {"id": "test", "name": "Test", "adapter": "test", "enabled": True, "url": "https://example.com", "repo": "owner/repo", "query": "test"}
        assert validate_source(source) == source

    def test_source_rejects_unknown_fields(self):
        from intel_sources.settings import validate_source
        source = {"id": "test", "name": "Test", "adapter": "test", "enabled": True, "secret": "not allowed"}
        with pytest.raises(ValueError, match="unknown"):
            validate_source(source)

    def test_source_enabled_must_be_bool(self):
        from intel_sources.settings import validate_source
        source = {"id": "test", "name": "Test", "adapter": "test", "enabled": "yes"}
        with pytest.raises(ValueError, match="enabled"):
            validate_source(source)

    def test_source_id_must_be_string(self):
        from intel_sources.settings import validate_source
        source = {"id": 123, "name": "Test", "adapter": "test", "enabled": True}
        with pytest.raises(ValueError, match="id"):
            validate_source(source)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])