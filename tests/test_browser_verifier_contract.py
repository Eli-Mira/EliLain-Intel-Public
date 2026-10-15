"""Regression: verify_project_ui_browser must pass both dashboard and daily-digest artifacts."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'verify_project_ui_browser.py'

playwright = pytest.importorskip('playwright.sync_api')


@pytest.fixture(scope='module', autouse=True)
def require_browser():
    with playwright.sync_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).is_file():
            pytest.skip('Playwright Chromium is not installed')


def _run_html(html_path: Path) -> list[dict]:
    """Execute the standalone browser verifier on a single index.html; return parsed reports."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(html_path)],
        capture_output=True, text=True,
        cwd=str(ROOT), timeout=120,
    )
    assert result.returncode == 0, (
        f'verify_project_ui_browser exited {result.returncode}\n'
        f'STDOUT: {result.stdout}\nSTDERR: {result.stderr}'
    )
    return json.loads(result.stdout)


def test_daily_digest_artifact_passes_browser_verifier(tmp_path):
    """Freshly-generated daily digest must run without the original dashboard-only assumptions."""
    from intel_sources.daily_public import export_digest
    from test_daily_public import minimal_digest

    reports = _run_html(export_digest(minimal_digest(), tmp_path / 'public'))
    widths = {r['width'] for r in reports}
    js_values = {r['javascript'] for r in reports}
    themes = {r['system_theme'] for r in reports}
    assert widths == {360, 390, 1440}
    assert js_values == {True, False}
    assert themes == {'dark', 'light'}
    assert len(reports) == 3 * 2 * 2
    for r in reports:
        assert r['discovery_rows'] == 1
        assert r['sorting_verified'] is False
        assert r['no_horizontal_overflow'] is True
        assert r['zoom_200_percent'] is True
        assert r['network_requests'] == 0
        assert r['page_errors'] == []
        assert r['console_errors'] == []


def test_dashboard_artifact_still_passes_browser_verifier(tmp_path):
    """The existing dashboard/catalog contract must remain green with its richer assertions."""
    from intel_sources.public_report import build_public_payload
    from tracker_core.reporter_html import LainReporter

    state = {
        'sources': [],
        'items': [
            {
                'source': 'github-trending',
                'id': 'github:owner/tool',
                'url': 'https://github.com/owner/tool',
                'headline': 'Tool',
                'checked_at': '2026-10-03T12:00:00Z',
                'first_seen': '2026-09-30T12:00:00Z',
                'project_types': ['tools'],
                'evidence': {'total_stars': 10},
            },
            {
                'source': 'skills-sh',
                'id': 'skills:author/repo/skill',
                'url': 'https://skills.sh/author/repo/skill',
                'headline': 'Skill',
                'checked_at': '2026-10-03T12:00:00Z',
                'first_seen': '2026-09-30T12:00:00Z',
                'project_types': ['tools'],
                'evidence': {},
            },
        ],
    }
    payload = build_public_payload(state)
    html = LainReporter().render(**payload, public_export=True)
    out_dir = tmp_path / 'public'
    out_dir.mkdir()
    (out_dir / 'index.html').write_text(html, encoding='utf-8')
    reports = _run_html(out_dir / 'index.html')
    widths = {r['width'] for r in reports}
    js_values = {r['javascript'] for r in reports}
    themes = {r['system_theme'] for r in reports}
    assert widths == {360, 390, 1440}
    assert js_values == {True, False}
    assert themes == {'dark', 'light'}
    assert len(reports) == 3 * 2 * 2
    for r in reports:
        assert r['discovery_rows'] == 2
        assert r['no_horizontal_overflow'] is True
        assert r['zoom_200_percent'] is True
        assert r['network_requests'] == 0
        assert r['page_errors'] == []
        assert r['console_errors'] == []
    # sorting_verified is True only when JS is enabled (dashboard contract)
    for r in reports:
        if r['javascript']:
            assert r['sorting_verified'] is True
        else:
            assert r['sorting_verified'] is False
