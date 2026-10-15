"""Approved Skills.sh detail redirects must not broaden listing identity policy."""
import copy
from datetime import datetime, timezone

import pytest

from intel_sources.public_report import build_public_payload

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
LISTING_OBSERVED = '2026-09-01T10:00:00+00:00'
DETAIL_OBSERVED = '2026-10-01T10:00:00+00:00'
CANONICAL = 'https://skills.sh/owner/repo/sql-tool'
REDIRECT = 'https://www.skills.sh/owner/repo/sql-tool'


def saved_state():
    return {'sources': [{'name': 'skills-sh', 'status': 'ok', 'checked_at': LISTING_OBSERVED}],
            'items': [{'id': 'skills:owner/repo/sql-tool', 'source': 'skills-sh',
                       'headline': 'SQL Tool', 'url': CANONICAL,
                       'checked_at': LISTING_OBSERVED, 'first_seen': LISTING_OBSERVED,
                       'evidence': {'views': {'all-time': {'rank': 7, 'installs': 1234}},
                                    'metadata': {'url': CANONICAL, 'final_url': REDIRECT,
                                                 'status': 'ok', 'checked_at': DETAIL_OBSERVED,
                                                 'http_status': 200, 'sha256': 'a' * 64,
                                                 'description_available': True, 'topics_available': False,
                                                 'description_field': 'meta[name=description]',
                                                 'description': 'PRIVATE_DESCRIPTION_CANARY',
                                                 'error': 'PRIVATE_ERROR_CANARY'}}}]}


def test_approved_www_detail_preserves_typed_metadata_and_canonical_listing():
    saved = saved_state()
    original = copy.deepcopy(saved)
    item = build_public_payload(saved, now=NOW)['tool_items'][0]
    fields = {entry['label']: entry['value'] for entry in item['public_metadata']}
    assert fields == {'all-time rank': '7', 'all-time installs': '1,234',
                      'Detail metadata status': 'ok', 'Detail observed at': DETAIL_OBSERVED,
                      'Detail HTTP status': '200', 'Detail body SHA-256': 'a' * 64,
                      'Detail description available': 'true', 'Detail topics available': 'false',
                      'Detail description field': 'meta[name=description]'}
    assert item['url'] == CANONICAL
    assert item['freshness'] == 'stale'
    assert item['evidence'][0]['observed_at'] == LISTING_OBSERVED
    assert 'PRIVATE_' not in str(item)
    assert saved == original


@pytest.mark.parametrize('key', ['url', 'final_url'])
@pytest.mark.parametrize('url', [
    'https://evil.example/owner/repo/sql-tool',
    'https://www.skills.sh.evil.example/owner/repo/sql-tool',
    'https://skills.sh.evil.example/owner/repo/sql-tool',
    'https://user:password@www.skills.sh/owner/repo/sql-tool',
    'https://www.skills.sh/owner/repo/sql-tool?token=PRIVATE_CANARY',
    'https://www.skills.sh/owner/repo/sql-tool#PRIVATE_CANARY',
    'http://www.skills.sh/owner/repo/sql-tool',
    'https://www.skills.sh:444/owner/repo/sql-tool',
    'https://www.skills.sh/other/repo/sql-tool',
    'https://www.skills.sh/owner/other/sql-tool',
    'https://www.skills.sh/owner/repo/other',
    'https://www.skills.sh/Owner/repo/sql-tool',
    'https://www.skills.sh/owner/repo/sql-tool/',
    'https://www.skills.sh/owner/repo/sql-tool/extra',
    'https://www.skills.sh/owner/repo/%73ql-tool',
    'https://www.skills.sh/owner/repo/../sql-tool',
    None,
])
def test_detail_rejects_unapproved_url_without_losing_listing_fields(key, url):
    saved = saved_state()
    saved['items'][0]['evidence']['metadata'][key] = url
    item = build_public_payload(saved, now=NOW)['tool_items'][0]
    assert item['url'] == CANONICAL
    assert item['public_metadata'] == [{'label': 'all-time rank', 'value': '7'},
                                       {'label': 'all-time installs', 'value': '1,234'}]
    assert 'PRIVATE_' not in str(item)


def test_www_is_not_approved_for_listing_identity():
    saved = saved_state()
    saved['items'][0]['url'] = REDIRECT
    assert build_public_payload(saved, now=NOW)['tool_items'] == []


@pytest.mark.parametrize('url', [CANONICAL, REDIRECT])
def test_both_approved_detail_hosts_bind_exact_skill_identity(url):
    saved = saved_state()
    saved['items'][0]['evidence']['metadata'].update(url=url, final_url=url)
    item = build_public_payload(saved, now=NOW)['tool_items'][0]
    assert {'label': 'Detail metadata status', 'value': 'ok'} in item['public_metadata']
    assert item['url'] == CANONICAL


def test_www_exception_does_not_broaden_github_detail_hosts():
    saved = saved_state()
    saved['sources'][0]['name'] = 'github-trending'
    row = saved['items'][0]
    row.update(id='github:owner/repo', source='github-trending', url='https://github.com/owner/repo')
    row['evidence']['metadata'].update(url=row['url'], final_url='https://www.github.com/owner/repo')
    item = build_public_payload(saved, now=NOW)['tool_items'][0]
    assert not item.get('public_metadata')
