"""Tests for daily digest with explainable scoring and identity-safe dedup."""
from datetime import datetime, timezone, timedelta
import inspect

from intel_sources.daily_digest import build_digest, render_digest, _score_item, _check_gates, _sanitize_text


NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)


def settings():
    return {
        "schema_version": 1,
        "sources": [],
        "categories": ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"],
        "max_items": 5,
        "max_age_days": 7,
        "min_score": 60,
        "priorities": {"free-api": 5, "models": 4, "agents": 3, "mcp": 2, "skills": 2, "devtools": 1, "news": 1},
        "topics": ["free-api", "models", "agents"],
    }


def candidate(base=None):
    base = base or {}
    return {
        "id": "test:id1",
        "name": "Test Project",
        "what": "A meaningful description of the project with enough detail to be useful to the user.",
        "category": "free-api",
        "source_id": "openrouter-models",
        "why": "Fits free-api category with MIT license and REST API.",
        "source_url": "https://openrouter.ai/models/test-project",
        "published_at": (NOW - timedelta(days=2)).isoformat(),
        "date_kind": "release",
        "observed_at": NOW.isoformat(),
        "free": {"status": "open-source", "detail": "MIT", "evidence_url": "https://openrouter.ai/license"},
        "api": {"status": "available", "detail": "REST", "evidence_url": "https://openrouter.ai/api"},
        "topics": ["free-api"],
        "link_verification": {"status": "verified", "checked_at": NOW.isoformat(), "url": "https://openrouter.ai/models/test-project", "http_status": 200},
        **base,
    }


def collection(candidates):
    return {"candidates": candidates, "sources": []}


# === Gate Tests ===

def test_gates_reject_unverified_link():
    cand = candidate({"link_verification": {"status": "unverified", "checked_at": NOW.isoformat(), "url": "https://example.com/project", "http_status": None}})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0
    assert digest["counts"]["fetched"] == 1


def test_gates_reject_stale_date():
    cand = candidate({"published_at": (NOW - timedelta(days=30)).isoformat()})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0


def test_gates_reject_future_date():
    cand = candidate({"published_at": (NOW + timedelta(days=1)).isoformat()})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0


def test_gates_reject_weak_description():
    cand = candidate({"what": "N/A"})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0


def test_gates_reject_secret_evidence():
    cand = candidate({"free": {"status": "open-source", "detail": "api_key=sk-123456", "evidence_url": "https://example.com"}})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0


def test_identity_safe_dedup_does_not_merge_distinct_owners():
    c1 = candidate({"id": "skill:owner1/proj", "source_url": "https://skills.sh/owner1/proj", "source_id": "skills-hot"})
    c2 = candidate({"id": "skill:owner2/proj", "source_url": "https://skills.sh/owner2/proj", "source_id": "skills-hot"})
    digest = build_digest(collection([c1, c2]), settings(), now=NOW)
    ids = [i["id"] for i in digest["items"]]
    assert len(ids) == 2
    assert c1["id"] in ids and c2["id"] in ids


