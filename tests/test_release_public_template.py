"""Release regressions: supplied design, useful public metadata, actual Chromium."""
from datetime import datetime, timezone
import copy
import hashlib
import json
import os
from pathlib import Path
import re

from bs4 import BeautifulSoup
import pytest

from intel_sources.public_report import build_public_payload, export_public_report
from tracker_core.reporter_html import LainReporter

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
OBSERVED = '2026-10-01T11:00:00+00:00'
ORIGINAL_STYLE_SHA256 = '347adbd0fc894df1675f565ca9d93d591a320ac2b63934b0d7615fa3554e76b8'


def release_state():
    specs = [
        ('openrouter-models', 'openrouter:model:org/model', 'https://openrouter.ai/org/model', {
            'context_length': 128000, 'top_provider': {'max_completion_tokens': 8192},
            'architecture': {'input_modalities': ['text', 'image'], 'output_modalities': ['text']},
            'supported_parameters': ['tools', 'reasoning'],
            'pricing': {'prompt': '0.000002', 'completion': '0.00001'}}),
        ('openrouter-providers', 'openrouter:provider:host', 'https://openrouter.ai/provider/host', {
            'headquarters': 'US', 'datacenters': ['US', 'DE']}),
        ('unorouter-models', 'unorouter:model:model', 'https://unorouter.com/en/models/model', {
            'type': 'text', 'is_free': False, 'online': True,
            'tags': ['Tools', 'Vision'], 'supported_endpoint_types': ['openai'],
            'pricing': {'input_price': 2, 'output_price': 10, 'fixed_price': 0, 'is_fixed_price': False}}),
        ('github-trending', 'github:org/repo', 'https://github.com/org/repo', {
            'language': 'Python', 'total_stars': 12345, 'forks': 45,
            'periods': {'daily': {'rank': 2, 'stars_gained': 123}}}),
        ('skills-sh', 'skills:org/repo/skill', 'https://skills.sh/org/repo/skill', {
            'views': {'all-time': {'rank': 3, 'installs': 4567}}}),
    ]
    return {'items': [dict(source=s, id=i, url=u, headline=i.split(':', 1)[1],
        summary='PRIVATE_DESCRIPTION_CANARY', checked_at=OBSERVED,
        first_seen='2026-09-29T11:00:00+00:00', project_types=['tools'],
        evidence={**e, 'description': 'PRIVATE_EVIDENCE_CANARY', 'error': 'PRIVATE_ERROR_CANARY',
                  'api_key': 'PRIVATE_KEY_CANARY'}) for s, i, u, e in specs],
        'sources': [dict(name=s, status='ok', checked_at=OBSERVED, error='PRIVATE_SOURCE_CANARY')
                    for s, _, _, _ in specs]}


def render_export(tmp_path, state=None):
    source = tmp_path / 'state.json'
    source.write_text(json.dumps(state or release_state()), encoding='utf-8')
    return export_public_report(source, tmp_path / 'missing.db', tmp_path / 'public', now=NOW)


def test_public_typed_source_metadata_reaches_consumer_not_disclaimer_only(tmp_path):
    state = release_state()
    before = copy.deepcopy(state)
    artifact = render_export(tmp_path, state)
    soup = BeautifulSoup(artifact.read_text(encoding='utf-8'), 'html.parser')
    expected = {
        'openrouter-models': ['Context: 128,000 tokens', 'Max output: 8,192 tokens',
            'Input modalities: image, text', 'Output modalities: text',
            'Supported parameters: reasoning, tools',
            'Listed input price: USD 2 / million tokens', 'Listed output price: USD 10 / million tokens'],
        'openrouter-providers': ['Headquarters country: US', 'Datacenter countries: DE, US'],
        'unorouter-models': ['Model type: text', 'Catalog tags: Tools, Vision', 'Endpoint formats: openai',
            'Listed input price: USD 2 / million tokens', 'Listed output price: USD 10 / million tokens',
            'Catalog free flag: false', 'Catalog online flag: true'],
        'github-trending': ['Language: Python', 'Stars: 12,345', 'Forks: 45',
            'daily rank: 2', 'daily stars gained: 123'],
        'skills-sh': ['all-time rank: 3', 'all-time installs: 4,567'],
    }
    for source, fields in expected.items():
        row = soup.select_one(f'.discovery-item[data-source="{source}"]')
        assert row is not None
        for field in fields:
            assert field in row.get_text(' ', strip=True), (source, field)
        assert row.select_one('.public-metadata'), source
        assert OBSERVED in row.text
        assert 'unverified' in row.text or 'advisory only' in row.text
    assert 'PRIVATE_' not in soup.text
    assert state == before


