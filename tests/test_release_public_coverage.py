"""Public inventory completeness is distinct from small caller-selected exports."""
from collections import Counter
from datetime import datetime, timezone
import copy
import hashlib
import json
from pathlib import Path

import pytest

from intel_sources import public_report as public
from test_public_report import public_browser
from verification_fixtures import load_verification_inventory

NOW = datetime(2026, 10, 1, 12, tzinfo=timezone.utc)
OBSERVED = '2026-10-01T10:00:00+00:00'


def model(slug='org/model', **overrides):
    row = dict(id='openrouter:model:' + slug, source='openrouter-models',
               url='https://openrouter.ai/' + slug, headline=slug,
               checked_at=OBSERVED, first_seen=OBSERVED, evidence={}, project_types=['text'])
    row.update(overrides)
    return row


def state(rows):
    return dict(items=rows, sources=[dict(name=source, checked_at=OBSERVED, status='ok')
                for source in sorted({r['source'] for r in rows})])


def test_default_exports_complete_ordinary_inventory_but_explicit_cap_discloses_omissions():
    saved = state([model(f'org/model-{i:04}') for i in range(638)])
    original = copy.deepcopy(saved)
    complete = public.build_public_payload(saved, now=NOW)
    assert len(complete['tool_items']) == 638
    coverage = complete['source_items'][0]['coverage']
    assert 'Retained validated public inventory: 638 rows; exported: 638; omitted by caller cap: 0.' in coverage
    assert 'Complete validated saved inventory' in coverage
    capped = public.build_public_payload(saved, now=NOW, max_items_per_source=2)
    assert len(capped['tool_items']) == 2
    assert 'Retained validated public inventory: 638 rows; exported: 2; omitted by caller cap: 636.' in capped['source_items'][0]['coverage']
    assert saved == original


def test_complete_inventory_fails_closed_at_row_or_payload_bounds(monkeypatch):
    saved = state([model(f'org/model-{i}') for i in range(3)])
    monkeypatch.setattr(public, 'MAX_PUBLIC_ITEMS', 2, raising=False)
    with pytest.raises(ValueError, match='inventory.*limit'):
        public.build_public_payload(saved, now=NOW)
    monkeypatch.setattr(public, 'MAX_PUBLIC_ITEMS', 100, raising=False)
    monkeypatch.setattr(public, 'MAX_PUBLIC_PAYLOAD_BYTES', 100, raising=False)
    with pytest.raises(ValueError, match='payload.*size'):
        public.build_public_payload(saved, now=NOW)


def test_standalone_size_guard_is_measured_in_utf8_bytes_before_writes(tmp_path, monkeypatch):
    saved = tmp_path / 'state.json'
    saved.write_text(json.dumps(state([model()])), encoding='utf-8')
    destination = tmp_path / 'public'
    destination.mkdir()
    previous = destination / 'index.html'
    previous.write_bytes(b'previous safe artifact')
    monkeypatch.setattr(public, 'MAX_PUBLIC_HTML_BYTES', 100, raising=False)
    with pytest.raises(ValueError, match='HTML.*size'):
        public.export_public_report(saved, tmp_path / 'missing.db', destination, now=NOW)
    assert previous.read_bytes() == b'previous safe artifact'
    assert list(destination.iterdir()) == [previous]


def test_standalone_bound_includes_csp_and_non_ascii_utf8(monkeypatch):
    html = '<html><head><meta charset="UTF-8"></head><body>é</body></html>'
    monkeypatch.setattr(public, 'MAX_PUBLIC_HTML_BYTES', len(html.encode('utf-8')))
    with pytest.raises(ValueError, match='HTML.*size'):
        public._standalone_html(html)


def test_openrouter_tilde_alias_has_exact_public_identity_binding():
    alias = '~vendor/model-latest'
    payload = public.build_public_payload(state([model(alias)]), now=NOW)
    assert [r['id'] for r in payload['tool_items']] == ['openrouter:model:' + alias]
    assert payload['tool_items'][0]['url'] == 'https://openrouter.ai/' + alias


@pytest.mark.parametrize('slug,url', [
    ('vendor/~model', 'https://openrouter.ai/vendor/~model'),
    ('~~vendor/model', 'https://openrouter.ai/~~vendor/model'),
    ('~vendor/model', 'https://evil.example/~vendor/model'),
    ('~vendor/model', 'https://openrouter.ai/vendor/model'),
    ('~vendor/model', 'https://openrouter.ai/~vendor/model?api_key=PRIVATE_CANARY'),
    ('~vendor/model', 'https://openrouter.ai/%7Evendor/model'),
    ('~vendor/..', 'https://openrouter.ai/~vendor/..'),
    ('~vendor/sk-PRIVATE_CANARY', 'https://openrouter.ai/~vendor/sk-PRIVATE_CANARY'),
])
def test_tilde_policy_never_allows_wrong_path_host_credential_or_escape(slug, url):
    assert public.build_public_payload(state([model(slug, url=url)]), now=NOW)['tool_items'] == []


