"""Customer dashboard acceptance: real projection and offline file Chromium."""
from datetime import datetime, timezone
import json
from bs4 import BeautifulSoup

from intel_sources.public_report import build_public_payload, export_public_report
from tracker_core.reporter_html import LainReporter

NOW = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)


def state():
    return {'sources': [], 'items': [
        {'source': 'github-trending', 'id': 'github:owner/tool',
         'url': 'https://github.com/owner/tool', 'headline': 'Tool',
         'checked_at': '2026-10-01T12:00:00Z', 'first_seen': '2026-09-30T12:00:00Z',
         'project_types': ['tools'], 'evidence': {'total_stars': 10}},
        {'source': 'skills-sh', 'id': 'skills:author/repo/skill',
         'url': 'https://skills.sh/author/repo/skill', 'headline': 'Skill',
         'project_types': ['tools'], 'evidence': {}}]}


def artifact(tmp_path, saved=None):
    path = tmp_path / 'state.json'
    path.write_text(json.dumps(saved or state()), encoding='utf-8')
    return export_public_report(path, tmp_path / 'missing.db', tmp_path / 'public', now=NOW)


def test_public_never_evaluates_or_renders_personal_project_groups(monkeypatch):
    from intel_sources import recommendations
    def forbidden(*args, **kwargs):
        raise AssertionError('Public export called personal project evaluator')
    monkeypatch.setattr(recommendations, 'evaluate_projects', forbidden)
    payload = build_public_payload(state(), now=NOW)
    assert 'recommendation_groups' not in payload
    assert 'recommendation_coverage' not in payload
    assert len(payload['tool_items']) == 2
    html = LainReporter().render(**payload, public_export=True,
        recommendation_groups={'private': {'project': 'Desktop application PRIVATE_PROJECT_CANARY'}})
    assert 'PRIVATE_PROJECT_CANARY' not in html
    assert 'project-shortlists' not in html
    assert 'skills:author/repo/skill' in html
    soup = BeautifulSoup(html, 'html.parser')
    assert soup.select('section.tracker')[0]['id'] == 'discoveries'
    assert 'Provider observations excluded' in soup.text
    assert 'News excluded' in soup.text


def test_public_renderer_ignores_private_recommendation_coverage():
    reporter = LainReporter()
    args = {'api_items': [], 'public_export': True, 'report_date': '2026-10-03',
            'generated_at': '2026-10-03T12:00:00Z'}
    clean = reporter.render(**args)
    forged = reporter.render(**args, recommendation_coverage={'inventory_count': 'PRIVATE_COVERAGE_CANARY'})
    assert forged == clean
    assert 'PRIVATE_COVERAGE_CANARY' not in forged


def test_chosen_avatar_is_exact_bounded_embedded_art_not_a_general_image_allowlist(tmp_path):
    import base64
    import hashlib
    import pytest
    from intel_sources.public_report import _standalone_html
    html = artifact(tmp_path).read_text(encoding='utf-8')
    soup = BeautifulSoup(html, 'html.parser')
    images = soup.select('img')
    assert len(images) == 1, 'chosen avatar is absent'
    image = images[0]
    assert image['alt'] == 'Lain portrait — Serial Experiments Lain'
    prefix = 'data:image/jpeg;base64,'
    assert image['src'].startswith(prefix)
    raw = base64.b64decode(image['src'][len(prefix):], validate=True)
    assert len(raw) == 74801
    assert hashlib.sha256(raw).hexdigest() == '4f928317acb16f4ab30488a83d34410ce78c884797fef2c00dfcada3163f6135'
    assert 'Third-party artwork' in soup.footer.text
    assert 'not covered by the code' in soup.footer.text
    assert "img-src data:" in html
    assert 'C:\\Users' not in html
    for forged in ['<img src="data:image/jpeg;base64,eA==">',
                   '<img src="https://example.org/avatar.jpg">',
                   '<img src="data:image/svg+xml;base64,PHN2Zz4=">']:
        with pytest.raises(ValueError, match='dependency'):
            _standalone_html(html.replace('</body>', forged + '</body>'))
    with pytest.raises(ValueError, match='dependency'):
        _standalone_html(html.replace('alt="Lain', 'srcset="https://example.org/x" alt="Lain'))


import os
import pytest
from playwright.sync_api import sync_playwright, expect