def test_public_provider_with_missing_structured_fields_reports_unknown_not_disclaimer_only(tmp_path):
    state = release_state()
    state['items'][1]['evidence'] = {'headquarters': None, 'datacenters': None,
        'description': 'PRIVATE_PROVIDER_DESCRIPTION_CANARY'}
    soup = BeautifulSoup(render_export(tmp_path, state).read_text(encoding='utf-8'), 'html.parser')
    row = soup.select_one('.discovery-item[data-source="openrouter-providers"]')
    assert row.select_one('.public-metadata-unavailable')
    assert 'Structured public metadata not supplied in this saved observation' in row.text
    assert 'PRIVATE_' not in soup.text


def test_public_source_window_coverage_is_typed_not_generic_or_raw_error(tmp_path):
    state = release_state()
    state['sources'][3]['coverage'] = {'periods': {'daily': {'status': 'ok', 'item_count': 15},
        'weekly': {'status': 'partial', 'item_count': 12}, 'monthly': {'status': 'error', 'error': 'PRIVATE_PERIOD_CANARY'}}}
    state['sources'][4]['coverage'] = {'mode': 'public-html', 'views': {
        'all-time': {'status': 'ok', 'rendered_rows': 100}, 'trending': {'status': 'partial', 'rendered_rows': 98},
        'hot': {'status': 'error', 'error': 'PRIVATE_VIEW_CANARY'}}}
    payload = build_public_payload(state, now=NOW)
    coverage = {s['name']: s['coverage'] for s in payload['source_items']}
    assert 'daily: ok (15 rows)' in coverage['github-trending']
    assert 'weekly: partial (12 rows)' in coverage['github-trending']
    assert 'monthly: error (rows unavailable)' in coverage['github-trending']
    assert 'all-time: ok (100 rows)' in coverage['skills-sh']
    assert 'trending: partial (98 rows)' in coverage['skills-sh']
    assert 'hot: error (rows unavailable)' in coverage['skills-sh']
    assert 'Valid saved public inventory: 1 rows' in coverage['skills-sh']
    assert 'PRIVATE_' not in str(payload)


def test_typed_metadata_advertised_inventory_matches_reachable_typed_rows(tmp_path):
    state = release_state()
    for row in state['items']:
        row['evidence'] = {'description': 'PRIVATE_PROSE_ONLY_CANARY'}
    payload = build_public_payload(state, now=NOW)
    advertised = {s['name']: int(re.search(r'Valid saved public inventory: ([\d,]+) rows', s['coverage']).group(1).replace(',', ''))
                  for s in payload['source_items']}
    typed = {name: sum(1 for r in payload['tool_items'] if r['source'] == name and r.get('public_metadata'))
             for name in advertised}
    # Rows without vetted typed fields are still published with an explicit notice,
    # so advertised inventory must count the reachable rows, not only the typed ones.
    assert advertised == {name: sum(1 for r in payload['tool_items'] if r['source'] == name) for name in advertised}
    assert typed == {'openrouter-models': 0, 'openrouter-providers': 0, 'unorouter-models': 0,
                     'github-trending': 0, 'skills-sh': 0}
    soup = BeautifulSoup(LainReporter().render(**payload, public_export=True), 'html.parser')
    assert soup.select('.public-metadata-unavailable') and not soup.select('.public-metadata')


