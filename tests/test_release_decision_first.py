"""User acceptance: a decision report first, complete observed inventory second."""
from datetime import datetime, timezone
from pathlib import Path
import os

from bs4 import BeautifulSoup
import pytest
from playwright.sync_api import sync_playwright, expect

from intel_sources.public_report import build_public_payload, _standalone_html
from tracker_core.reporter_html import LainReporter

NOW = datetime(2026, 10, 1, 18, tzinfo=timezone.utc)
CHECKED = '2026-10-01T17:00:00+00:00'
TDD = 'skills:obra/superpowers/test-driven-development'


def public_payload(extra_rows=150):
    identities = [TDD] + [f'skills:fixture-owner/public-repo/unreviewed-{index}' for index in range(extra_rows)]
    items = [{'id':identifier, 'headline':identifier.split(':',1)[1],
              'source':'skills-sh', 'url':'https://skills.sh/'+identifier.split(':',1)[1],
              'checked_at':CHECKED, 'evidence':{}, 'project_types':['tools']}
             for identifier in identities]
    return build_public_payload({'items':items,'sources':[
        {'name':'skills-sh','status':'ok','checked_at':CHECKED}]}, now=NOW)


@pytest.mark.parametrize('public_export',[True,False])
def test_public_projection_has_no_personal_review_in_either_render_mode(public_export):
    payload=public_payload()
    soup=BeautifulSoup(LainReporter().render(**payload,public_export=public_export),'html.parser')
    order=[section.get('id') for section in soup.select('section.tracker')]
    # Public projection no longer produces personal project recommendations (R09).
    assert not soup.select('#project-shortlists')
    if public_export:
        assert order[0] == 'discoveries'
    assert len(soup.select('#discoveries .discovery-item')) == len(payload['tool_items'])


def test_public_inventory_is_explicit_native_disclosure_without_data_loss():
    payload=public_payload()
    soup=BeautifulSoup(LainReporter().render(**payload,public_export=True),'html.parser')
    inventory=soup.select_one('details#discovery-inventory')
    assert inventory is not None, 'the full catalog is still the default open view'
    assert not inventory.has_attr('open')
    assert inventory.find('summary',recursive=False) is not None
    assert 'Browse complete exported inventory' in inventory.find('summary', recursive=False).get_text()
    assert f"{len(payload['tool_items'])} of {payload['inventory_total']} collected rows" in \
        inventory.find('summary', recursive=False).get_text()
    assert 'not recommendations' in inventory.get_text()
    # Nothing in a public report may start expanded.
    assert not soup.select('details[open]')
    # The filter result counter stays outside the collapsed archive so its live
    # region is announced rather than hidden until the catalog is opened.
    count = soup.select_one('#discovery-result-count')
    assert count is not None and count.find_parent('details') is None
    assert 'open the inventory below to browse and filter' in count.text
    assert count.get('aria-controls') == 'discovery-inventory'
    # The controls that mutate the announced number sit in the same disclosure
    # scope as the number itself, never hidden behind the archive it filters.
    filters = soup.select_one('.discovery-filters')
    assert filters is not None and filters.find_parent('details') is None
    assert soup.select_one('#discovery-empty').find_parent('details') is None
    assert soup.select_one('noscript').find_parent('details') is None
    # No stutter: one count line, not a header plus an identical status.
    assert len(soup.select('#discoveries > .meta[id="discovery-result-count"]')) == 1
    assert 'tool / model discoveries' not in count.text
    noscript=soup.select_one('noscript').get_text()
    assert 'Filters require JavaScript; every collected row is still reachable below' in noscript
    assert 'all discoveries are shown below' not in soup.get_text()
    # The notice points at the element, never at a label that a degraded payload
    # may not render.
    assert 'Browse complete exported inventory' not in noscript
    assert inventory.find('summary',recursive=False).get_text(' ',strip=True).startswith('Browse ')
    assert len(inventory.select('.discovery-item'))==len(payload['tool_items'])
    assert soup.select_one('#recommendation-coverage') is None


def capped_payload(cap):
    identities = [TDD] + [f'skills:fixture-owner/public-repo/unreviewed-{index}' for index in range(40)]
    items = [{'id':identifier, 'headline':identifier.split(':',1)[1],
              'source':'skills-sh', 'url':'https://skills.sh/'+identifier.split(':',1)[1],
              'checked_at':CHECKED, 'evidence':{}, 'project_types':['tools']}
             for identifier in identities]
    return build_public_payload({'items':items,'sources':[
        {'name':'skills-sh','status':'ok','checked_at':CHECKED}]}, now=NOW,
        max_items_per_source=cap)


def test_capped_export_never_claims_a_complete_catalog():
    payload = capped_payload(5)
    exported = len(payload['tool_items'])
    total = payload['inventory_total']
    assert exported < total, 'fixture must actually be capped'
    soup = BeautifulSoup(LainReporter().render(**payload,public_export=True),'html.parser')
    summary = soup.select_one('details#discovery-inventory > summary').get_text(' ', strip=True)
    assert f'{exported} of {total} collected rows' in summary, summary
    assert len(soup.select('#discoveries .discovery-item')) == exported
    # Omitted rows stay disclosed through coverage, never silently dropped.
    assert f'omitted by caller cap: {total - exported:,}' in soup.select_one('#source-health').get_text()


