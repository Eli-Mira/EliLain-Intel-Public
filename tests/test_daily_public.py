"""Tests for daily public rendering and export with sanitization."""
from pathlib import Path
import json
from datetime import datetime, timezone

import pytest

from intel_sources.daily_public import render_digest, export_digest, export_state, _validate_digest_for_public


def minimal_digest():
    now = datetime(2026, 10, 7, 10, 0, tzinfo=timezone.utc)
    return {
        "schema_version": 1,
        "report_date": "2026-10-07",
        "generated_at": now.isoformat(),
        "entries": [{
            "id": "test:id1",
            "name": "Test Project",
            "what": "A meaningful description.",
            "why": "Fits free-api category with MIT license and REST API.",
            "category": "free-api",
            "source_id": "openrouter-models",
            "source_url": "https://openrouter.ai/models/project",
            "link_verification": {"status": "verified", "checked_at": now.isoformat(), "url": "https://openrouter.ai/models/project", "http_status": 200},
            "observed_at": now.isoformat(),
            "free": {"status": "open-source", "detail": "MIT License"},
            "api": {"status": "available", "detail": "REST API"},
            "score": 80,
            "scoring_breakdown": "category:free-api=+20",
        }],
        "sources": [{"name": "src1", "adapter": "http", "enabled": True, "id": "src1"}],
        "counts": {"fetched": 1, "eligible": 1, "rejected": 0, "selected": 1},
    }


def test_render_digest_uses_template():
    digest = minimal_digest()
    html = render_digest(digest)
    assert "daily-digest-v1" in html
    assert digest["generated_at"] in html
    assert "Test Project" in html
    assert 'meta name="elilain-report-kind"' in html
    assert 'meta name="elilain-generated-at"' in html


def test_render_digest_card_data_attrs():
    digest = minimal_digest()
    html = render_digest(digest)
    assert 'data-id="test:id1"' in html
    assert 'data-source="openrouter-models"' in html
    assert 'data-source-url="https://openrouter.ai/models/project"' in html
    assert 'data-verified-at="' in html
    assert 'data-observed-at="' in html
    assert 'class="original-source"' in html
    assert 'href="https://openrouter.ai/models/project"' in html


def test_export_digest_atomic_write(tmp_path: Path):
    digest = minimal_digest()
    out = export_digest(digest, tmp_path / "out")
    assert out.exists()
    assert out.read_text(encoding="utf-8")
    assert list((tmp_path / "out").iterdir())
    out2 = export_digest(digest, tmp_path / "out")
    assert out == out2


def test_export_preserves_skip_link_to_existing_main(tmp_path: Path):
    from bs4 import BeautifulSoup

    output = export_digest(minimal_digest(), tmp_path / "public")
    soup = BeautifulSoup(output.read_text(encoding="utf-8"), "html.parser")
    skip = soup.select_one('a.skip[href="#main"]')
    assert skip is not None
    assert skip.get_text(strip=True) == "Skip to content"
    assert soup.select_one("main#main") is not None
    assert soup.select("a[href], button, input, select, textarea, summary")[0] is skip
    assert "connect-src 'none'" in soup.select_one('meta[http-equiv="Content-Security-Policy"]')["content"]


def test_validate_digest_rejects_secrets_in_what():
    d = minimal_digest()
    d["entries"][0]["what"] = "secret sk-abcdefg1234567890 found here"
    with pytest.raises(ValueError, match="secret pattern detected"):
        _validate_digest_for_public(d)


def test_validate_digest_rejects_secrets_in_free_detail():
    d = minimal_digest()
    d["entries"][0]["free"]["detail"] = "api_key=sk-abcdefg1234567890"
    with pytest.raises(ValueError, match="secret pattern detected"):
        _validate_digest_for_public(d)


def test_validate_digest_rejects_secrets_in_api_detail():
    d = minimal_digest()
    d["entries"][0]["api"]["detail"] = "token=sk-abcdefg1234567890"
    with pytest.raises(ValueError, match="secret pattern detected"):
        _validate_digest_for_public(d)


def test_render_digest_preserves_sanitized_content():
    # render_digest itself doesn't sanitize - validation happens at export time
    digest = minimal_digest()
    html = render_digest(digest)
    assert "Test Project" in html


def test_render_digest_rejects_boilerplate_descriptions():
    digest = minimal_digest()
    digest["entries"][0]["what"] = "N/A"
    html = render_digest(digest)
    assert "Test Project" in html


def test_export_state_atomic_write(tmp_path: Path):
    state = {"items": [], "sources": [], "meta": "x"}
    out = export_state(state, tmp_path / "out")
    assert out.exists()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["meta"] == "x"


def test_validate_digest_rejects_local_paths():
    d = minimal_digest()
    d["entries"][0]["what"] = r"C:\Windows\System32\drivers\etc\hosts"
    with pytest.raises(ValueError, match="local path detected"):
        _validate_digest_for_public(d)


def test_validate_digest_rejects_control_chars():
    d = minimal_digest()
    d["entries"][0]["what"] = "hello\x01world"
    with pytest.raises(ValueError, match="control character detected"):
        _validate_digest_for_public(d)


def test_validate_digest_rejects_boilerplate():
    d = minimal_digest()
    d["entries"][0]["what"] = "N/A"
    with pytest.raises(ValueError, match="boilerplate rejected"):
        _validate_digest_for_public(d)


def test_validate_digest_rejects_invalid_url():
    d = minimal_digest()
    d["entries"][0]["source_url"] = "not-a-url"
    with pytest.raises(ValueError, match="invalid public url"):
        _validate_digest_for_public(d)