def test_public_layout_starts_explore_without_personal_review_or_unchecked_changes(tmp_path):
    artifact = render_export(tmp_path)
    soup = BeautifulSoup(artifact.read_text(encoding='utf-8'), 'html.parser')
    sections = [s.get('id') for s in soup.select('section.tracker')]
    # R09 supersedes the earlier personalized decision-first public product.
    assert sections == ['discoveries', 'public-scope', 'source-health']
    assert not soup.select('#project-shortlists')
    assert 'changes' not in sections and 'actions' not in sections
    assert 'No material changes detected' not in soup.text
    assert 'Provider observations excluded from this public export' in soup.select_one('#providers').text
    assert 'News excluded from this public export' in soup.select_one('#news').text
    assert 'not a fresh fetch' in soup.footer.text


def test_named_lain_reporter_never_silently_renders_an_unrelated_fallback(tmp_path, monkeypatch):
    from jinja2 import TemplateNotFound
    from tracker_core.reporter_html import HTMLReporter
    monkeypatch.setattr(HTMLReporter, 'TEMPLATE_DIR', tmp_path)
    (tmp_path / 'report_template.html').write_text('unrelated fallback design', encoding='utf-8')
    with pytest.raises(TemplateNotFound):
        LainReporter().render(api_items=[])


def test_public_export_missing_design_is_generic_and_writes_nothing(tmp_path, monkeypatch):
    from tracker_core.reporter_html import HTMLReporter
    template_dir = tmp_path / 'templates'
    template_dir.mkdir()
    (template_dir / 'report_template.html').write_text('unrelated fallback', encoding='utf-8')
    monkeypatch.setattr(HTMLReporter, 'TEMPLATE_DIR', template_dir)
    with pytest.raises(ValueError, match='public template unavailable'):
        render_export(tmp_path)
    assert not (tmp_path / 'public/index.html').exists()


def test_public_excludes_key_scoped_discoveries_even_under_a_public_source_name(tmp_path):
    state = release_state()
    state['sources'][-1]['coverage'] = {'mode': 'api'}
    state['items'][0]['evidence']['catalog_scope'] = 'key-accessible'
    state['items'][2]['evidence']['provenance'] = {'kind': 'key_scoped_catalog'}
    payload = build_public_payload(state, now=NOW)
    assert {r['source'] for r in payload['tool_items']} == {'github-trending', 'openrouter-providers'}
    assert 'skills-sh' not in {r['name'] for r in payload['source_items']}
    assert 'PRIVATE_' not in str(payload)


@pytest.mark.parametrize('bad', [True, -1, 10**30, float('nan'), float('inf'), 'PRIVATE_NUMBER_CANARY',
    '0;PRIVATE_PRICE_CANARY', '1e999', '-0.2', '<script>PRIVATE_XSS_CANARY</script>', {}, [], None])
def test_typed_metadata_rejects_malformed_private_numbers_and_prose(bad):
    state = release_state()
    model, provider, uno, repo, skill = state['items']
    model['evidence'].update(context_length=bad, top_provider={'max_completion_tokens': bad},
        pricing={'prompt': bad, 'completion': bad}, architecture={'input_modalities': [bad]},
        supported_parameters=[bad])
    provider['evidence'].update(headquarters=bad, datacenters=[bad])
    uno['evidence'].update(type=bad, tags=[bad], supported_endpoint_types=[bad],
        pricing={'input_price': bad, 'output_price': bad, 'fixed_price': bad, 'is_fixed_price': False})
    repo['evidence'].update(language=bad, total_stars=bad, forks=bad,
        periods={'daily': {'rank': bad, 'stars_gained': bad}})
    skill['evidence'].update(views={'all-time': {'rank': bad, 'installs': bad}})
    for row in state['items']:
        row['public_metadata'] = [{'label': 'PRIVATE_LABEL_CANARY', 'value': 'PRIVATE_FORGED_CANARY'}]
        row['fingerprint_data'] = {'description': 'PRIVATE_FINGERPRINT_CANARY'}
    payload = build_public_payload(state, now=NOW)
    assert 'PRIVATE_' not in str(payload)
    assert all('public_metadata' not in r for r in payload['tool_items'] if r['source'] != 'unorouter-models')
    # The legitimate typed catalog booleans remain, never an availability claim.
    assert [m['label'] for m in next(r for r in payload['tool_items'] if r['source']=='unorouter-models')['public_metadata']] == ['Catalog free flag', 'Catalog online flag']


