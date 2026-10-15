import httpx
import pytest

from freellmapi_tracker.models import FreeProvider, ProviderStatus
from freellmapi_tracker.prober import AutoProber


@pytest.mark.asyncio
async def test_missing_unified_auth_is_configuration_warning_not_broken_provider(monkeypatch, caplog):
    monkeypatch.delenv("FREELLMAPI_API_KEY", raising=False)
    prober = AutoProber()
    assert not prober.api_key
    result = await prober.probe_provider(FreeProvider("Groq", "groq"))
    assert result.error_type == "config"
    assert result.response_category == "router_auth"
    assert result.to_status()[0] == ProviderStatus.UNKNOWN
    assert not result.attempted_models
    assert "WARNING" in caplog.text and "unified" in caplog.text.lower()


@pytest.mark.asyncio
async def test_probe_uses_actual_catalog_fallback_and_records_route_evidence():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-unified"
        if request.method == "GET":
            return httpx.Response(200, json={"data": [
                {"id": "removed", "owned_by": "groq"},
                {"id": "actual-tiny", "owned_by": "groq"},
                {"id": "other-provider", "owned_by": "google"}]})
        import json
        model = json.loads(request.content)["model"]
        if model == "removed":
            return httpx.Response(404, json={"error": {"message": "Model not found"}})
        assert model == "actual-tiny"
        assert request.headers["X-FreeLLM-Cache"] == "off"
        return httpx.Response(200, json={"choices": [{"message": {"content": "1"}}]},
                              headers={"X-Routed-Via": "groq/actual-tiny"})
    prober = AutoProber(api_key="fixture-unified", transport=httpx.MockTransport(handler))
    result = await prober.probe_provider(FreeProvider("Groq", "groq", probe_model="removed"))
    assert result.success
    assert result.to_status()[0] == ProviderStatus.LIVE
    assert result.model_tested == "actual-tiny"
    assert result.attempted_models == ["removed", "actual-tiny"]
    assert result.response_category == "success"
    assert [e["http_status"] for e in result.evidence] == [404, 200]
    assert result.evidence[-1]["routed_via"] == "groq/actual-tiny"
    assert result.checked_at.tzinfo is not None
    assert len(requests) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("headers,body,category", [
    ({"X-Routed-Via": "google/tiny"}, {"choices": [{"message": {"content": "1"}}]}, "route_mismatch"),
    ({}, {"choices": [{"message": {"content": "1"}}]}, "route_unverified"),
    ({"X-Routed-Via": "groq/tiny", "X-FreeLLM-Cache": "hit"}, {"choices": [{"message": {"content": "1"}}]}, "cached_response"),
    ({"X-Routed-Via": "groq/tiny"}, {"error": "not a completion"}, "invalid_response"),
])
async def test_http_200_without_fresh_same_provider_completion_is_not_live(headers, body, category):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "tiny", "owned_by": "groq"}]})
        return httpx.Response(200, json=body, headers=headers)
    prober = AutoProber(api_key="fixture", transport=httpx.MockTransport(handler))
    result = await prober.probe_provider(FreeProvider("Groq", "groq"))
    assert not result.success
    assert result.response_category == category
    assert result.to_status()[0] == ProviderStatus.UNKNOWN


