"""Acceptance regressions using isolated DBs and deterministic HTTP only."""
import json
import sqlite3

import httpx
import pytest

from freellmapi_tracker.models import FreeProvider, ProviderStatus
from freellmapi_tracker.prober import AutoProber
from freellmapi_tracker.tracker import FreeProviderTracker


def router_tracker(tmp_path, rows):
    db = tmp_path / 'router.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE api_keys(id INTEGER, platform TEXT, label TEXT, status TEXT, enabled INTEGER)')
        conn.executemany('INSERT INTO api_keys VALUES (?, ?, ?, ?, ?)', [
            (91, 'groq', '', 'healthy', 1), (92, 'custom', '', 'healthy', 1),
            (93, 'custom', '', 'healthy', 0)])
        conn.execute('CREATE TABLE models(id INTEGER, platform TEXT, model_id TEXT, key_id INTEGER, enabled INTEGER, endpoint_scope TEXT)')
        conn.executemany('INSERT INTO models VALUES (?, ?, ?, ?, ?, ?)', rows)
    tracker = FreeProviderTracker(db, tmp_path / 'state.json')
    tracker.load_from_db()
    return tracker


def test_empty_literal_model_ids_are_not_eligible(tmp_path):
    tracker = router_tracker(tmp_path, [
        (1, 'groq', '', None, 1, ''), (2, 'groq', '   ', None, 1, ''),
        (3, 'groq', ' actual ', None, 1, ''),
        (4, 'groq', 'bad-binding', 'no quota headers exposed', 1, ''),
        (5, 'groq', 'wrong-key', 92, 1, '')])
    # A nonblank literal is never silently repaired/trimmed.
    assert tracker.load_probe_catalog() == {'groq': [' actual ']}


@pytest.mark.asyncio
async def test_db_binding_pins_exact_platform_when_http_catalog_lists_only_group_slugs(tmp_path):
    tracker = router_tracker(tmp_path, [(1, 'groq', 'vendor/model-original', None, 1, '')])
    calls = []
    def handler(request):
        calls.append(request.method)
        if request.method == 'GET':
            # Real v0.13.2 shape: no upstream platforms emitted, generic owner.
            return httpx.Response(200, json={'data': [
                {'id': 'model-group-slug', 'owned_by': 'freellmapi', 'available': True}]})
        payload = json.loads(request.content)
        assert payload['model'] == 'groq:vendor/model-original'
        assert payload['max_tokens'] == 5
        return httpx.Response(200, json={'choices': [{'message': {'content': '1'}}]},
                              headers={'X-Routed-Via': 'groq/vendor/model-original'})
    routes = tracker.load_probe_catalog(qualified=True)
    assert routes == {'groq': ['groq:vendor/model-original']}
    result = await AutoProber(api_key='fixture', bound_model_routes=routes,
        transport=httpx.MockTransport(handler)).probe_provider(tracker.providers['groq'])
    assert result.success and result.to_status()[0] == ProviderStatus.LIVE
    assert result.attempted_models == ['groq:vendor/model-original']
    assert calls == ['GET', 'POST']


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [{}, {'content': None}, {'content': ''},
    {'content': '   '}, {'content': []}, {'content': 1},
    {'tool_calls': [{'id': 'call-fixture'}]}])
async def test_empty_completion_never_proves_live(message):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'tiny', 'owned_by': 'groq'}]})
        return httpx.Response(200, json={'choices': [{'message': message}]},
                              headers={'X-Routed-Via': 'groq/tiny'})
    result = await AutoProber(api_key='fixture', transport=httpx.MockTransport(handler)).probe_provider(FreeProvider('Groq', 'groq'))
    assert not result.success
    assert result.response_category == 'invalid_response'
    assert result.to_status()[0] == ProviderStatus.UNKNOWN


@pytest.mark.asyncio
async def test_same_platform_wrong_model_does_not_verify_bound_candidate():
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'group', 'owned_by': 'freellmapi'}]})
        return httpx.Response(200, json={'choices': [{'message': {'content': '1'}}]},
                              headers={'X-Routed-Via': 'groq/different-model'})
    result = await AutoProber(api_key='fixture', bound_model_routes={'groq': ['groq:exact-model']},
        transport=httpx.MockTransport(handler)).probe_provider(FreeProvider('Groq', 'groq'))
    assert not result.success
    assert result.response_category == 'route_mismatch'
    assert result.to_status()[0] == ProviderStatus.UNKNOWN


def test_successful_completion_action_does_not_claim_free_entitlement(tmp_path):
    from freellmapi_tracker.models import ProviderProbeResult
    tracker = router_tracker(tmp_path, [(1, 'groq', 'tiny', None, 1, '')])
    result = ProviderProbeResult('groq', True, 'tiny', http_status=200)
    provider = tracker.apply_probe_result('groq', result)
    assert 'free tier active' not in provider.freellmapi_action.lower()
    assert 'entitlement' in provider.freellmapi_action.lower()