@pytest.fixture(scope='module')
def release_browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True,
            executable_path=os.environ.get('PLAYWRIGHT_EXECUTABLE_PATH'))
        yield browser
        browser.close()


def contrast_ratio(color, background):
    def luminance(css):
        rgb = [int(n)/255 for n in re.findall(r'\d+', css)[:3]]
        linear = [v/12.92 if v <= .04045 else ((v+.055)/1.055)**2.4 for v in rgb]
        return sum(a*b for a,b in zip(linear, [.2126, .7152, .0722]))
    high, low = sorted([luminance(color), luminance(background)], reverse=True)
    return (high+.05)/(low+.05)


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('javascript', [True, False])
def test_release_chromium_all_controls_offline_metadata_and_readable_counters(release_browser, tmp_path, width, javascript):
    state = release_state()
    artifact = render_export(tmp_path, state)
    context = release_browser.new_context(viewport={'width': width, 'height': 900},
        offline=True, java_script_enabled=javascript, color_scheme='dark')
    page = context.new_page()
    requests, errors, console_errors = [], [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda msg: console_errors.append(msg.text) if msg.type == 'error' else None)
    try:
        page.goto(artifact.as_uri(), wait_until='load')
        # R09: Explore is the first public section. Measure before interaction,
        # so native disclosure auto-scroll cannot hide a layout regression.
        assert page.evaluate('scrollY') == 0
        assert page.locator('#discoveries').evaluate('(x)=>x.getBoundingClientRect().top') < 300
        assert page.locator('#project-shortlists').count() == 0
        # Nothing in a public report may start expanded.
        assert page.locator('details[open]').count() == 0
        # Collapsed by default: the status must point at the disclosure rather
        # than claim its rows are already on screen.
        assert page.locator('#discovery-result-count').text_content() == (
            '5 discoveries collected \u2014 open the inventory below to browse and filter')
        # The catalog is a collapsed archive by default; opening it must reveal
        # every collected row, never a filtered subset.
        assert page.locator('.discovery-item:visible').count() == 0
        page.locator('#discovery-inventory > summary').click()
        assert page.locator('.discovery-item:visible').count() == len(state['items'])
        assert page.locator('.public-metadata').count() == len(state['items'])
        assert page.locator('.wrap').evaluate('(x)=>getComputedStyle(x).maxWidth') == '1120px'
        assert page.locator('.portrait').evaluate('(x)=>getComputedStyle(x).width') == '64px'
        assert page.locator('body').evaluate('(x)=>getComputedStyle(x).backgroundColor') == 'rgb(5, 7, 10)'
        assert page.locator('h1').evaluate('(x)=>getComputedStyle(x).color') == 'rgb(111, 255, 233)'
        assert not page.evaluate('document.documentElement.scrollWidth > innerWidth')
        count = page.locator('#discovery-result-count')
        colors = count.evaluate('(x)=>[getComputedStyle(x).color,getComputedStyle(document.body).backgroundColor]')
        assert contrast_ratio(*colors) >= 4.5, colors
        if javascript:
            # The controller reports the open, unfiltered archive.
            from playwright.sync_api import expect
            expect(count).to_have_text('5 discoveries, no filter applied')
        else:
            # Without the controller the static line stands; it never claims
            # rows are on screen, which is the truthful thing to say.
            assert count.text_content() == (
                '5 discoveries collected \u2014 open the inventory below to browse and filter')
        if javascript:
            for row in state['items']:
                page.get_by_label('Discovery source', exact=True).select_option(row['source'])
                assert page.locator('.discovery-item:visible').count() == 1
                assert count.text_content() == '1 of 5 discoveries match the filters'
                assert page.locator('.discovery-item:visible .public-metadata').count() == 1
            page.get_by_label('Search discoveries', exact=True).fill('no-match-zzzz')
            assert page.locator('#discovery-empty').is_visible()
            assert count.text_content() == '0 of 5 discoveries match the filters'
            page.get_by_label('Search discoveries', exact=True).fill('')
            page.get_by_label('Discovery source', exact=True).select_option('')
            page.get_by_label('Project type', exact=True).select_option('tools')
            assert page.locator('.discovery-item:visible').count() == 5
            page.get_by_label('Project type', exact=True).select_option('')
            page.get_by_label('Search discoveries', exact=True).fill('Context: 128,000')
            assert page.locator('.discovery-item:visible').count() == 1
            page.get_by_label('Search discoveries', exact=True).fill('')
            page.get_by_label('Search discoveries', exact=True).focus()
            assert page.get_by_label('Search discoveries', exact=True).evaluate('(x)=>x===document.activeElement')
        else:
            for control in ['#discovery-search', '#discovery-project', '#discovery-source']:
                assert page.locator(control).is_disabled(), control
            assert page.get_by_text('Filters require JavaScript; every collected row is still reachable below, in the collapsible inventory archive.', exact=True).is_visible()
        details = page.locator('details')
        # Every disclosure still toggles both ways. Default-closed is asserted
        # above on a freshly loaded page; the inventory was reopened since.
        assert details.evaluate_all('''(xs)=>{for(const d of xs){const s=d.querySelector(':scope > summary');if(d.open)s.click();if(d.open)throw Error('close');s.click();if(!d.open)throw Error('open');s.click();if(d.open)throw Error('close');}return xs.length;}''') == details.count()
        # The shortlist's own deferred/excluded disclosures now come first in the
        # document, so the evidence disclosure is addressed inside the inventory.
        # The toggle sweep above leaves every disclosure closed, including the
        # inventory archive, so reopen it before inspecting an evidence row.
        page.locator('#discovery-inventory > summary').click()
        evidence = page.locator('#discoveries details.evidence').first
        evidence.locator(':scope > summary').click()
        assert evidence.get_attribute('open') is not None
        assert OBSERVED in evidence.inner_text()
        assert 'PRIVATE_' not in page.locator('body').inner_text()
        assert page.locator('[onerror], [onclick], iframe, script[src]').count() == 0
        from tracker_core.branding import avatar_data_uri
        assert page.locator('img').get_attribute('src') == avatar_data_uri()
        for href in page.locator('a[href]').evaluate_all('(xs)=>xs.map(x=>x.getAttribute("href"))'):
            assert href in {'#discoveries', '#source-health', '#public-scope'} or href.startswith(('https://openrouter.ai/', 'https://unorouter.com/',
                'https://api.unorouter.com/', 'https://github.com/', 'https://skills.sh'))
        assert requests == [artifact.as_uri()]
        assert not errors and not console_errors
    finally:
        context.close()


