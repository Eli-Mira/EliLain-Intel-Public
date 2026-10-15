"""RED tests: reproduce actual pre-repair bugs before fixes."""
from datetime import datetime, timezone, timedelta
import pytest

from intel_sources.daily_digest import _sanitize_text, _check_gates, build_digest, _score_item

NOW = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)

def settings():
    return {
        "schema_version": 1,
        "sources": [],
        "categories": ["free-api", "models", "agents", "mcp", "skills", "devtools", "news"],
        "max_items": 5, "max_age_days": 7, "min_score": 60,
        "priorities": {"free-api": 5, "models": 4, "agents": 3, "mcp": 2, "skills": 2, "devtools": 1, "news": 1},
        "topics": ["free-api", "models", "agents"],
    }

def candidate(base=None):
    base = base or {}
    return {
        "id": "test:id1", "name": "Test Project",
        "what": "A meaningful description of the project with enough detail to be useful to the user.",
        "category": "free-api", "source_id": "openrouter-models",
        "source_url": "https://openrouter.ai/models/project",
        "published_at": (NOW - timedelta(days=2)).isoformat(), "date_kind": "release",
        "observed_at": NOW.isoformat(),
        "free": {"status": "open-source", "detail": "MIT", "evidence_url": "https://openrouter.ai/license"},
        "api": {"status": "available", "detail": "REST", "evidence_url": "https://openrouter.ai/api"},
        "topics": ["free-api"],
        "link_verification": {"status": "verified", "checked_at": NOW.isoformat(), "url": "https://openrouter.ai/models/project", "http_status": 200},
        "why": "Fits free-api category with MIT license and REST API.",
        **base,
    }

def collection(cands):
    return {"candidates": cands, "sources": []}


# BUG 1: sanitize strips v<2.0> and angle-bracket tech text
class TestSanitizeTechText:
    def test_v2_stripped_bug(self):
        assert _sanitize_text("requires v<2.0> and C++") == "requires v<2.0> and C++"

    def test_cpp_untouched(self):
        assert _sanitize_text("C++") == "C++"

    def test_real_html_stripped(self):
        assert _sanitize_text("<script>alert(1)</script>") == ""


# BUG 2: javascript: source_url passes gates
class TestProtocolGates:
    def test_javascript_url_rejected(self):
        assert _check_gates(candidate({"source_url": "javascript:alert(1)"}), settings(), NOW) is not None

    def test_data_url_rejected(self):
        assert _check_gates(candidate({"source_url": "data:text/html,x"}), settings(), NOW) is not None


# BUG 3: generic 'relevant' why passes gates
class TestMeaningfulWhy:
    def test_generic_why_rejected(self):
        assert _check_gates(candidate({"why": "relevant"}), settings(), NOW) is not None

    def test_placeholder_generic_rejected(self):
        assert _check_gates(candidate({"why": "placeholder"}), settings(), NOW) is not None

    def test_factual_why_accepted(self):
        assert _check_gates(candidate({"why": "Fits free-api category with MIT license."}), settings(), NOW) is None


# BUG 4: naive/offsetless datetime causes TypeError
class TestNaiveDateNoCrash:
    def test_naive_no_offset_does_not_crash(self):
        try:
            _check_gates(candidate({"published_at": "2026-10-05T10:00:00"}), settings(), NOW)
        except TypeError:
            pytest.fail("Naive datetime must not raise TypeError")

    def test_null_published_at(self):
        assert _check_gates(candidate({"published_at": None}), settings(), NOW) == "missing published_at"

    def test_future_by_one_second_rejected(self):
        assert _check_gates(candidate({"published_at": (NOW + timedelta(seconds=1)).isoformat()}), settings(), NOW) == "future date"


# BUG 5: score depends on input order (source_counts mutated during iteration)
class TestPermutationInvariant:
    def test_reverse_order_same_scores(self):
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