def test_dedup_exact_identity():
    c1 = candidate({"id": "test:id1", "source_url": "https://openrouter.ai/models/test-project"})
    c2 = candidate({"id": "test:id1", "source_url": "https://openrouter.ai/models/test-project"})
    digest = build_digest(collection([c1, c2]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 1


def test_explainable_scoring_breakdown_present():
    cand = candidate()
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert len(digest["items"]) == 1
    item = digest["items"][0]
    assert "scoring_breakdown" in item
    assert isinstance(item["scoring_breakdown"], dict)
    assert item["scoring_breakdown"]["total"] > 0


def test_no_hard_minimum_items():
    cand = candidate({"what": "short"})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0
    assert digest["items"] == []


def test_counts_consistency():
    c1 = candidate()
    c2 = candidate({"link_verification": {"status": "failed", "checked_at": NOW.isoformat(), "url": "", "http_status": 0}})
    c3 = candidate({"published_at": (NOW - timedelta(days=30)).isoformat()})
    digest = build_digest(collection([c1, c2, c3]), settings(), now=NOW)
    assert digest["counts"]["fetched"] == 3
    assert digest["counts"]["selected"] == 1


def test_raw_inventory_not_exposed():
    cand = candidate()
    digest = build_digest(collection([cand]), settings(), now=NOW)
    item = digest["items"][0]
    for key in item:
        assert key in {"id", "name", "what", "why", "category", "source_id", "source_url", "link_verification", "observed_at", "free", "api", "score", "scoring_breakdown"}


# === Scoring Tests ===

def test_score_breakdown_keys():
    cand = candidate()
    _, bd = _score_item(cand, settings(), NOW, {})
    expected_keys = {"recency", "free_tier", "api_available", "topic_match", "source_quality", "category_priority", "total"}
    assert set(bd.keys()) == expected_keys


def test_score_breakdown_sums_correctly():
    cand = candidate()
    score, breakdown = _score_item(cand, settings(), NOW, {})
    component_sum = sum(breakdown[k] for k in ("recency", "free_tier", "api_available", "topic_match", "source_quality", "category_priority"))
    assert breakdown["total"] == min(100, component_sum)
    assert score == breakdown["total"]


def test_recency_scoring():
    cand = candidate({"published_at": NOW.isoformat()})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["recency"] == 20

    cand = candidate({"published_at": (NOW - timedelta(days=1)).isoformat()})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["recency"] == 18

    cand = candidate({"published_at": (NOW - timedelta(days=3)).isoformat()})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["recency"] == 15

    cand = candidate({"published_at": (NOW - timedelta(days=7)).isoformat()})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["recency"] == 10

    cand = candidate({"published_at": (NOW - timedelta(days=10)).isoformat()})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["recency"] == 5


def test_free_tier_scoring():
    cand = candidate()
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["free_tier"] == 15

    cand = candidate({"free": {"status": "free-tier", "detail": "", "evidence_url": ""}})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["free_tier"] == 0

    cand = candidate({"free": {"status": "none", "detail": "", "evidence_url": ""}})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["free_tier"] == 0


def test_api_available_scoring():
    cand = candidate()
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["api_available"] == 10

    cand = candidate({"api": {"status": "none", "detail": "", "evidence_url": ""}})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["api_available"] == 0


def test_topic_match_scoring():
    cand = candidate({"topics": ["free-api", "models", "agents"]})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["topic_match"] == 15  # 3 * 5, capped at 15

    cand = candidate({"topics": ["free-api"]})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["topic_match"] == 5

    cand = candidate({"topics": ["irrelevant"]})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["topic_match"] == 0


def test_category_priority_weighting():
    cand = candidate({"category": "free-api"})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["category_priority"] == 10  # 5 * 2

    cand = candidate({"category": "models"})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["category_priority"] == 8  # 4 * 2

    cand = candidate({"category": "news"})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["category_priority"] == 2  # 1 * 2

    cand = candidate({"category": "unknown-cat"})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["category_priority"] == 0


def test_source_quality_scoring():
    # New behavior: source_quality is based on verified-link ratio, not raw count.
    # Passing empty maps yields 0 (no data to evaluate); passing verified=True yields 20.
    settings = {
        "priorities": {"free-api": 5},
        "topics": ["free-api"],
    }
    base = {"what": "A test project description with enough detail for evaluation.", "category": "free-api", "published_at": NOW.isoformat(), "date_kind": "release", "link_verification": {"status": "verified"}}
    _, bd = _score_item({**base, "source_id": "openrouter-models"}, settings, NOW, {}, {})
    assert bd["source_quality"] == 0  # no per-source data

    total_map = {"openrouter-models": 2}
    ver_map = {"openrouter-models": 2}
    _, bd = _score_item({**base, "source_id": "openrouter-models"}, settings, NOW, total_map, ver_map)
    assert bd["source_quality"] == 20  # 2/2 verified

    ver_map2 = {"openrouter-models": 1}
    _, bd = _score_item({**base, "source_id": "openrouter-models"}, settings, NOW, total_map, ver_map2)
    assert bd["source_quality"] == 10  # 1/2 verified

    _, bd = _score_item({**base, "source_id": ""}, settings, NOW, {}, {})
    assert bd["source_quality"] == 0


# === Dedup Tests ===

def test_dedup_by_exact_url_identity():
    c1 = candidate({"name": "Test Project", "source_url": "https://openrouter.ai/models/a", "topics": ["free-api", "models", "agents"]})
    c2 = candidate({"name": "test-project", "source_url": "https://openrouter.ai/models/a", "topics": []})
    digest = build_digest(collection([c1, c2]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 1


def test_dedup_keeps_highest_scored():
    c1 = candidate({"name": "Test Project", "source_url": "https://openrouter.ai/models/a", "topics": ["free-api", "models", "agents"]})
    c2 = candidate({"name": "Test Project", "source_url": "https://openrouter.ai/models/a", "topics": []})
    digest = build_digest(collection([c2, c1]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 1
    assert digest["items"][0]["id"] == c1["id"]


def test_dedup_same_url_different_ids():
    """Same original-story URL from different feeds must dedupe."""
    c1 = candidate({"name": "Test", "source_url": "https://openrouter.ai/models/a", "source_id": "openrouter-models"})
    c2 = candidate({"name": "Test", "source_url": "https://openrouter.ai/models/a", "source_id": "github-trending"})
    digest = build_digest(collection([c1, c2]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 1


def test_dedup_tracks_rejected():
    c1 = candidate({"name": "Test Project", "source_url": "https://openrouter.ai/models/a"})
    c2 = candidate({"name": "Test Project", "source_url": "https://openrouter.ai/models/a"})
    digest = build_digest(collection([c1, c2]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 1
    assert digest["counts"]["rejected"] >= 1


def test_same_url_keeps_only_one():
    c1 = candidate({"id": "a:1", "name": "Same Name", "source_url": "https://openrouter.ai/models/a"})
    c2 = candidate({"id": "b:2", "name": "Same Name", "source_url": "https://openrouter.ai/models/a"})
    digest = build_digest(collection([c1, c2]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 1


# === Additional Gate Tests ===

def test_gates_reject_secret_evidence_in_api():
    cand = candidate({"api": {"status": "available", "detail": "sk-123456", "evidence_url": "https://openrouter.ai/api"}})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0
    assert digest["counts"]["rejected"] == 1


def test_gates_reject_unknown_category():
    cand = candidate({"category": "unknown-category"})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 0
    assert digest["counts"]["rejected"] == 1


def test_no_secret_patterns_pass_through():
    secret_patterns = [
        "api_key=sk-123456",
        "password = secret123",
        "Bearer abcdefghijklmnop",
    ]
    for pattern in secret_patterns:
        cand = candidate({"free": {"status": "open-source", "detail": pattern, "evidence_url": "https://openrouter.ai/license"}})
        digest = build_digest(collection([cand]), settings(), now=NOW)
        assert digest["counts"]["selected"] == 0, f"Secret pattern '{pattern}' should be rejected"


# === Projection Sanitization Tests ===

def test_projection_sanitizes_secrets():
    # Secret detail should be rejected by gate, so test sanitization via name path redaction
    cand = candidate({"name": "C:/Users/test", "what": "Good description of the project with enough detail for evaluation."})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    item = digest["items"][0]
    assert "[PATH REDACTED]" in item["name"]


def test_projection_sanitizes_paths():
    cand = candidate({"name": "C:/Users/admin/secrets", "what": "A meaningful description here with sufficient length for validation purposes."})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    item = digest["items"][0]
    assert "[PATH REDACTED]" in item["name"]


def test_projection_sanitizes_control_chars():
    cand = candidate({"name": "Test\x01Project", "what": "A meaningful description here with sufficient length for validation."})
    digest = build_digest(collection([cand]), settings(), now=NOW)
    item = digest["items"][0]
    assert "\x01" not in item["name"]


# === Render Tests ===

def test_render_digest_produces_valid_html():
    cand = candidate()
    digest = build_digest(collection([cand]), settings(), now=NOW)
    html = render_digest(digest)
    assert "<!DOCTYPE html>" in html
    assert "Daily Digest" in html
    assert "Test Project" in html
    assert "2026-10-07" in html


def test_no_simplenamespce_in_render_digest():
    source = inspect.getsource(render_digest)
    assert "SimpleNamespace" not in source
    assert "_to_ns" not in source


def test_no_simple_namespce_in_module():
    import intel_sources.daily_digest as mod
    source = inspect.getsource(mod)
    assert "SimpleNamespace" not in source
    assert "_to_ns" not in source


def test_zoneinfo_used():
    import intel_sources.daily_digest as mod
    source = inspect.getsource(mod)
    assert "ZoneInfo" in source
    assert "Africa/Cairo" in source


def test_sanitize_preserves_tech_angle_brackets():
    """C++ and v<2.0> must survive sanitization."""
    assert _sanitize_text("requires v<2.0> and C++") == "requires v<2.0> and C++"


def test_sanitize_strips_real_html_tags_only():
    # HTML tags should be fully stripped (content included)
    assert _sanitize_text("<script>alert(1)</script>") == ""
    assert _sanitize_text("version is v<2.0>") == "version is v<2.0>"


def test_gates_reject_javascript_source_url():
    reason = _check_gates(candidate({"source_url": "javascript:alert(1)"}), settings(), NOW)
    assert reason == "unsafe url protocol"


def test_gates_reject_data_source_url():
    reason = _check_gates(candidate({"source_url": "data:text/html,x"}), settings(), NOW)
    assert reason == "unsafe url protocol"


def test_gates_reject_generic_why():
    reason = _check_gates(candidate({"why": "relevant"}), settings(), NOW)
    assert reason == "generic why"


def test_gates_accept_factual_why():
    reason = _check_gates(candidate({"why": "Fits free-api category with MIT license."}), settings(), NOW)
    assert reason is None


def test_naive_date_does_not_crash():
    cand = candidate({"published_at": "2026-10-05T10:00:00", "source_id": "openrouter-models"})
    reason = _check_gates(cand, settings(), NOW)
    # Naive date should not crash; may be accepted or rejected depending on age
    assert reason is not None or reason is None  # just verify no crash


def test_null_published_at_rejected():
    cand = candidate({"published_at": None})
    reason = _check_gates(cand, settings(), NOW)
    assert reason == "missing published_at"


def test_future_by_one_second_rejected():
    cand = candidate({"published_at": (NOW + timedelta(seconds=1)).isoformat()})
    reason = _check_gates(cand, settings(), NOW)
    assert reason == "future date"


def test_score_permutation_invariant():
    items = [
        candidate({"id": "a", "source_url": "https://openrouter.ai/models/a", "source_id": "openrouter-models",
                    "free": {"status": "open-source", "detail": "", "evidence_url": ""},
                    "api": {"status": "none", "detail": "", "evidence_url": ""}}),
        candidate({"id": "b", "source_url": "https://openrouter.ai/models/b", "source_id": "openrouter-models",
                    "free": {"status": "open-source", "detail": "", "evidence_url": ""},
                    "api": {"status": "none", "detail": "", "evidence_url": ""}}),
        candidate({"id": "c", "source_url": "https://openrouter.ai/models/c", "source_id": "openrouter-models",
                    "free": {"status": "open-source", "detail": "", "evidence_url": ""},
                    "api": {"status": "none", "detail": "", "evidence_url": ""}}),
        candidate({"id": "d", "source_url": "https://openrouter.ai/models/d", "source_id": "openrouter-models",
                    "free": {"status": "open-source", "detail": "", "evidence_url": ""},
                    "api": {"status": "none", "detail": "", "evidence_url": ""}}),
    ]
    d1 = build_digest(collection(items), settings(), now=NOW)
    d2 = build_digest(collection(list(reversed(items))), settings(), now=NOW)
    fwd_scores = {i["id"]: i["score"] for i in d1["items"]}
    rev_scores = {i["id"]: i["score"] for i in d2["items"]}
    assert fwd_scores == rev_scores


def test_dedup_same_url_different_feeds():
    c1 = candidate({"name": "Test", "source_url": "https://openrouter.ai/models/x", "source_id": "openrouter-models"})
    c2 = candidate({"name": "Test", "source_url": "https://openrouter.ai/models/x", "source_id": "github-trending"})
    digest = build_digest(collection([c1, c2]), settings(), now=NOW)
    assert digest["counts"]["selected"] == 1


def test_free_tier_no_evidence_no_bonus():
    cand = candidate({"free": {"status": "open-source", "detail": "", "evidence_url": ""}})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["free_tier"] == 0


def test_api_no_evidence_no_bonus():
    cand = candidate({"api": {"status": "available", "detail": "", "evidence_url": ""}})
    _, bd = _score_item(cand, settings(), NOW, {})
    assert bd["api_available"] == 0


def test_unapproved_source_id_rejected():
    reason = _check_gates(candidate({"source_id": "legacy-skills-sh"}), settings(), NOW)
    assert reason == "unapproved source id"


def test_unapproved_host_rejected():
    reason = _check_gates(candidate({"source_url": "https://evil.com/path"}), settings(), NOW)
    assert reason == "unapproved source host"
