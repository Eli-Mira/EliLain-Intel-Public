"""Release audit regressions: read-only catalogs, no inference or credentials."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from intel_sources.catalogs import fetch_catalogs
from intel_sources.store import IntelStore


def fetch_payload(payload, source='unorouter-models', response_headers=None):
    def handler(request):
        assert request.method == 'GET'
        assert 'authorization' not in request.headers
        matches = (source == 'unorouter-models' and request.url.path == '/api/pricing/catalog') or (
            source == 'openrouter-models' and request.url.path == '/api/v1/models') or (
            source == 'openrouter-providers' and request.url.path == '/api/v1/providers')
        return httpx.Response(200, json=payload, headers=response_headers) if matches else httpx.Response(503)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await fetch_catalogs(client, include_local=False)
    return asyncio.run(run())


def test_unorouter_live_singular_embedding_type_is_retrieval_candidate():
    result = fetch_payload({'models': [{'model_name': 'embedding:free', 'type': 'embedding',
        'tags': ['Embedding', 'Files', '8.2K'], 'chat': False,
        'supported_endpoint_types': ['embedding', 'openai'], 'is_free': True, 'online': True}]})
    row = result['items'][0]
    assert 'embeddings' in row['project_types']
    assert row['evidence']['inference_verified'] is False
    assert row['evidence']['free_entitlement_verified'] is False


def test_openrouter_lifecycle_and_alias_changes_survive_persistence(tmp_path):
    raw = {'id': 'vendor/latest', 'name': 'Latest', 'created': 123,
           'knowledge_cutoff': '2026-01', 'expiration_date': '2026-10-05',
           'alias_target': {'slug': 'vendor/v1', 'name': 'V1'},
           'links': {'details': '/api/v1/models/vendor/latest/endpoints'}}
    first = fetch_payload({'data': [raw]}, 'openrouter-models')
    row = first['items'][0]
    for key in ('knowledge_cutoff', 'expiration_date', 'alias_target', 'links'):
        assert row['evidence'][key] == raw[key]
    store = IntelStore(tmp_path / 'state.json')
    assert store.update(first['items'], first['sources'])['changes'] == []
    for key, value in (('expiration_date', '2026-10-08'),
                       ('alias_target', {'slug': 'vendor/v2', 'name': 'V2'})):
        changed = deepcopy(raw)
        changed[key] = value
        second = fetch_payload({'data': [changed]}, 'openrouter-models')
        state = store.update(second['items'], second['sources'])
        assert state['changes'][0]['change_type'] == 'updated'
        assert state['changes'][0]['evidence'][key] == value


def test_unorouter_original_fixed_price_chat_and_release_metadata_are_not_lost():
    raw = {'model_name': 'image-model', 'type': 'image', 'tags': ['Image'],
           'fixed_price': 0.01788, 'original_fixed_price': 0.04, 'is_fixed_price': True,
           'chat': False, 'release_ts': 1788825600000}
    row = fetch_payload({'models': [raw]})['items'][0]
    assert row['evidence']['pricing']['original_fixed_price'] == 0.04
    assert row['evidence']['chat'] is False
    assert row['evidence']['release_ts'] == raw['release_ts']
    assert 'release_ts' not in row['fingerprint_data']
    assert row['fingerprint_data']['chat'] is False
    changed = deepcopy(raw)
    changed['original_fixed_price'] = 0.08
    assert fetch_payload({'models': [changed]})['items'][0]['fingerprint_data'] != row['fingerprint_data']


@pytest.mark.parametrize('source,container,identity', [
    ('openrouter-models', 'data', 'id'),
    ('openrouter-providers', 'data', 'slug'),
    ('unorouter-models', 'models', 'model_name'),
])
def test_duplicate_rows_are_partial_with_unique_accepted_coverage(source, container, identity):
    payload = {container: [{identity: 'same'}, {identity: 'same'}, {identity: 'good'}, None],
               'total_count': 4}
    result = fetch_payload(payload, source)
    observed = next(s for s in result['sources'] if s['name'] == source)
    assert observed['item_count'] == 2
    assert len(result['items']) == len({r['id'] for r in result['items']}) == 2
    assert observed['status'] == 'partial'
    assert observed['coverage']['returned_count'] == 4
    assert observed['coverage']['accepted_count'] == 2
    assert observed['coverage']['invalid_count'] == 1
    assert observed['coverage']['duplicate_count'] == 1
    assert observed['coverage']['declared_total'] == 4
    assert observed['coverage']['pagination_followed'] is False


def test_inconsistent_declared_total_is_partial_not_full_coverage():
    result = fetch_payload({'models': [{'model_name': 'one'}, {'model_name': 'two'}],
                            'counts': {'models': 1}})
    observed = next(s for s in result['sources'] if s['name'] == 'unorouter-models')
    assert observed['status'] == 'partial'
    assert 'inconsistent' in observed['error'].lower()


@pytest.mark.parametrize('bad', [
    {'tags': {'Text': True}}, {'tags': [5]}, {'is_free': 'false'},
    {'online': 1}, {'chat': 'false'}, {'is_fixed_price': 'false'},
    {'original_fixed_price': 'NaN'},
])
def test_unorouter_malformed_price_or_capability_rows_are_not_healthy(bad):
    result = fetch_payload({'models': [{'model_name': 'bad', **bad}, {'model_name': 'good'}]})
    observed = next(s for s in result['sources'] if s['name'] == 'unorouter-models')
    assert observed['status'] == 'partial'
    assert observed['item_count'] == 1
    assert [r['id'] for r in result['items']] == ['unorouter:model:good']


@pytest.mark.parametrize('pricing', [
    {'image_output': 'NaN'}, {'audio': 'not-price'}, {'internal_reasoning': True},
    {'input_cache_write_1h': 'Infinity'}, {'audio_output': []},
    {'overrides': [{'min_prompt_tokens': 10, 'completion': 'NaN'}]},
    {'overrides': 'invalid'}, {'overrides': [{'min_prompt_tokens': -1, 'prompt': '0.1'}]},
])
def test_openrouter_all_observed_price_dimensions_and_tiers_are_validated(pricing):
    result = fetch_payload({'data': [{'id': 'bad', 'pricing': pricing}, {'id': 'good'}]},
                           'openrouter-models')
    observed = next(s for s in result['sources'] if s['name'] == 'openrouter-models')
    assert observed['status'] == 'partial'
    assert observed['item_count'] == 1
    assert [r['id'] for r in result['items']] == ['openrouter:model:good']


@pytest.mark.parametrize('source,container,raw', [
    ('openrouter-models', 'data', {'id': 'vendor/model:free'}),
    ('unorouter-models', 'models', {'model_name': 'model:free'}),
])
def test_public_free_model_identities_are_not_lost_to_percent_encoded_colons(source, container, raw, tmp_path):
    from intel_sources.public_report import build_public_payload
    result = fetch_payload({container: [raw]}, source)
    store = IntelStore(tmp_path / 'state.json')
    state = store.update(result['items'], result['sources'])
    public = build_public_payload(state)
    assert [r['id'] for r in public['tool_items']] == [result['items'][0]['id']]
    assert ':free' in public['tool_items'][0]['url']
    assert '%' not in public['tool_items'][0]['url']


def test_reasoning_effort_membership_not_order_changes_fingerprint():
    raw = {'id': 'vendor/model', 'reasoning': {'mandatory': False,
           'supported_efforts': ['high', 'low'], 'default_effort': 'low'}}
    row = fetch_payload({'data': [raw]}, 'openrouter-models')['items'][0]
    changed = deepcopy(raw)
    changed['reasoning']['supported_efforts'].reverse()
    reordered = fetch_payload({'data': [changed]}, 'openrouter-models')['items'][0]
    assert row['fingerprint_data'] == reordered['fingerprint_data']
    changed['reasoning']['supported_efforts'].append('medium')
    assert fetch_payload({'data': [changed]}, 'openrouter-models')['items'][0]['fingerprint_data'] != row['fingerprint_data']
    assert row['evidence']['reasoning']['supported_efforts'] == ['high', 'low']


@pytest.mark.parametrize('failure', [{}, {'models': []}, {'success': False, 'models': [{'model_name': 'false-good'}]}])
def test_failed_catalog_retains_stale_history_without_inventing_changes(failure, tmp_path):
    now = datetime.now(timezone.utc)
    first = fetch_payload({'models': [{'model_name': 'good', 'online': True, 'is_free': True}]})
    store = IntelStore(tmp_path / 'state.json')
    baseline = store.update(first['items'], first['sources'], now=now)
    first_seen = baseline['items'][0]['first_seen']
    observation_time = baseline['items'][0]['checked_at']
    failed = fetch_payload(failure)
    retained = store.update(failed['items'], failed['sources'], now=now + timedelta(minutes=1))
    assert retained['changes'] == []
    assert len(retained['items']) == 1
    assert retained['items'][0]['freshness'] == 'stale'
    assert retained['items'][0]['checked_at'] == observation_time
    assert retained['items'][0]['first_seen'] == first_seen
    assert retained['items'][0]['not_observed_in_latest_fetch'] is True
    restored = store.update(first['items'], first['sources'], now=now + timedelta(minutes=2))
    assert restored['changes'] == []
    assert restored['items'][0]['freshness'] == 'fresh'
    assert not restored['items'][0].get('not_observed_in_latest_fetch')
    assert store.read(now=now + timedelta(hours=25))['items'][0]['freshness'] == 'stale'
    assert restored['items'][0]['evidence']['inference_verified'] is False


@pytest.mark.parametrize('age,policy,expected', [
    ('845', 'public, max-age=120, stale-while-revalidate=3600', 'exceeds-max-age'),
    ('100', 'public, max-age=120', 'within-max-age'),
    ('unsafe-cookie-value', 'max-age=invalid-token', 'unknown'),
])
def test_http_cache_age_is_separate_from_collection_and_inference_freshness(age, policy, expected):
    result = fetch_payload({'data': [{'id': 'vendor/model'}]}, 'openrouter-models',
                           {'Age': age, 'Cache-Control': policy})
    source = next(s for s in result['sources'] if s['name'] == 'openrouter-models')
    assert source['coverage']['http_cache_freshness'] == expected
    if expected != 'unknown':
        assert source['coverage']['http_cache_age_seconds'] == int(age)
        assert source['coverage']['http_cache_max_age_seconds'] == 120
    else:
        assert source['coverage']['http_cache_age_seconds'] is None
        assert source['coverage']['http_cache_max_age_seconds'] is None
        assert age not in str(result) and policy not in str(result)
    assert source['status'] == 'ok'  # Successful collection, not origin freshness.
    assert result['items'][0]['evidence']['inference_verified'] is False