@pytest.fixture(scope='module')
def dashboard_browser():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True,
            executable_path=os.environ.get('PLAYWRIGHT_EXECUTABLE_PATH'))
        yield browser
        browser.close()


@pytest.mark.parametrize('width', [360, 390, 1440])
def test_readable_explore_surface_responsive_no_js_and_zoom(tmp_path, dashboard_browser, width):
    path = artifact(tmp_path)
    context = dashboard_browser.new_context(offline=True, java_script_enabled=False,
        viewport={'width': width, 'height': 900})
    try:
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(path.as_uri())
        assert page.evaluate('scrollY') == 0
        assert page.locator('#discoveries').bounding_box()['y'] < 300
        assert page.locator('body').evaluate('x => getComputedStyle(x).fontSize') == '17px'
        assert 'Segoe UI' in page.locator('body').evaluate('x => getComputedStyle(x).fontFamily')
        assert page.locator('h1').evaluate('x => getComputedStyle(x).fontFamily') != page.locator('body').evaluate('x => getComputedStyle(x).fontFamily')
        assert page.locator('nav[aria-label="Dashboard sections"]').is_visible()
        assert page.locator('#discovery-search').is_disabled()
        page.locator('#discovery-inventory > summary').focus()
        page.keyboard.press('Enter')
        assert page.locator('.discovery-item:visible').count() == 2
        assert page.locator('.item-desc').first.evaluate('x => getComputedStyle(x).fontSize') == '17px'
        assert page.locator('.portrait img').evaluate('x => x.complete && x.naturalWidth > 0')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.evaluate('document.body.style.zoom = "200%"')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors
    finally:
        context.close()


def popularity_state():
    saved = state()
    prototype = saved['items'][0]
    saved['items'] = [{**prototype, 'id': 'github:owner/' + name,
        'headline': name, 'url': 'https://github.com/owner/' + name,
        'evidence': {'total_stars': value, 'description': 'PRIVATE_METRIC_CANARY'}}
        for name, value in [('a-missing', True), ('b-high', 100), ('c-low', 10), ('d-tie', 100)]]
    saved['items'] += [{**state()['items'][1], 'id': 'skills:author/repo/' + name,
        'url': 'https://skills.sh/author/repo/' + name,
        'evidence': {'views': {'all-time': {'installs': value}}}}
        for name, value in [('a-low', 500), ('b-high', 5000)]]
    return saved


def test_popular_sort_uses_only_typed_per_source_metrics_and_stable_unknowns(tmp_path, dashboard_browser):
    saved = popularity_state()
    payload = build_public_payload(saved, now=NOW)
    rows = {row['id']: row for row in payload['tool_items']}
    assert rows['github:owner/a-missing'].get('popularity') == {'value': None, 'basis': 'GitHub total stars'}
    assert rows['github:owner/b-high']['popularity'] == {'value': 100, 'basis': 'GitHub total stars'}
    assert rows['skills:author/repo/b-high']['popularity'] == {'value': 5000, 'basis': 'Skills.sh all-time installs'}
    assert 'PRIVATE_METRIC_CANARY' not in str(payload)
    context = dashboard_browser.new_context(offline=True)
    try:
        page = context.new_page()
        page.goto(artifact(tmp_path, saved).as_uri())
        page.get_by_label('Sort discoveries', exact=True).select_option('popular')
        assert page.locator('.discovery-item:visible').count() == 6
        assert page.locator('.discovery-item').evaluate_all('(xs)=>xs.map(x=>x.dataset.id)') == [
            'github:owner/b-high', 'github:owner/d-tie', 'github:owner/c-low', 'github:owner/a-missing',
            'skills:author/repo/b-high', 'skills:author/repo/a-low']
        assert 'within each source' in page.locator('#discovery-sort-basis').inner_text()
        assert 'not suitability' in page.locator('#discovery-sort-basis').inner_text()
        page.get_by_label('Discovery source', exact=True).select_option('skills-sh')
        assert page.locator('.discovery-item:visible').count() == 2
        page.get_by_label('Discovery source', exact=True).select_option('')
        page.get_by_label('Sort discoveries', exact=True).select_option('identity')
        assert page.locator('.discovery-item').first.get_attribute('data-id') == 'github:owner/a-missing'
    finally:
        context.close()


@pytest.mark.parametrize('evidence', [None, {}, [], {'total_stars': True}, {'total_stars': -1},
    {'total_stars': 10**12}, {'total_stars': '100'}, {'total_stars': float('nan')}])