def test_locked_design_and_template_copies_are_identical():
    active = ROOT / 'src/templates/report_template_lain.html'
    packaged = ROOT / 'src/tracker-core/templates/report_template_lain.html'
    assert active.read_bytes() == packaged.read_bytes()
    locked = re.search(r'^<style>\n.*?</style>', active.read_text(encoding='utf-8'), re.S | re.M).group()
    assert hashlib.sha256(locked.encode()).hexdigest() == ORIGINAL_STYLE_SHA256
    reporter = LainReporter()
    assert Path(reporter.env.get_template('report_template_lain.html').filename).resolve() == active.resolve()


@pytest.mark.parametrize('checkout', [True, False])
def test_template_lookup_supports_checkout_and_installed_wheel_layout(tmp_path, checkout):
    from tracker_core import reporter_html

    module_file = tmp_path / 'python/site-packages/tracker_core/reporter_html.py'
    installed = module_file.parents[1] / 'templates'
    legacy = module_file.parents[2] / 'templates'
    installed.mkdir(parents=True)
    (installed / 'report_template_lain.html').write_text('<html>installed template</html>', encoding='utf-8')
    if checkout:
        legacy.mkdir()
        (legacy / 'report_template_lain.html').write_text('<html>checkout template</html>', encoding='utf-8')
    namespace = {'__file__': str(module_file), '__name__': reporter_html.__name__,
                 '__package__': 'tracker_core'}
    source = Path(reporter_html.__file__).read_text(encoding='utf-8')
    exec(compile(source, reporter_html.__file__, 'exec'), namespace)
    reporter = namespace['LainReporter']()
    assert reporter.TEMPLATE_DIR == (legacy if checkout else installed)
    assert reporter.render(api_items=[], public_export=True) == (
        '<html>checkout template</html>' if checkout else '<html>installed template</html>')

