"""M6 tests: canonical identity, corroboration grouping, novelty.

One launch corroborated by several publications must present as one finding
with all provenance preserved; different versions, different tools and
different entities must never merge; missing dates are UNKNOWN_FRESHNESS.
"""
from intel_sources.evidence import canonical_url, entity_key, group_findings, novelty


def _cand(cid, url, source_id, kind, published=None, version=None, title=None):
    cand = {
        "id": cid, "name": title or cid, "source_id": source_id,
        "source_url": url, "evidence_kind": kind, "published_at": published,
    }
    if version:
        cand["version"] = version
    return cand


def test_canonical_url_strips_tracking_and_fragments():
    assert canonical_url(
        "https://Example.com/Path/?utm_source=x&fbclid=y&keep=1#frag"
    ) == "https://example.com/Path?keep=1"
    assert canonical_url("https://example.com/a/") == "https://example.com/a"
    assert canonical_url("http://example.com:80/x") == "http://example.com/x"
    assert canonical_url("not a url") == ""


def test_entity_key_prefers_repo_and_arxiv_identity():
    assert entity_key(_cand("a", "https://github.com/Owner/Repo/releases/v1",
                            "github-releases-tracked", "repo_releases", version="v1")) \
        == "repo:owner/repo@v1"
    assert entity_key(_cand("a", "https://github.com/Owner/Repo",
                            "github-trending", "fixture_or_reference")) == "repo:owner/repo"
    assert entity_key(_cand("a", "https://arxiv.org/abs/2610.12345v2",
                            "arxiv-cs-ai", "official_feed")) == "arxiv:2610.12345"
    assert entity_key(_cand("a", "https://example.com/story?utm_source=x",
                            "official-openai-news", "official_feed")) \
        == "https://example.com/story"


def test_cross_source_corroboration_merges_with_all_provenance():
    release = _cand("r1", "https://github.com/ollama/ollama/releases/tag/v9",
                    "github-releases-tracked", "repo_releases", "2026-10-25T00:00:00Z")
    reference = _cand("r2", "https://github.com/ollama/ollama",
                      "replace-so-reference", "manual_reference", "2026-10-20T00:00:00Z")
    editorial = _cand("r3", "https://simonwillison.net/2026/Oct/25/ollama/",
                      "simonwillison-atom", "editorial_feed", "2026-10-25T01:00:00Z")
    findings = group_findings([release, reference, editorial])
    # release + reference share the repo entity; editorial is its own story
    assert len(findings) == 2
    repo = next(f for f in findings if f["entity_key"] == "repo:ollama/ollama")
    assert repo["id"] == "r1"  # repo_releases outranks manual_reference
    assert len(repo["corroborations"]) == 1  # reference corroborates
    assert repo["corroborations"][0]["source_id"] == "replace-so-reference"
    editorial_f = next(f for f in findings if f["entity_key"].startswith("https://simonwillison"))
    assert editorial_f["corroborations"] == []


def test_same_name_different_tools_never_merge():
    a = _cand("a", "https://github.com/ownerA/tool", "s1", "repo_releases")
    b = _cand("b", "https://github.com/ownerB/tool", "s2", "repo_releases")
    findings = group_findings([a, b])
    assert len(findings) == 2


def test_same_entity_different_versions_are_separate_events():
    v1 = _cand("v1", "https://github.com/o/r/releases/v1", "s1", "repo_releases", version="v1")
    v2 = _cand("v2", "https://github.com/o/r/releases/v2", "s1", "repo_releases", version="v2")
    findings = group_findings([v1, v2])
    assert len(findings) == 2
    assert {f["entity_key"] for f in findings} == {"repo:o/r@v1", "repo:o/r@v2"}


def test_paraphrased_articles_with_same_canonical_url_merge():
    a = _cand("a", "https://news.example.com/story?utm_campaign=x", "s1", "editorial_feed")
    b = _cand("b", "https://news.example.com/story", "s2", "editorial_feed")
    findings = group_findings([a, b])
    assert len(findings) == 1
    assert len(findings[0]["corroborations"]) == 1


def test_grouping_is_order_independent():
    cands = [
        _cand("r1", "https://github.com/o/r", "s1", "repo_releases", "2026-10-25T00:00:00Z"),
        _cand("r2", "https://github.com/o/r", "s2", "manual_reference"),
        _cand("n1", "https://example.com/n", "s3", "editorial_feed"),
    ]
    forward = group_findings(cands)
    backward = group_findings(list(reversed(cands)))
    assert [(f["entity_key"], f["id"]) for f in forward] == \
           [(f["entity_key"], f["id"]) for f in backward]


def test_missing_dates_are_unknown_freshness_not_just_launched():
    undated = _cand("u", "https://example.com/no-date", "s1", "official_feed")
    findings = group_findings([undated])
    assert findings[0]["freshness"] == "UNKNOWN_FRESHNESS"
    assert findings[0]["first_published_at"] is None


def test_novelty_marks_first_seen_and_repeat_against_baseline():
    cands = [_cand("a", "https://github.com/o/r", "s1", "repo_releases"),
             _cand("b", "https://example.com/new", "s2", "editorial_feed")]
    findings = group_findings(cands)
    baseline = {"repo:o/r"}  # seen in a prior run
    marked = novelty(findings, baseline)
    marks = {f["entity_key"]: f["novelty"] for f in marked}
    assert marks["repo:o/r"] == "repeat"
    assert marks["https://example.com/new"] == "first_seen"