@pytest.fixture(scope='module')
def decision_browser():
    """Exactly the suite's browser policy: an explicit path or CI must never turn a
    failure into a skip; only a genuinely absent default browser in an unconfigured
    local run may skip. A launch failure always propagates."""
    with sync_playwright() as playwright:
        executable=os.environ.get('PLAYWRIGHT_EXECUTABLE_PATH')
        local_browser_missing=not Path(playwright.chromium.executable_path).is_file()
        if not executable and os.getenv('GITHUB_ACTIONS')!='true' and local_browser_missing:
            pytest.skip('Chromium is not installed locally; install it or set PLAYWRIGHT_EXECUTABLE_PATH')
        instance=playwright.chromium.launch(headless=True,executable_path=executable)
        yield instance
        instance.close()


@pytest.mark.parametrize('javascript',[True,False])
@pytest.mark.parametrize('width',[390,1440])
def test_mobile_desktop_explore_first_and_inventory_opens_offline(tmp_path,javascript,width,decision_browser):
    payload=public_payload()
    path=tmp_path/'index.html'
    path.write_text(_standalone_html(LainReporter().render(**payload,public_export=True)),encoding='utf-8')
    context=decision_browser.new_context(viewport={'width':width,'height':900},java_script_enabled=javascript,offline=True,color_scheme="dark")
    page=context.new_page()
    requests,errors=[],[]
    page.on('request',lambda request:requests.append(request.url))
    page.on('pageerror',lambda error:errors.append(str(error)))
    try:
        page.goto(path.as_uri(),wait_until='load')
        assert page.locator('#discoveries .discovery-item:visible').count()==0, 'unreviewed catalog is exposed by default'
        assert page.evaluate('scrollY') == 0
        assert page.locator('#discoveries').bounding_box()['y'] < 300
        assert page.locator('#project-shortlists').count() == 0
        inventory=page.locator('#discovery-inventory')
        summary=inventory.locator(':scope > summary')
        # Native keyboard activation must work even without scripting.
        summary.focus()
        page.keyboard.press('Enter')
        assert inventory.get_attribute('open') is not None
        assert page.locator('#discoveries .discovery-item:visible').count()==len(payload['tool_items'])
        if javascript:
            # Filters are outside the archive, so they must be usable while it
            # is closed — and must reveal whatever they matched.
            summary.focus()
            page.keyboard.press('Enter')
            assert inventory.get_attribute('open') is None
            expect(page.locator('#discovery-result-count')).to_have_text(
                '%d discoveries collected \u2014 open the inventory below to browse and filter'%len(payload['tool_items']))
            unpinned_color=summary.evaluate('(x)=>getComputedStyle(x).color')
            assert page.locator('#discovery-search').is_enabled()
            page.locator('#discovery-search').fill('unreviewed-149')
            assert inventory.get_attribute('open') is not None, 'filtering left its result hidden in a collapsed archive'
            assert page.locator('#discoveries .discovery-item:visible').count()==1
            assert page.locator('#discovery-result-count').text_content()=='1 of %d discoveries match the filters'%len(payload['tool_items'])
            # Pinned open while filtered, and visibly marked as such, so the
            # reader is not fighting a disclosure that springs back.
            assert inventory.get_attribute('data-pinned')=='true'
            # A pointer cursor is shared with the unpinned state and cannot
            # establish a visible pin cue. Compare the actual resolved colour.
            pinned_color=summary.evaluate('(x)=>getComputedStyle(x).color')
            assert pinned_color != unpinned_color, 'pin cue is visually identical to the unpinned summary'
            cyan=summary.evaluate('(x)=>getComputedStyle(x).getPropertyValue("--cyan").trim()')
            assert cyan=='#6fffe9'
            assert pinned_color=='rgb(111, 255, 233)'
            page.locator('#discovery-search').fill('')
            assert inventory.get_attribute('data-pinned') is None
            assert summary.evaluate('(x)=>getComputedStyle(x).color')==unpinned_color
            assert page.locator('#discoveries .discovery-item:visible').count()==len(payload['tool_items'])
        else:
            assert page.locator('#discovery-search').is_disabled()
        summary.focus()
        page.keyboard.press('Enter')
        assert inventory.get_attribute('open') is None
        assert page.locator('#discoveries .discovery-item:visible').count()==0
        # A collapsed archive must never be described as if its rows were on screen.
        total=len(payload['tool_items'])
        # Native disclosure toggles queue their event. Poll for the exact
        # resulting text rather than racing that event with text_content().
        expect(page.locator('#discovery-result-count')).to_have_text(
            '%d discoveries collected \u2014 open the inventory below to browse and filter'%total)
        assert 'match the filters' not in page.locator('#discovery-result-count').text_content()
        if javascript:
            summary.focus()
            page.keyboard.press('Enter')
            assert inventory.get_attribute('open') is not None
            expect(page.locator('#discovery-result-count')).to_have_text(
                '%d discoveries, no filter applied'%total)
            summary.focus()
            page.keyboard.press('Enter')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert requests==[path.as_uri()] and not errors
    finally:
        context.close()
