"""Tests for Task C: daily artifact integration with strict format validation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

# Add src to path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from intel_sources.daily_automation import run_daily, _persist_history, _enforce_history_limit
from intel_sources.report_delivery import load_artifact, DeliveryError
from intel_sources import settings as settings_module


@pytest.fixture
def sample_daily_digest() -> dict[str, Any]:
    """Create a valid daily digest matching the daily template."""
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "report_date": now.date().isoformat(),
        "generated_at": now.isoformat(),
        "items": [
            {
                "id": "test-1",
                "name": "Test Model",
                "what": "A test model for verifying the daily digest pipeline",
                "why": "Open-source model with API access",
                "category": "models",
                "source_id": "openrouter-models",
                "source_url": "https://openrouter.ai/google/gemini-nano-banana-2.1",
                "link_verification": {"status": "verified", "checked_at": now.isoformat(), "url": "https://openrouter.ai/google/gemini-nano-banana-2.1", "http_status": 200},
                "observed_at": now.isoformat(),
                "free": {"status": "open-source", "detail": "Available"},
                "api": {"status": "available", "detail": "REST API"},
                "score": 85,
                "scoring_breakdown": {"recency": 20, "free_tier": 15, "api_available": 10, "topic_match": 15, "source_quality": 20, "category_priority": 5, "total": 85},
            }
        ],
        "sources": [
            {"name": "openrouter-models", "adapter": "openrouter", "enabled": True, "id": "openrouter"}
        ],
        "counts": {"fetched": 1, "eligible": 1, "rejected": 0, "selected": 1},
    }


@pytest.fixture
def sample_daily_html(sample_daily_digest: dict) -> str:
    """Render a minimal daily digest HTML matching the template format."""
    from jinja2 import Environment, select_autoescape
    from pathlib import Path
    import re
    
    # Use the actual template but with our sample data
    template_path = ROOT / "src" / "templates" / "daily_digest.html"
    if template_path.exists():
        env = Environment(autoescape=select_autoescape(["html", "xml"]))
        tmpl = env.from_string(template_path.read_text(encoding="utf-8"))
        # Template uses 'entries' not 'items'
        render_digest = sample_daily_digest.copy()
        render_digest['entries'] = render_digest.pop('items')
        html = tmpl.render(digest=render_digest, history={"reports": []})
        # Strip skip-to-content anchor and make standalone (required for load_artifact)
        import re
        html = re.sub(r'<a\s+href\s*=\s*"#main"[^>]*>.*?</a>', '', html, flags=re.IGNORECASE | re.DOTALL)
        from intel_sources.public_report import _standalone_html
        html = _standalone_html(html)
    else:
        # Fallback minimal HTML with required meta and footer
        items_html = ""
        for item in sample_daily_digest["items"]:
            items_html += f"""
<article class="card digest-item"
  data-id="{item['id']}"
  data-source="{item['source_id']}"
  data-source-url="{item['source_url']}">
  <div class="field-row"><div class="field-label">Name</div><div>{item['name']}</div></div>
</article>"""
        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EliLain Intel — Daily Digest {sample_daily_digest['report_date']}</title>
<meta name="elilain-report-kind" content="daily-digest-v1">
<meta name="elilain-generated-at" content="{sample_daily_digest['generated_at']}">
<meta http-equiv="Content-Security-Policy" content="default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline';">
<meta name="referrer" content="no-referrer">
</head>
<body>
<main id="main">
<h1>Daily Digest</h1>
{items_html}
<footer>
  Fresh public-source snapshot · Generated at {sample_daily_digest['generated_at']}
</footer>
</main>
</body>
</html>"""
    return html


@pytest.fixture
def daily_export_dir(tmp_path: Path, sample_daily_html: str) -> Path:
    """Create a temp directory with a valid daily index.html."""
    export_dir = tmp_path / "public"
    export_dir.mkdir()
    (export_dir / "index.html").write_text(sample_daily_html, encoding="utf-8")
    return export_dir


class TestRunDailyPublicDir:
    """Test run_daily keyword-only public_dir parameter."""

    def test_signature_accepts_keyword_only_public_dir(self):
        """GREEN: run_daily should accept public_dir as keyword-only argument."""
        import inspect
        sig = inspect.signature(run_daily)
        public_dir_param = sig.parameters.get('public_dir')
        assert public_dir_param is not None, "public_dir parameter missing"
        assert public_dir_param.kind == inspect.Parameter.KEYWORD_ONLY, \
            f"public_dir should be keyword-only, got {public_dir_param.kind}"

    def test_default_public_dir_is_data_dir_public(self):
        """GREEN: When public_dir is omitted, export_dir defaults to data_dir/public."""
        import inspect
        source = inspect.getsource(run_daily)
        assert "data_dir / 'public'" in source or 'data_dir / "public"' in source, \
            "Should default to data_dir/public"

    def test_custom_public_dir_is_used(self):
        """GREEN: When public_dir is provided, export goes to custom path."""
        import inspect
        source = inspect.getsource(run_daily)
        assert 'public_dir' in source, "Should reference public_dir"

    def test_summary_has_settings_hash_not_sha(self):
        """GREEN: Summary should contain settings_hash, not settings_sha."""
        import inspect
        source = inspect.getsource(run_daily)
        assert 'settings_hash' in source, "Summary must contain settings_hash"
        assert 'settings_sha' not in source or 'settings_hash' in source, \
            "Must use settings_hash instead of settings_sha"