def test_typed_catalog_lifecycle_alias_capabilities_survive_without_cached_prose():
    evidence = {'expiration_date': '2026-10-05', 'knowledge_cutoff': '2025-09-30',
        'alias_target': {'slug': 'vendor/model:free', 'name': 'PRIVATE_ALIAS_NAME_CANARY'},
        'reasoning': {'mandatory': True, 'default_enabled': False, 'supports_max_tokens': True,
                      'supported_efforts': ['high', 'xhigh'], 'default_effort': 'high'},
        'supported_parameters': ['tools', 'parallel_tool_calls', 'verbosity', 'logit_bias', 'top_a'],
        'description': 'PRIVATE_DESCRIPTION_CANARY', 'links': {'details': 'http://localhost/PRIVATE_CANARY'}}
    payload = public.build_public_payload(state([model('~vendor/model-latest', evidence=evidence)]), now=NOW)
    fields = {v['label']: v['value'] for v in payload['tool_items'][0]['public_metadata']}
    assert fields['Catalog expiration date'] == '2026-10-05'
    assert fields['Catalog knowledge cutoff'] == '2025-09-30'
    assert fields['Catalog alias target'] == 'vendor/model:free'
    assert fields['Reasoning mandatory'] == 'true'
    assert fields['Reasoning default enabled'] == 'false'
    assert fields['Reasoning supports max tokens'] == 'true'
    assert fields['Reasoning efforts'] == 'high, xhigh'
    assert fields['Reasoning default effort'] == 'high'
    assert fields['Supported parameters'] == 'logit_bias, parallel_tool_calls, tools, top_a, verbosity'
    assert 'PRIVATE_' not in str(payload) and 'localhost' not in str(payload)


@pytest.mark.parametrize('bad', ['2026-02-30', '2026-13-01', 'PRIVATE_DATE_CANARY', {}, True])
def test_lifecycle_rejects_invalid_dates_and_private_alias_target(bad):
    evidence = dict(expiration_date=bad, knowledge_cutoff=bad,
                    alias_target={'slug': 'vendor/sk-PRIVATE_CANARY', 'name': 'PRIVATE_CANARY'})
    payload = public.build_public_payload(state([model(evidence=evidence)]), now=NOW)
    assert not payload['tool_items'][0].get('public_metadata')


def test_all_known_price_dimensions_keep_exact_amounts_and_dimension_specific_units():
    pricing = {key: '0.0000001234567890123' for key in ('prompt', 'completion', 'input_cache_read',
        'input_cache_write', 'input_cache_write_1h', 'audio', 'audio_output', 'image_token',
        'input_audio_cache', 'internal_reasoning')}
    pricing.update(image='0.1234567890123', image_output='0.25', request='0.5', web_search='0.75')
    payload = public.build_public_payload(state([model(evidence={'pricing': pricing})]), now=NOW)
    fields = {v['label']: v['value'] for v in payload['tool_items'][0]['public_metadata']}
    assert fields['Listed input price'] == 'USD 0.1234567890123 / million tokens'
    for label in ('Listed output price', 'Listed cache read price', 'Listed cache write price',
                  'Listed 1h cache write price', 'Listed audio input price', 'Listed audio output price',
                  'Listed image token price', 'Listed audio cache price', 'Listed internal reasoning price'):
        assert fields[label] == 'USD 0.1234567890123 / million tokens'
    assert fields['Listed input image price'] == 'USD 0.1234567890123 / input image'
    assert fields['Listed output image price'] == 'USD 0.25 / output image'
    assert fields['Listed request price'] == 'USD 0.5 / request'
    assert fields['Listed web search price'] == 'USD 0.75 / web search'


def test_unorouter_embedding_chat_and_original_reference_prices_are_public_typed_fields():
    row = model(id='unorouter:model:model', source='unorouter-models', url='https://unorouter.com/en/models/model',
                evidence={'type': 'embedding', 'chat': False, 'pricing': {'is_fixed_price': False,
                'input_price': '0.1234567890123', 'output_price': 2,
                'original_input_price': 1, 'original_output_price': 10}})
    fields = {v['label']: v['value'] for v in public.build_public_payload(state([row]), now=NOW)['tool_items'][0]['public_metadata']}
    assert fields['Model type'] == 'embedding'
    assert fields['Catalog chat flag'] == 'false'
    assert fields['Listed input price'] == 'USD 0.1234567890123 / million tokens'
    assert fields['Catalog original input price'] == 'USD 1 / million tokens'
    assert fields['Catalog original output price'] == 'USD 10 / million tokens'