def test_missing_or_malformed_popularity_never_becomes_zero_or_forged_prose(evidence):
    saved = state()
    saved['items'][0]['evidence'] = evidence
    row = build_public_payload(saved, now=NOW)['tool_items'][0]
    assert row['popularity'] == {'value': None, 'basis': 'GitHub total stars'}


def test_latest_sorts_valid_first_observation_not_refetch_or_regeneration(tmp_path, dashboard_browser):
    saved = popularity_state()
    times = ['2026-10-03T11:00:00Z', '2026-09-28T11:00:00Z', '2026-10-03T13:00:00Z',
             '2026-10-03T11:00:00Z', '2026-10-03T11:30:00+03:00', '2026-10-03T12:00:00']
    for row, first in zip(saved['items'], times):
        row['first_seen'] = first
        row['checked_at'] = '2026-10-03T11:59:59Z'
    payload = build_public_payload(saved, now=NOW)
    dates = {row['id']: row.get('first_observed_at') for row in payload['tool_items']}
    assert dates['github:owner/a-missing'] == times[0]
    assert dates['github:owner/c-low'] is None  # Future first-observation rejected.
    assert dates['skills:author/repo/b-high'] is None  # Offsetless is unknown.
    context = dashboard_browser.new_context(offline=True)
    try:
        page = context.new_page()
        page.goto(artifact(tmp_path, saved).as_uri())
        page.get_by_label('Sort discoveries', exact=True).select_option('latest')
        assert page.locator('.discovery-item:visible').count() == 6
        assert page.locator('.discovery-item').evaluate_all('(xs)=>xs.map(x=>x.dataset.id)') == [
            'github:owner/a-missing', 'github:owner/d-tie', 'skills:author/repo/a-low',
            'github:owner/b-high', 'github:owner/c-low', 'skills:author/repo/b-high']
        basis = page.locator('#discovery-sort-basis').inner_text()
        assert 'First observed' in basis and 'not publication' in basis and 'regeneration' in basis
        assert 'unknown' in page.locator('[data-id="github:owner/c-low"]').inner_text()
        page.get_by_label('Search discoveries', exact=True).fill('d-tie')
        assert page.locator('.discovery-item:visible').count() == 1
        page.get_by_label('Search discoveries', exact=True).fill('')
        assert page.locator('.discovery-item:visible').count() == 6
    finally:
        context.close()


def test_latest_respects_saved_submillisecond_precision(tmp_path, dashboard_browser):
    saved = popularity_state()
    for row in saved['items']:
        row['first_seen'] = None
    saved['items'][0]['first_seen'] = '2026-10-01T12:00:00.000001Z'
    saved['items'][1]['first_seen'] = '2026-10-01T12:00:00.000002Z'
    context = dashboard_browser.new_context(offline=True)
    try:
        page = context.new_page()
        page.goto(artifact(tmp_path, saved).as_uri())
        page.locator('#discovery-sort').select_option('latest')
        assert page.locator('.discovery-item').first.get_attribute('data-id') == 'github:owner/b-high'
    finally:
        context.close()


def test_relevant_is_explicitly_omitted_not_a_fake_option(tmp_path):
    soup = BeautifulSoup(artifact(tmp_path).read_text(encoding='utf-8'), 'html.parser')
    assert [o['value'] for o in soup.select('#discovery-sort option')] == ['identity', 'popular', 'latest']
    notice = soup.select_one('#relevance-policy')
    assert notice is not None
    assert 'Relevant is omitted' in notice.text
    assert 'literal search' in notice.text
    assert 'personal-project' not in soup.text.lower()


def test_public_capped_inventory_discloses_exported_total_without_project_review(tmp_path):
    saved = popularity_state()
    payload = build_public_payload(saved, now=NOW, max_items_per_source=1)
    assert payload.get('inventory_total') == 6
    assert len(payload['tool_items']) == 2
    soup = BeautifulSoup(LainReporter().render(**payload, public_export=True), 'html.parser')
    assert '2 of 6 collected rows' in soup.select_one('#discovery-inventory > summary').text
    assert 'omitted by caller cap: 3' in soup.select_one('#source-health').text
    assert 'recommendation_groups' not in payload


