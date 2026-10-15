"""Exercise the production NewsTracker.probe_providers path, not a helper."""
import hashlib
import json
import httpx
import pytest
import main
from freellmapi_tracker.prober import AutoProber
from freellmapi_tracker.models import ProviderStatus
from test_release_provider_bindings import router_tracker


@pytest.mark.asyncio
async def test_production_probe_uses_exact_db_member_not_http_group(tmp_path, monkeypatch):
    monkeypatch.setenv('NOTIFICATION_CHANNEL', 'none')
    monkeypatch.setenv('FREELLMAPI_API_KEY', 'fixture-only')
    provider_tracker = router_tracker(tmp_path, [(1, 'groq', 'vendor/member-original', None, 1, '')])
    before = hashlib.sha256(provider_tracker.db_path.read_bytes()).hexdigest()
    tracker = main.NewsTracker(data_dir=tmp_path / 'data', cache_dir=tmp_path / 'cache',
                               reports_dir=tmp_path / 'reports', freellmapi_url='https://router-fixture.example')
    tracker.provider_tracker = provider_tracker
    requested = []

    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data': [
                {'id': 'group-slug-only', 'owned_by': 'freellmapi', 'available': True}]})
        payload = json.loads(request.content)
        requested.append(payload['model'])
        assert payload['model'] == 'groq:vendor/member-original'
        return httpx.Response(200, json={'choices': [{'message': {'content': '1'}}]},
                              headers={'X-Routed-Via': 'groq/vendor/member-original'})

    def factory(**kwargs):
        return AutoProber(**kwargs, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(main, 'AutoProber', factory)
    updated = await tracker.probe_providers()
    assert requested == ['groq:vendor/member-original']
    assert updated >= 1
    assert provider_tracker.providers['groq'].status == ProviderStatus.LIVE
    assert hashlib.sha256(provider_tracker.db_path.read_bytes()).hexdigest() == before
