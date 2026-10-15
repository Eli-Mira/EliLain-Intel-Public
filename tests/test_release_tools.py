"""Customer-release regressions over real adapters; never install discovered code."""
import httpx
import pytest
from intel_sources import tools


def leaderboard():
    return '<title>Skills</title><a href="/owner/repo/sql-tool"><span>1</span><h3>sql-tool</h3><p>owner/repo</p><span>42</span></a>'


async def fetch(handler, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await tools.fetch_tools(client, github_periods=(), **kwargs)


@pytest.mark.asyncio
async def test_empty_detail_is_not_useful_enrichment():
    def handler(request):
        if request.url.path in ('/', '/hot', '/trending'):
            return httpx.Response(200, text=leaderboard())
        return httpx.Response(200, text='<meta name="description" content="Install the sql-tool skill"><main><h1>sql-tool</h1></main>')
    result = await fetch(handler)
    metadata = result['sources'][0]['coverage']['metadata']
    assert metadata['attempted'] == 1
    assert result['sources'][0]['coverage']['representation'] == 'server-rendered-initial-html'
    assert 'hydrated' in result['sources'][0]['coverage']['scope']
    assert metadata['enriched'] == 0
    assert metadata['empty'] == 1
    assert result['items'][0]['evidence']['metadata']['status'] == 'empty'
    assert result['items'][0]['summary'] == ''


@pytest.mark.asyncio
async def test_detail_redirect_cannot_attach_another_skill_capability():
    def handler(request):
        if request.url.path in ('/', '/hot', '/trending'):
            return httpx.Response(200, text=leaderboard())
        if request.url.path == '/owner/repo/sql-tool':
            return httpx.Response(307, headers={'Location': '/another/repo/other'})
        return httpx.Response(200, text='<meta name="description" content="Android game engine"><main><h1>other</h1></main>')
    result = await fetch(handler)
    row = result['items'][0]
    assert row['summary'] == ''
    assert row['evidence']['metadata']['status'] == 'error'
    assert 'identity' in row['evidence']['metadata']['error'].lower()
    assert result['sources'][0]['coverage']['metadata']['enriched'] == 0


@pytest.mark.asyncio
async def test_api_bearer_is_not_forwarded_to_redirected_origin():
    requested = []
    def handler(request):
        requested.append(request)
        if request.url.path.startswith('/api/'):
            return httpx.Response(307, headers={'Location': 'https://www.skills.sh/api/v1/skills'})
        if request.url.host == 'www.skills.sh':
            return httpx.Response(401)
        return httpx.Response(503)
    await fetch(handler, skills_token='release-test-token')
    leaked = [r for r in requested if r.url.host == 'www.skills.sh' and 'authorization' in r.headers]
    assert leaked == []


@pytest.mark.asyncio
@pytest.mark.parametrize('suffix', ['?access_token=untrusted-marker', '#other-identity'])
async def test_skill_identity_does_not_silently_normalize_query_or_fragment(suffix):
    result = await fetch(lambda request: httpx.Response(200, text=leaderboard().replace('/owner/repo/sql-tool', '/owner/repo/sql-tool' + suffix)))
    assert result['items'] == []
    assert result['sources'][0]['status'] == 'error'
    assert 'untrusted-marker' not in str(result)


@pytest.mark.asyncio
async def test_public_detail_provenance_survives_store_without_forging_github_metadata(tmp_path):
    import hashlib
    from datetime import datetime, timezone
    from intel_sources.store import IntelStore
    detail = '<meta name="description" content="SQL analytics"><main><h1>sql-tool</h1><a href="/topic/data">Data</a></main>'
    def handler(request):
        if request.url.path in ('/', '/hot', '/trending'):
            return httpx.Response(200, text=leaderboard())
        if request.url.host == 'skills.sh':
            return httpx.Response(307, headers={'Location': 'https://www.skills.sh/owner/repo/sql-tool'})
        return httpx.Response(200, text=detail)
    result = await fetch(handler)
    state = IntelStore(tmp_path / 'intel.json').update(result['items'], result['sources'])
    row = state['items'][0]
    metadata = row['evidence']['metadata']
    assert metadata['url'] == row['url']
    assert metadata['final_url'] == 'https://www.skills.sh/owner/repo/sql-tool'
    assert metadata['sha256'] == hashlib.sha256(detail.encode()).hexdigest()
    assert metadata['http_status'] == 200
    assert datetime.fromisoformat(metadata['checked_at']) <= datetime.now(timezone.utc)
    assert row['summary'] == 'SQL analytics'
    assert 'data' in row['project_types']
    assert 'official_metadata' not in row['evidence']  # Skills.sh is not the GitHub API.


@pytest.mark.asyncio
async def test_detail_budget_reconciles_empty_useful_and_unscanned(monkeypatch):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', 2)
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path in ('/', '/hot', '/trending'):
            return httpx.Response(200, text=''.join(leaderboard().replace('sql-tool', f'tool-{i}') for i in range(4)))
        if request.url.path.endswith('tool-0'):
            return httpx.Response(200, text='<main><h1>tool-0</h1><a href="/topic/data">SQL data</a><script>never execute</script></main>')
        return httpx.Response(200, text='<main><h1>tool-1</h1></main>')
    result = await fetch(handler)
    report = result['sources'][0]['coverage']['metadata']
    assert report['attempted'] == report['enriched'] + report['empty'] + report['errors'] == 2
    assert report['not_scanned'] == 2
    assert report['descriptions'] == 0 and report['topics_available'] == 1
    assert len(requests) == 5
    assert 'data' in result['items'][0]['project_types']
    assert result['items'][1]['evidence']['metadata']['status'] == 'empty'
    assert result['items'][2]['evidence']['metadata']['status'] == 'not-scanned'
    assert all(request.url.host == 'skills.sh' for request in requests)
    assert 'never execute' not in str(result)


@pytest.mark.asyncio
async def test_adapter_failure_preserves_old_state_as_stale_not_fresh_success(tmp_path):
    from datetime import datetime, timezone
    from intel_sources.store import IntelStore
    async def collect(fail):
        def handler(request):
            if fail:
                return httpx.Response(503)
            if request.url.host == 'github.com':
                return httpx.Response(200, text='<title>Trending</title><article class="Box-row"><h2><a href="/Owner/Repo">Owner/Repo</a></h2><p>SQL analytics</p></article>')
            if request.url.path in ('/', '/hot', '/trending'):
                return httpx.Response(200, text=leaderboard())
            return httpx.Response(200, text='<main><h1>sql-tool</h1></main>')
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await tools.fetch_tools(client, github_periods=('daily',))
    first = await collect(False)
    store = IntelStore(tmp_path / 'intel.json')
    now = datetime.now(timezone.utc)
    baseline = store.update(first['items'], first['sources'], now=now)
    assert len(baseline['items']) == 2 and baseline['baseline'] is True
    assert baseline['changes'] == []
    old = {row['id']: row for row in baseline['items']}
    failed = await collect(True)
    retained = store.update(failed['items'], failed['sources'])
    assert len(retained['items']) == 2 and retained['changes'] == []
    assert all(source['status'] == 'error' and source['item_count'] == 0 for source in retained['sources'])
    for row in retained['items']:
        assert row['checked_at'] == old[row['id']]['checked_at']
        assert row['first_seen'] == old[row['id']]['first_seen']
        assert row['evidence'] == old[row['id']]['evidence']
        assert row['freshness'] == 'stale' and row['not_observed_in_latest_fetch'] is True


@pytest.mark.asyncio
async def test_failed_api_keeps_usable_rows_and_public_fallback_auth_isolated(monkeypatch):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', 0)
    requests = []
    def handler(request):
        requests.append(request)
        if request.url.path.startswith('/api/'):
            assert request.headers.get('authorization') == 'Bearer release-test-token'
            if request.url.params['view'] == 'hot':
                return httpx.Response(401)
            return httpx.Response(200, json={'data': [{'id': 'owner/repo/api-tool', 'description': 'SQL analytics'}], 'pagination': {'hasMore': False}})
        assert 'authorization' not in request.headers
        return httpx.Response(200, text=leaderboard())
    result = await fetch(handler, skills_token='release-test-token')
    assert {row['id'] for row in result['items']} == {'skills:owner/repo/api-tool', 'skills:owner/repo/sql-tool'}
    source = result['sources'][0]
    assert source['status'] == 'partial' and source['item_count'] == 2
    assert source['coverage']['mode'] == 'api+public-html'
    assert source['coverage']['api']['views']['hot']['status'] == 'error'
    assert len(requests) == 6
    assert 'release-test-token' not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('source', ['github', 'skills'])
async def test_redirected_view_is_not_mislabeled_as_requested_coverage(source):
    def handler(request):
        if source == 'github':
            if request.url.host != 'github.com':
                return httpx.Response(503)
            if request.url.params.get('since') == 'daily':
                return httpx.Response(307, headers={'Location': '/trending?since=weekly'})
            return httpx.Response(200, text='<article class="Box-row"><h2><a href="/owner/repo">owner/repo</a></h2></article>')
        if request.url.host != 'skills.sh':
            return httpx.Response(503)
        if request.url.path in ('/', '/trending'):
            return httpx.Response(307, headers={'Location': '/hot'})
        return httpx.Response(200, text=leaderboard())
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await tools.fetch_tools(client, github_periods=('daily',))
    if source == 'github':
        assert result['items'] == []
        assert result['sources'][1]['coverage']['periods']['daily']['status'] == 'error'
    else:
        assert set(result['items'][0]['evidence']['views']) == {'hot'}
        assert result['sources'][0]['coverage']['views']['all-time']['status'] == 'error'
        assert result['sources'][0]['coverage']['views']['trending']['status'] == 'error'