def test_custom_routes_are_endpoint_pinned_and_do_not_repair_foreign_bindings(tmp_path):
    tracker = router_tracker(tmp_path, [
        (1, 'custom', 'same/model', 92, 1, 'https://relay-fixture.example/v1/'),
        (2, 'custom', 'same/model', 93, 1, 'https://other-fixture.example/v1'),
        (3, 'custom', 'unscoped', 92, 1, ''),
        (4, 'custom', 'null-key', None, 1, 'https://relay-fixture.example/v1'),
        (5, 'custom', 'malformed', 'no quota headers exposed', 1, 'https://relay-fixture.example/v1'),
        (6, 'custom', 'disabled', 92, 0, 'https://relay-fixture.example/v1')])
    before = tracker.db_path.read_bytes()
    assert tracker.load_probe_catalog(qualified=True) == {
        'custom': ['custom:same/model#relay-fixture.example-v1']}
    assert tracker.db_path.read_bytes() == before
    assert not tracker.state_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize('routes', [{}, {'groq': []}, {'groq': ['google:tiny', 'groq:']}])
async def test_explicit_empty_or_wrong_platform_bindings_never_fall_back_to_http_aliases(routes):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={'data': [{'id': 'tiny', 'owned_by': 'groq'}]})
    result = await AutoProber(api_key='fixture', bound_model_routes=routes,
        transport=httpx.MockTransport(handler)).probe_provider(FreeProvider('Groq', 'groq'))
    assert calls == ['GET']
    assert result.attempted_models == []
    assert result.response_category == 'no_models'
    assert result.to_status()[0] == ProviderStatus.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize('routed,cache,category', [
    ('google/tiny', '', 'route_mismatch'), ('', '', 'route_unverified'),
    ('groq/tiny', 'HIT', 'cached_response'), ('groq/tiny', '', 'success')])
async def test_bound_route_still_requires_fresh_matching_success(routed, cache, category):
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [{'id': 'generic-group', 'owned_by': 'freellmapi'}]})
        assert json.loads(request.content)['model'] == 'groq:tiny'
        return httpx.Response(200, json={'choices': [{'message': {'content': '1'}}]},
                              headers={'X-Routed-Via': routed, 'X-FreeLLM-Cache': cache})
    result = await AutoProber(api_key='fixture', bound_model_routes={'groq': ['groq:tiny']},
        transport=httpx.MockTransport(handler)).probe_provider(FreeProvider('Groq', 'groq'))
    assert result.response_category == category
    assert result.success is (category == 'success')
    assert result.to_status()[0] == (ProviderStatus.LIVE if category == 'success' else ProviderStatus.UNKNOWN)


@pytest.mark.asyncio
async def test_generic_owned_by_without_db_binding_does_not_identify_upstream():
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={'data': [{'id': 'tiny', 'owned_by': 'freellmapi'}]})
    result = await AutoProber(api_key='fixture', transport=httpx.MockTransport(handler)).probe_provider(FreeProvider('Groq', 'groq', probe_model='tiny'))
    assert calls == ['GET']
    assert result.response_category == 'no_models'
    assert not result.success


@pytest.mark.asyncio
async def test_tracker_update_path_supplies_exact_db_bindings(tmp_path, monkeypatch):
    from freellmapi_tracker.models import ProviderProbeResult
    import freellmapi_tracker.tracker as tracker_module
    tracker = router_tracker(tmp_path, [(1, 'groq', 'tiny', None, 1, '')])
    received = []
    class CaptureProber:
        def __init__(self, **kwargs):
            received.append(kwargs)
        async def probe_provider(self, provider):
            return ProviderProbeResult(provider.platform, False, 'none', error_type='config')
    monkeypatch.setattr(tracker_module, 'AutoProber', CaptureProber)
    await tracker._probe_single(tracker.providers['groq'])
    assert received == [{'bound_model_routes': {'groq': ['groq:tiny']}}]


@pytest.mark.asyncio
@pytest.mark.parametrize('route', ['custom:tiny', 'custom:tiny#', 'custom:#endpoint'])
async def test_custom_probe_rejects_unqualified_or_empty_endpoint_members(route):
    calls = []
    def handler(request):
        calls.append(request.method)
        return httpx.Response(200, json={'data': [{'id': 'tiny', 'owned_by': 'freellmapi'}]})
    result = await AutoProber(api_key='fixture', bound_model_routes={'custom': [route]},
        transport=httpx.MockTransport(handler)).probe_provider(FreeProvider('Custom', 'custom'))
    assert calls == ['GET']
    assert result.attempted_models == []
    assert result.to_status()[0] == ProviderStatus.UNKNOWN