def test_current_public_capability_enums_are_not_silently_lost():
    rows = [model(evidence={'supported_parameters': ['prediction'],
        'architecture': {'output_modalities': ['decisions']}}),
        model(id='unorouter:model:model', source='unorouter-models', url='https://unorouter.com/en/models/model',
        evidence={'tags': ['Embedding', 'Open Weights', 'Task', 'Deprecated'],
          'supported_endpoint_types': ['embedding', 'image-generation', 'aihorde', 'openai-alpha-search',
            'openai-response', 'openai-response-compact', 'typesafe-decisions']})]
    projected = public.build_public_payload(state(rows), now=NOW)['tool_items']
    fields = [{m['label']: m['value'] for m in r['public_metadata']} for r in projected]
    assert fields[0]['Supported parameters'] == 'prediction'
    assert fields[0]['Output modalities'] == 'decisions'
    assert fields[1]['Catalog tags'] == 'Deprecated, Embedding, Open Weights, Task'
    assert fields[1]['Endpoint formats'] == 'aihorde, embedding, image-generation, openai-alpha-search, openai-response, openai-response-compact, typesafe-decisions'


def test_detail_description_field_is_fixed_provenance_not_arbitrary_cached_text():
    row = model(id='skills:org/repo/skill', source='skills-sh', url='https://skills.sh/org/repo/skill',
        evidence={'metadata': {'description_field': 'meta[name=description]'}})
    payload = public.build_public_payload(state([row]), now=NOW)
    assert payload['tool_items'][0]['public_metadata'] == [{'label': 'Detail description field', 'value': 'meta[name=description]'}]
    row['evidence']['metadata']['description_field'] = 'PRIVATE_FIELD_CANARY'
    assert not public.build_public_payload(state([row]), now=NOW)['tool_items'][0].get('public_metadata')


def test_detail_observation_provenance_is_typed_and_does_not_refresh_listing():
    old = '2026-09-01T10:00:00+00:00'
    row = model(id='skills:org/repo/skill', source='skills-sh', url='https://skills.sh/org/repo/skill',
        checked_at=old, evidence={'metadata': {'status': 'ok', 'checked_at': OBSERVED,
        'url': 'https://skills.sh/org/repo/skill', 'final_url': 'https://skills.sh/org/repo/skill',
        'http_status': 200, 'sha256': 'a' * 64, 'description_available': True, 'topics_available': False,
        'description': 'PRIVATE_DESCRIPTION_CANARY', 'error': 'PRIVATE_ERROR_CANARY', 'api_key': 'PRIVATE_KEY_CANARY'}})
    item = public.build_public_payload(state([row]), now=NOW)['tool_items'][0]
    fields = {v['label']: v['value'] for v in item['public_metadata']}
    assert fields['Detail metadata status'] == 'ok'
    assert fields['Detail observed at'] == OBSERVED
    assert fields['Detail HTTP status'] == '200'
    assert fields['Detail body SHA-256'] == 'a' * 64
    assert fields['Detail description available'] == 'true'
    assert fields['Detail topics available'] == 'false'
    assert item['freshness'] == 'stale'
    assert item['evidence'][0]['observed_at'] == old
    assert 'PRIVATE_' not in str(item)


def test_detail_provenance_rejects_wrong_host_and_untyped_fields():
    row = model(id='github:org/repo', source='github-trending', url='https://github.com/org/repo',
        evidence={'metadata': {'status': {'private': 'PRIVATE_CANARY'}, 'http_status': True,
        'sha256': 'PRIVATE_HASH_CANARY', 'checked_at': '2099-01-01T00:00:00Z',
        'final_url': 'https://evil.example/org/repo', 'description_available': 'PRIVATE_BOOL_CANARY'}})
    item = public.build_public_payload(state([row]), now=NOW)['tool_items'][0]
    assert not item.get('public_metadata')


def test_source_catalog_and_detail_coverage_survives_as_typed_counts_not_errors():
    saved = state([model()])
    saved['sources'][0]['coverage'] = {'scope': 'public-catalog', 'returned_count': 638,
        'accepted_count': 637, 'invalid_count': 1, 'duplicate_count': 0, 'declared_total': 638,
        'has_more': False, 'pagination_followed': False, 'output_modalities': 'all',
        'http_cache_age_seconds': 143, 'http_cache_max_age_seconds': 120,
        'http_cache_freshness': 'exceeds-max-age', 'error': 'PRIVATE_COVERAGE_CANARY',
        'metadata': {'budget': 40, 'attempted': 40, 'enriched': 37, 'empty': 1, 'errors': 2,
        'not_scanned': 414, 'scope': 'PRIVATE_META_SCOPE_CANARY'}}
    coverage = public.build_public_payload(saved, now=NOW)['source_items'][0]['coverage']
    for text in ('returned: 638', 'accepted: 637', 'invalid: 1', 'duplicate: 0', 'declared total: 638',
        'has more: false', 'pagination followed: false', 'output modalities: all',
        'HTTP cache age: 143 seconds', 'HTTP cache max age: 120 seconds', 'HTTP cache freshness: exceeds-max-age',
        'detail budget: 40', 'detail attempted: 40', 'detail enriched: 37', 'detail errors: 2', 'detail not scanned: 414'):
        assert text in coverage
    assert 'PRIVATE_' not in coverage