@pytest.mark.asyncio
@pytest.mark.parametrize("code,message,category,status", [
    (401, "Invalid upstream API key", "auth", ProviderStatus.BROKEN_KEY),
    (403, "Forbidden", "auth", ProviderStatus.BROKEN_KEY),
    (402, "Credits exhausted", "quota", ProviderStatus.EXHAUSTED),
    (429, "Too many requests; add more API keys", "rate_limit", ProviderStatus.DEGRADED),
    (429, "Daily quota exhausted", "quota", ProviderStatus.EXHAUSTED),
    (404, "Model not found", "model_not_found", ProviderStatus.UNKNOWN),
    (503, "Upstream authentication failed temporarily", "server_error", ProviderStatus.DEGRADED),
    (401, "Invalid unified API key", "router_auth", ProviderStatus.UNKNOWN),
    (429, "Rate limited, invalid API key in unrelated diagnostic", "rate_limit", ProviderStatus.DEGRADED),
])
async def test_completion_error_categories_do_not_confuse_key_quota_or_model(code, message, category, status):
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "tiny", "owned_by": "groq"}]})
        return httpx.Response(code, json={"error": {"message": message}})
    result = await AutoProber(api_key="fixture", transport=httpx.MockTransport(handler)).probe_provider(FreeProvider("Groq", "groq"))
    assert result.response_category == category
    assert result.error_type == category
    assert result.to_status()[0] == status
    assert result.attempted_models == ["tiny"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,category,level", [(401, "router_auth", "WARNING"), (503, "router_error", "ERROR"), ("offline", "network", "ERROR"), ("timeout", "timeout", "ERROR"), ("malformed", "invalid_catalog", "WARNING")])
async def test_router_failures_return_unknown_for_every_provider(failure, category, level, caplog):
    posts = []
    def handler(request):
        posts.append(request)
        if failure == "offline":
            raise httpx.ConnectError("secret URL https://host/?token=fixture-leak", request=request)
        if failure == "timeout":
            raise httpx.ReadTimeout("timed out", request=request)
        if failure == "malformed":
            return httpx.Response(200, text="not JSON")
        return httpx.Response(failure, json={"error": {"message": "router rejected request"}})
    providers = [FreeProvider("Groq", "groq"), FreeProvider("Google", "google")]
    results = await AutoProber(api_key="fixture", transport=httpx.MockTransport(handler)).probe_all(providers, delay_between=0)
    assert set(results) == {"groq", "google"}
    for result in results.values():
        assert result.to_status()[0] == ProviderStatus.UNKNOWN
        assert result.response_category == category
        assert not result.attempted_models
        assert result.evidence[0]["phase"] == "catalog"
    assert all(request.method == "GET" for request in posts)
    assert level in caplog.text
    assert "fixture-leak" not in caplog.text


@pytest.mark.asyncio
async def test_disabled_provider_is_never_requested_even_if_probe_flag_true():
    def handler(request):
        raise AssertionError("Disabled provider must not send requests")
    p = FreeProvider("Groq", "groq", status=ProviderStatus.DISABLED, probe_enabled=True)
    prober = AutoProber(api_key="fixture", transport=httpx.MockTransport(handler))
    assert await prober.probe_all([p], delay_between=0) == {}
    result = await prober.probe_provider(p)
    assert result.response_category == "disabled"
    assert result.to_status()[0] == ProviderStatus.DISABLED


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,category", [("network", "network"), ("timeout", "timeout"), ("quota_code", "quota"), ("string_error", "auth")])
async def test_completion_failure_evidence_is_sanitized_and_uses_structured_categories(mode, category):
    import json
    secret = "opaque-unified-fixture"
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "tiny", "owned_by": "groq"}]})
        if mode == "network":
            raise httpx.ConnectError(f"connection failure {secret} https://host/a?api_key=opaque-url-fixture", request=request)
        if mode == "timeout":
            raise httpx.ReadTimeout(f"timeout {secret}", request=request)
        if mode == "quota_code":
            return httpx.Response(429, json={"error": {"code": "insufficient_quota", "message": f"request rejected {secret}"}})
        return httpx.Response(401, json={"error": f"invalid key {secret}"})
    result = await AutoProber(api_key=secret, transport=httpx.MockTransport(handler)).probe_provider(FreeProvider("Groq", "groq"))
    assert result.response_category == category
    assert secret not in result.error_message
    assert "opaque-url-fixture" not in json.dumps(result.to_dict())
    assert result.evidence[-1]["category"] == category
    assert result.evidence[-1]["phase"] == "completion"
    assert result.evidence[-1]["checked_at"] == result.checked_at.isoformat()
    assert result.evidence[-1]["response_time_ms"] >= 0
    if mode in ("network", "timeout"):
        assert result.to_status()[0] == ProviderStatus.UNKNOWN