def test_public_artifact_tools_build_and_verify_real_single_file_offline(tmp_path):
    import subprocess
    import sys
    import hashlib
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    saved = tmp_path / 'state.json'
    saved.write_text(json.dumps(popularity_state()), encoding='utf-8')
    before = hashlib.sha256(saved.read_bytes()).hexdigest()
    output = tmp_path / 'latest'
    built = subprocess.run([sys.executable, str(root / 'scripts/build_project_ui_artifacts.py'),
        str(output), '--state', str(saved), '--news-db', str(tmp_path / 'missing.db')],
        capture_output=True, text=True)
    assert built.returncode == 0, built.stderr
    metadata = json.loads(built.stdout)
    assert metadata['public']['exported'] == 6
    path = Path(metadata['public']['path'])
    assert sorted(p.name for p in path.parent.iterdir()) == ['index.html']
    assert metadata['public']['sha256'] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert hashlib.sha256(saved.read_bytes()).hexdigest() == before
    checked = subprocess.run([sys.executable, str(root / 'scripts/verify_project_ui_browser.py'), str(path)],
        capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
    reports = json.loads(checked.stdout)
    assert len(reports) == 12  # 360 / 390 / 1440, JS on/off, both system themes.
    assert all(r['discovery_rows'] == 6 and r['project_groups'] == 0 for r in reports)
    assert all(r['no_horizontal_overflow'] and not r['page_errors'] and not r['console_errors'] for r in reports)
    assert all(r['network_requests'] == 0 for r in reports)
    assert all(r['sorting_verified'] for r in reports if r['javascript'])


def test_image_free_legacy_artifact_keeps_stricter_original_csp():
    from intel_sources.public_report import _standalone_html
    html = '<html><head><meta charset="UTF-8"></head><body>Legacy safe file</body></html>'
    checked = _standalone_html(html)
    assert "img-src 'none'" in checked
    assert 'img-src data:' not in checked


def contrast(color, background):
    import re
    def luminance(value):
        channels = [int(n) / 255 for n in re.findall(r'\d+', value)[:3]]
        return sum(weight * (v / 12.92 if v <= .04045 else ((v + .055) / 1.055) ** 2.4)
                   for weight, v in zip([.2126, .7152, .0722], channels))
    high, low = sorted([luminance(color), luminance(background)], reverse=True)
    return (high + .05) / (low + .05)


@pytest.mark.parametrize('storage_blocked', [False, True])
def test_grey_theme_system_default_toggle_persistence_and_storage_failure(tmp_path, dashboard_browser, storage_blocked):
    path = artifact(tmp_path)
    context = dashboard_browser.new_context(offline=True, color_scheme='light')
    if storage_blocked:
        context.add_init_script("Object.defineProperty(window, 'localStorage', {get(){throw new Error('unavailable')}})")
    try:
        page = context.new_page()
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(path.as_uri())
        toggle = page.get_by_role('button', name='Dark theme')
        assert toggle.is_visible(), 'theme control is missing'
        assert page.locator('body').evaluate('x => getComputedStyle(x).backgroundColor') == 'rgb(230, 233, 232)'
        for selector in ['body', '.prompt', 'p.meta', '.item-source', '#discovery-search']:
            colors = page.locator(selector).first.evaluate('x => [getComputedStyle(x).color, getComputedStyle(document.body).backgroundColor]')
            assert contrast(*colors) >= 4.5, (selector, colors)
        toggle.focus()
        page.keyboard.press('Enter')
        expect(page.locator('html')).to_have_attribute('data-theme', 'dark')
        assert page.get_by_role('button', name='Light theme').is_visible()
        page.reload()
        expect(page.locator('html')).to_have_attribute('data-theme', 'light' if storage_blocked else 'dark')
        if not storage_blocked:
            page.get_by_role('button', name='Light theme').click()
            page.reload()
            expect(page.locator('html')).to_have_attribute('data-theme', 'light')
        assert not errors
    finally:
        context.close()


def test_no_js_light_system_preference_keeps_readable_grey_layout(tmp_path, dashboard_browser):
    context = dashboard_browser.new_context(offline=True, java_script_enabled=False, color_scheme='light')
    try:
        page = context.new_page()
        page.goto(artifact(tmp_path).as_uri())
        assert page.locator('body').evaluate('x => getComputedStyle(x).backgroundColor') == 'rgb(230, 233, 232)'
        assert page.locator('#theme-toggle').is_disabled()
        page.locator('#discovery-inventory > summary').click()
        assert page.locator('.discovery-item:visible').count() == 2
    finally:
        context.close()
