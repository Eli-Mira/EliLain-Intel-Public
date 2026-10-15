"""Tests for daily_digest.html and intel_cockpit.html templates."""
from pathlib import Path
import re

import pytest
from jinja2 import Template

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / 'src' / 'templates'


def _load(name: str) -> str:
    p = TEMPLATES / name
    assert p.exists(), f'missing template {p}'
    return p.read_text(encoding='utf-8')


@pytest.mark.parametrize('name', ['daily_digest.html', 'intel_cockpit.html'])
def test_meta_tags(name: str):
    html = _load(name)
    assert 'elilain-report-kind' in html or 'Content-Security-Policy' in html
    assert '<meta charset="UTF-8">' in html


def test_digest_has_viewport():
    html = _load('daily_digest.html')
    assert 'viewport' in html
    assert 'width=device-width' in html


def test_cockpit_has_viewport():
    html = _load('intel_cockpit.html')
    assert 'viewport' in html
    assert 'width=device-width' in html


def test_digest_section_anchors():
    html = _load('daily_digest.html')
    for anchor in ('#daily-digest', '#discoveries', '#source-health', '#public-scope', '#report-history'):
        assert f'id="{anchor.lstrip(chr(35))}"' in html, f'missing anchor {anchor}'


def test_digest_card_data_attrs_rendered():
    tmpl_source = _load('daily_digest.html')
    now = '2026-10-07T10:00:00+00:00'
    tmpl = Template(tmpl_source)
    
    # Use SimpleNamespace to match render_digest behavior
    from types import SimpleNamespace
    def _to_ns(d):
        ns = SimpleNamespace()
        for k, v in d.items():
            if isinstance(v, dict):
                setattr(ns, k, _to_ns(v))
            elif isinstance(v, list):
                setattr(ns, k, v)
            else:
                setattr(ns, k, v)
        return ns
    
    digest = {
        'report_date': '2026-10-07',
        'generated_at': now,
        'entries': [{
            'id': 'test:id1',
            'name': 'Proj',
            'what': 'desc',
            'why': 'relevant',
            'category': 'models',
            'source_id': 's1',
            'source_url': 'https://example.com/p',
            'link_verification': {'status': 'verified', 'checked_at': now, 'url': 'https://example.com/p', 'http_status': 200},
            'observed_at': now,
            'free': {'status': 'open-source', 'detail': 'MIT License'},
            'api': {'status': 'available', 'detail': 'REST API'},
            'score': 80,
            'scoring_breakdown': 'base+free+api',
        }],
        'sources': [{'name': 's1', 'adapter': 'http', 'enabled': True, 'id': 's1'}],
        'counts': {'fetched': 1, 'eligible': 1, 'rejected': 0, 'selected': 1},
    }
    history = {'reports': []}
    
    html = tmpl.render(digest=_to_ns(digest), history=history)
    assert 'data-id="test:id1"' in html
    assert 'data-source="s1"' in html
    assert 'data-source-url="https://example.com/p"' in html
    assert 'data-verified-at="' in html
    assert 'data-observed-at="' in html


def test_original_link_class_present():
    html = _load('daily_digest.html')
    assert 'original-source' in html


def test_focus_visible_styles():
    html = _load('daily_digest.html')
    assert ':focus-visible' in html


def test_reduced_motion():
    html = _load('daily_digest.html')
    assert 'prefers-reduced-motion' in html


def test_no_external_scripts():
    html = _load('daily_digest.html')
    assert 'script src=' not in html
    assert 'src="http' not in html


def test_no_external_css():
    html = _load('daily_digest.html')
    assert 'link rel="stylesheet"' not in html
    assert '@import' not in html
    assert '<style>' in html


def test_inline_script_only():
    html = _load('daily_digest.html')
    assert '<script>' in html
    assert '</script>' in html


def test_noscript_fallback():
    html = _load('daily_digest.html')
    assert '<noscript>' in html or 'noscript' in html


def test_loading_error_empty_states():
    html = _load('daily_digest.html')
    for cls in ('loading', 'error', 'empty'):
        assert cls in html, f'missing state class {cls}'


def test_no_dead_ui_hrefs():
    html = _load('daily_digest.html')
    dead_pattern = r'href\s*=\s*"#"(?![a-zA-Z])'
    matches = re.findall(dead_pattern, html)
    assert len(matches) == 0, 'dead UI anchors found'


def test_cockpit_api_endpoints():
    html = _load('intel_cockpit.html')
    for ep in ('/api/settings', '/api/status', '/api/scan', '/api/history'):
        assert ep in html, f'missing endpoint {ep}'


def test_cockpit_csrf():
    html = _load('intel_cockpit.html')
    assert 'csrf_token' in html or 'CSRF' in html