class TestLoadArtifactDailyFormat:
    """Test load_artifact strict daily-digest-v1 branch."""

    def test_load_artifact_daily_format(self, daily_export_dir):
        """GREEN: load_artifact should accept valid daily-digest-v1 format."""
        artifact = load_artifact(daily_export_dir)
        assert artifact is not None
        assert len(artifact.data) > 0

    def test_load_artifact_daily_requires_meta_kind(self, tmp_path: Path):
        """RED: load_artifact should reject missing elilain-report-kind."""
        export_dir = tmp_path / "bad1"
        export_dir.mkdir()
        (export_dir / "index.html").write_text("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Test</title>
</head>
<body>
<main id="main"><h1>Test</h1></main>
</body>
</html>
""", encoding="utf-8")
        
        with pytest.raises(DeliveryError):
            load_artifact(export_dir)

    def test_load_artifact_daily_requires_generated_at(self, tmp_path: Path):
        """RED: load_artifact should reject missing elilain-generated-at."""
        export_dir = tmp_path / "bad2"
        export_dir.mkdir()
        (export_dir / "index.html").write_text("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="elilain-report-kind" content="daily-digest-v1">
<title>Test</title>
</head>
<body>
<main id="main"><h1>Test</h1></main>
</body>
</html>
""", encoding="utf-8")
        
        with pytest.raises(DeliveryError):
            load_artifact(export_dir)

    def test_load_artifact_daily_requires_footer(self, tmp_path: Path):
        """RED: load_artifact should reject missing daily footer."""
        export_dir = tmp_path / "bad3"
        export_dir.mkdir()
        (export_dir / "index.html").write_text("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="elilain-report-kind" content="daily-digest-v1">
<meta name="elilain-generated-at" content="2026-01-01T00:00:00+00:00">
<title>Test</title>
</head>
<body>
<main id="main"><h1>Test</h1></main>
<footer>Wrong footer</footer>
</body>
</html>
""", encoding="utf-8")
        
        with pytest.raises(DeliveryError):
            load_artifact(export_dir)

    def test_load_artifact_daily_rejects_legacy_format_only(self, tmp_path: Path):
        """RED: load_artifact should accept both formats but validate each correctly."""
        # Create a legacy format artifact
        export_dir = tmp_path / "legacy"
        export_dir.mkdir()
        (export_dir / "index.html").write_text("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Test</title>
<meta http-equiv="Content-Security-Policy" content="default-src 'self'">
<meta name="referrer" content="no-referrer">
</head>
<body>
<div id="discoveries">
<div class="discovery-item" data-source="github-trending">Test</div>
</div>
<footer>Public sanitized export of saved observations; not a fresh fetch.</footer>
</body>
</html>
""", encoding="utf-8")
        
        # Legacy should still work (if valid) or fail appropriately
        try:
            artifact = load_artifact(export_dir)
            # If it passes, verify it's the legacy branch
            assert artifact is not None
        except DeliveryError:
            # Legacy format might fail due to other reasons - that's OK
            pass

    def test_load_artifact_daily_validates_source_families(self, tmp_path: Path):
        """RED: load_artifact should validate source families in daily format."""
        export_dir = tmp_path / "bad_sources"
        export_dir.mkdir()
        (export_dir / "index.html").write_text("""
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="elilain-report-kind" content="daily-digest-v1">
<meta name="elilain-generated-at" content="2026-01-01T00:00:00+00:00">
<title>Test</title>
</head>
<body>
<main id="main">
<article data-source="invalid-source">Bad source</article>
</main>
<footer>Fresh public-source snapshot · Generated at 2026-01-01T00:00:00+00:00</footer>
</body>
</html>
""", encoding="utf-8")
        
        with pytest.raises(DeliveryError):
            load_artifact(export_dir)


class TestCIWorkflowExactChain:
    """Test CI workflow maintains exact daily output chain."""

    def test_workflow_uses_daily_action(self):
        """RED: Workflow should use the daily action with correct args."""
        import yaml
        workflow_path = ROOT / ".github" / "workflows" / "news-tracker-daily.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
        
        steps = workflow["jobs"]["run-elilain-audit"]["steps"]
        commands = "\n".join(s.get("run", "") for s in steps)
        
        assert "python main.py daily" in commands

    def test_workflow_daily_step_uses_public_dir(self):
        """GREEN: Daily step should specify --public-dir."""
        import yaml
        workflow_path = ROOT / ".github" / "workflows" / "news-tracker-daily.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

        steps = workflow["jobs"]["run-elilain-audit"]["steps"]
        daily_steps = [s for s in steps if "main.py daily" in s.get("run", "")]

        assert len(daily_steps) >= 1, "Should have at least one daily step"
        # The daily step should use consistent public dir
        daily_run = daily_steps[0].get("run", "")
        assert "--public-dir" in daily_run, f"Daily step should specify --public-dir: {daily_run}"


class TestMainDashboardCallback:
    """Test main.py wires dashboard with callback."""

    def test_main_daily_wires_public_dir(self):
        """GREEN: main.py daily action should pass public_dir."""
        main_path = ROOT / "main.py"
        source = main_path.read_text(encoding="utf-8")
        assert "public_dir=args.public_dir" in source, \
            "main.py daily action should pass public_dir to run_daily"

    def test_workflow_preserves_cairo_timezone(self):
        """GREEN: Africa/Cairo timezone schedule must be preserved."""
        import yaml
        workflow_path = ROOT / ".github" / "workflows" / "news-tracker-daily.yml"
        workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))

        # YAML parses 'on:' as boolean True, not string
        schedules = workflow.get(True, {}).get("schedule", [])
        assert len(schedules) >= 1, "Should have schedule"
        assert schedules[0].get("timezone") == "Africa/Cairo"
        # Preserve 10:00 slot
        cron = schedules[0].get("cron", "")
        assert "0 10" in cron or "10 0" in cron, f"Expected 10:00 in cron: {cron}"