@pytest.mark.asyncio
async def test_reflected_unified_key_is_not_selected_as_a_catalog_model():
    import json
    secret = "opaque-catalog-fixture"
    def handler(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": secret, "owned_by": "groq"}]})
        return httpx.Response(404)
    result = await AutoProber(api_key=secret, transport=httpx.MockTransport(handler)).probe_provider(FreeProvider("Groq", "groq"))
    assert secret not in json.dumps(result.to_dict())
    assert result.attempted_models == []
    assert result.to_status()[0] == ProviderStatus.UNKNOWN


@pytest.mark.asyncio
async def test_malformed_catalog_provider_metadata_is_unknown_not_silently_lost():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "tiny", "platforms": None}]})
    result = await AutoProber(api_key="fixture", transport=httpx.MockTransport(handler)).probe_all([FreeProvider("Groq", "groq")], delay_between=0)
    assert "groq" in result
    assert result["groq"].response_category == "invalid_catalog"
    assert result["groq"].to_status()[0] == ProviderStatus.UNKNOWN


@pytest.mark.asyncio
async def test_db_model_mapping_handles_real_freellmapi_owned_by_envelope(tmp_path):
    import sqlite3
    import json
    from freellmapi_tracker.tracker import FreeProviderTracker
    db = tmp_path/'router.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE api_keys(id INTEGER, platform TEXT, label TEXT, status TEXT, enabled INTEGER)')
        conn.execute("INSERT INTO api_keys VALUES (91, 'groq', 'test', 'healthy', 1)")
        conn.execute('CREATE TABLE models(platform TEXT, model_id TEXT, key_id INTEGER, enabled INTEGER)')
        conn.executemany('INSERT INTO models VALUES (?,?,?,?)', [('groq','actual',91,1), ('groq','wrong-key',92,1), ('groq','off',91,0), ('google','unrelated',None,1)])
    tracker = FreeProviderTracker(db, tmp_path/'state.json')
    tracker.load_from_db()
    mapping = tracker.load_probe_catalog()
    assert mapping == {'groq': ['actual']}
    posts = []
    def handler(request):
        if request.method == 'GET':
            return httpx.Response(200, json={'data':[{'id':'actual','owned_by':'freellmapi','available':True}, {'id':'wrong-key','owned_by':'freellmapi'}, {'id':'unrelated','owned_by':'freellmapi'}]})
        posts.append(json.loads(request.content)['model'])
        return httpx.Response(200,json={'choices':[{'message':{'content':'1'}}]},headers={'X-Routed-Via':'groq/actual'})
    prober = AutoProber(api_key='fixture', model_catalog=mapping, transport=httpx.MockTransport(handler))
    result = await prober.probe_provider(tracker.providers['groq'])
    assert result.success and posts == ['actual']
    absent = FreeProviderTracker(tmp_path/'absent.db', tmp_path/'absent-state.json')
    assert absent.load_probe_catalog() == {}
    assert not absent.db_path.exists()


@pytest.mark.asyncio
async def test_gateway_available_false_is_not_provider_inference_proof():
    requests = []
    def handler(request):
        requests.append(request)
        if request.method == 'GET':
            return httpx.Response(200,json={'data':[{'id':'actual','owned_by':'freellmapi','available':False}]})
        return httpx.Response(429,json={'error':{'message':'Daily quota exhausted'}})
    result = await AutoProber(api_key='fixture', model_catalog={'groq':['actual']}, transport=httpx.MockTransport(handler)).probe_provider(FreeProvider('Groq','groq'))
    assert len(requests) == 2
    assert result.attempted_models == ['actual']
    assert result.to_status()[0] == ProviderStatus.EXHAUSTED
