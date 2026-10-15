"""Assert production public dashboard/daily-digest behavior in real offline Chromium files."""
from __future__ import annotations
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import sys

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, expect


def _kind(path: Path) -> str:
    """Detect template contract: 'dashboard' for cockpit/catalog, 'daily' for daily-digest."""
    soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
    if soup.select('#discovery-inventory'):
        return 'dashboard'
    if soup.select('#discovery-list'):
        return 'daily'
    return 'unknown'


def verify(browser, path: Path, width: int, javascript: bool, theme: str) -> dict:
    kind = _kind(path)
    soup = BeautifulSoup(path.read_text(encoding='utf-8'), 'html.parser')
    if kind == 'dashboard':
        exported = soup.select('.discovery-item')
        identities = [row['data-id'] for row in exported]
    else:
        exported = soup.select('.digest-item')
        identities = [row['data-id'] for row in exported]
    context = browser.new_context(viewport={'width': width, 'height': 900}, offline=True,
                                  java_script_enabled=javascript, color_scheme=theme)
    page = context.new_page()
    requests, errors, console_errors = [], [], []
    page.on('request', lambda request: requests.append(request.url))
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('console', lambda msg: console_errors.append(msg.text) if msg.type == 'error' else None)
    try:
        page.goto(path.resolve().as_uri(), wait_until='load')
        assert page.evaluate('scrollY') == 0
        # Dashboard: #discoveries is near top. Daily digest: section is further down.
        assert page.locator('#discoveries').count() > 0
        assert page.locator('#project-shortlists').count() == 0
        assert page.locator('details[open]').count() == 0
        # Dashboard template: body fontSize=17px; daily digest: html=100% → 16px
        if kind == 'dashboard':
            assert page.locator('body').evaluate('x=>getComputedStyle(x).fontSize') == '17px'
            assert page.locator('body').evaluate('x=>getComputedStyle(x).backgroundColor') == (
                'rgb(230, 233, 232)' if theme == 'light' else 'rgb(5, 7, 10)')
        else:
            assert page.locator('body').evaluate('x=>getComputedStyle(x).fontSize') == '16px'
        if kind == 'dashboard':
            assert page.locator('.portrait img').evaluate('x=>x.complete && x.naturalWidth > 0')
            summary = page.locator('#discovery-inventory > summary')
            summary.focus()
            page.keyboard.press('Enter')
            assert page.locator('.discovery-item:visible').count() == len(identities)
            status = page.locator('#discovery-result-count')
            if javascript:
                expect(status).to_have_text(f'{len(identities)} discoveries, no filter applied')
            else:
                assert page.locator('#discovery-sort').is_disabled()
                assert page.locator('#theme-toggle').is_disabled()
            sorting_verified = False
            if javascript:
                popular = sorted(exported, key=lambda row: (row['data-source'],
                    row['data-popularity'] == '', -int(row['data-popularity'] or 0), row['data-id']))
                def latest_key(row):
                    value = row['data-first-observed']
                    stamp = datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp() if value else 0
                    return (not bool(value), -stamp, row['data-source'], row['data-id'])
                latest = sorted(exported, key=latest_key)
                for mode, expected in [('popular', popular), ('latest', latest)]:
                    page.locator('#discovery-sort').select_option(mode)
                    assert page.locator('.discovery-item').evaluate_all('(xs)=>xs.map(x=>x.dataset.id)') == [row['data-id'] for row in expected]
                    assert page.locator('.discovery-item:visible').count() == len(identities)
                sorting_verified = True
                page.locator('#discovery-search').fill('no-match-dashboard-qa-zzzz')
                expect(status).to_have_text(f'0 of {len(identities)} discoveries match the filters')
                assert page.locator('#discovery-empty').is_visible()
                page.locator('#discovery-search').fill('')
                for control in ['#discovery-project', '#discovery-source']:
                    options = page.locator(control + ' option').evaluate_all('(xs)=>xs.map(x=>x.value).filter(Boolean)')
                    if options:
                        page.locator(control).select_option(options[0])
                        assert page.locator('.discovery-item:visible').count() > 0
                        page.locator(control).select_option('')
                expect(status).to_have_text(f'{len(identities)} discoveries, no filter applied')
                page.locator('#theme-toggle').click()
                changed = 'dark' if theme == 'light' else 'light'
                expect(page.locator('html')).to_have_attribute('data-theme', changed)
                page.reload()
                expect(page.locator('html')).to_have_attribute('data-theme', changed)
        else:
            # daily digest: use .digest-item, no inventory, no sort/theme-toggle
            assert page.locator('#discovery-inventory').count() == 0
            assert page.locator('#discovery-sort').count() == 0
            assert page.locator('#theme-toggle').count() == 0
            assert page.locator('.portrait img').count() == 0
            assert page.locator('.digest-item:visible').count() == len(identities)
            if javascript:
                page.locator('#discovery-search').fill('no-match-daily-qa-zzzz')
                assert page.locator('#discovery-empty').is_visible()
                page.locator('#discovery-search').fill('')
                for control in ['#discovery-category', '#discovery-source']:
                    options = page.locator(control + ' option').evaluate_all('(xs)=>xs.map(x=>x.value).filter(Boolean)')
                    if options:
                        page.locator(control).select_option(options[0])
                        assert page.locator('.digest-item:visible').count() > 0
                        page.locator(control).select_option('')
            else:
                # Daily digest inputs exist but have no disabled attr;
                # JS is required for filtering — noscript note confirms this.
                pass
        # Check the full DOM, not only visible text in closed disclosures.
        text = page.locator('body').text_content().lower()
        assert not any(term in text for term in ('desktop app', 'desktop-app', 'vision-app', 'arcade-game', 'localhost', 'c:\\users'))
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        page.evaluate('document.body.style.zoom="200%"')
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        network = [url for url in requests if url.startswith(('http:', 'https:'))]
        assert not network and not errors and not console_errors
        assert all(url == path.resolve().as_uri() for url in requests)
        return {'width': width, 'javascript': javascript, 'system_theme': theme,
            'discovery_rows': len(identities) if kind == 'dashboard' else len(soup.select('#discovery-list .digest-item')), 'project_groups': 0,
            'no_horizontal_overflow': True, 'zoom_200_percent': True,
            'sorting_verified': sorting_verified if kind == 'dashboard' else False,
            'network_requests': len(network),
            'page_errors': errors, 'console_errors': console_errors,
            'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    finally:
        context.close()


def main(paths: list[str]) -> None:
    if not paths:
        raise ValueError('supply at least one public index.html')
    reports = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True,
            executable_path=os.environ.get('PLAYWRIGHT_EXECUTABLE_PATH'))
        try:
            for raw in paths:
                for width in (360, 390, 1440):
                    for javascript in (True, False):
                        for theme in ('dark', 'light'):
                            reports.append(verify(browser, Path(raw), width, javascript, theme))
        finally:
            browser.close()
    print(json.dumps(reports, indent=2, sort_keys=True))


if __name__ == '__main__':
    main(sys.argv[1:])