def test_inventory_without_source_row_has_unverified_coverage_not_missing_counts():
    payload = public.build_public_payload({'items': [model()], 'sources': []}, now=NOW)
    assert len(payload['source_items']) == 1
    assert payload['source_items'][0]['status'] == 'unverified'
    assert payload['source_items'][0]['checked_at'] is None
    assert payload['source_items'][0]['item_count'] == 1
    assert payload['tool_items'][0]['freshness'] == 'stale'


@pytest.mark.parametrize('scope', ['key-accessible', 'local-aggregated'])
def test_private_source_scope_cannot_be_authorized_by_public_name(scope):
    saved = state([model()])
    saved['sources'][0]['coverage'] = {'scope': scope}
    payload = public.build_public_payload(saved, now=NOW)
    assert payload['tool_items'] == payload['source_items'] == []


def test_source_coverage_reconciles_policy_rejections_and_duplicates_separately_from_cap():
    saved = state([model(), model(), model('vendor/other', url='https://evil.example/vendor/other')])
    coverage = public.build_public_payload(saved, now=NOW)['source_items'][0]['coverage']
    assert 'Saved source rows: 3; excluded by public policy or duplicate identity: 2.' in coverage
    assert 'Retained validated public inventory: 1 rows; exported: 1; omitted by caller cap: 0.' in coverage


@pytest.mark.parametrize('width', [390, 1440])
@pytest.mark.parametrize('javascript', [True, False])
def test_real_saved_inventory_full_projection_reaches_offline_dom(public_browser, tmp_path, width, javascript):
    root = Path(__file__).resolve().parents[1]
    saved_path = load_verification_inventory(root)
    before = hashlib.sha256(saved_path.read_bytes()).hexdigest()
    saved = json.loads(saved_path.read_text(encoding='utf-8'))
    payload = public.build_public_payload(saved, now=NOW)
    expected = Counter(r['source'] for r in payload['tool_items'])
    assert 0 < sum(expected.values()) <= public.MAX_PUBLIC_ITEMS
    artifact = public.export_public_report(saved_path, tmp_path / 'missing.db', tmp_path / 'public', now=NOW)
    assert artifact.stat().st_size <= public.MAX_PUBLIC_HTML_BYTES
    context = public_browser.new_context(viewport={'width': width, 'height': 900}, offline=True,
                                         java_script_enabled=javascript)
    requests, errors = [], []
    try:
        page = context.new_page()
        page.on('request', lambda request: requests.append(request.url))
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.on('console', lambda message: errors.append(message.text) if message.type == 'error' else None)
        page.goto(artifact.as_uri(), wait_until='load')
        assert Counter(page.locator('.discovery-item').evaluate_all('(rows) => rows.map(r => r.dataset.source)')) == expected
        assert page.locator('.discovery-item:visible').count() == 0
        page.locator('#discovery-inventory > summary').click()
        assert page.locator('.discovery-item:visible').count() == sum(expected.values())
        assert page.locator('.public-metadata li').count() == sum(len(r.get('public_metadata', [])) for r in payload['tool_items'])
        coverage = page.locator('#source-health').inner_text()
        for row in payload['source_items']:
            assert row['coverage'] in coverage
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        if javascript:
            for source, count in expected.items():
                page.get_by_label('Discovery source', exact=True).select_option(source)
                assert page.locator('.discovery-item:visible').count() == count
            page.get_by_label('Discovery source', exact=True).select_option('')
            last = payload['tool_items'][-1]['headline']
            page.get_by_label('Search discoveries', exact=True).fill(last)
            assert page.locator('.discovery-item:visible').count() > 0
        else:
            assert page.get_by_label('Discovery source', exact=True).is_disabled()
            page.locator('details.evidence summary').first.click()
            assert page.locator('details.evidence').first.get_attribute('open') is not None
        assert requests == [artifact.as_uri()]
        assert not errors
    finally:
        context.close()
    assert before == hashlib.sha256(saved_path.read_bytes()).hexdigest()
