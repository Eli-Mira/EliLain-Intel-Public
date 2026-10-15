"""Detail-selection regressions: advisory metadata only, no discovered-code execution."""
import httpx
import pytest

from intel_sources import tools


def board(names, owner='independent', start=1):
    return '<title>Skills</title>' + ''.join(
        f'<a href="/{owner}/catalog/{name}"><span>{rank}</span><h3>{name}</h3>'
        f'<p>{owner}/catalog</p><span>100</span></a>'
        for rank, name in enumerate(names, start)
    )


async def collect(handler, **kwargs):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await tools.fetch_tools(client, github_periods=(), **kwargs)


@pytest.mark.asyncio
@pytest.mark.parametrize('owner', ['independent', 'different-publisher'])
async def test_lower_ranked_workflow_candidates_get_details_without_inventory_loss(owner):
    # Deliberately not the identity in the prior audit: selection must generalize.
    useful = ['refactoring-workflow', 'test-driven-development', 'sql-analytics',
              'security-audit', 'python-camera-vision']
    names = [f'popular-{i}' for i in range(50)] + useful
    requested = []

    def handler(request):
        if request.url.path in ('/', '/trending', '/hot'):
            return httpx.Response(200, text=board(names, owner))
        requested.append(request)
        name = request.url.path.rsplit('/', 1)[-1]
        return httpx.Response(200, text=f'<meta name="description" content="Observed {name} metadata"><main><h1>{name}</h1></main>')

    result = await collect(handler)
    rows = {row['headline']: row for row in result['items']}
    assert set(rows) == set(names)
    assert len(requested) == tools.METADATA_LIMIT == 40
    assert all(rows[name]['evidence']['metadata']['status'] == 'ok' for name in useful)
    assert all(rows[name]['summary'] for name in useful)
    report = result['sources'][0]['coverage']['metadata']
    assert report['attempted'] == report['enriched'] == 40
    assert report['not_scanned'] == 15
    assert result['sources'][0]['item_count'] == 55


@pytest.mark.asyncio
async def test_broad_slots_sample_all_views_instead_of_all_time_only():
    requested = []
    views = {'/': 'classic', '/trending': 'rising', '/hot': 'newcomer'}

    def handler(request):
        if request.url.path in views:
            prefix = views[request.url.path]
            return httpx.Response(200, text=board([f'{prefix}-{i}' for i in range(50)]))
        requested.append(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(200, text='<main><h1>Detail</h1></main>')

    result = await collect(handler)
    assert len(result['items']) == 150
    assert len(requested) == 40
    assert {name.rsplit('-', 1)[0] for name in requested} == set(views.values())
    coverage = result['sources'][0]['coverage']['metadata']
    assert coverage['empty'] == 40 and coverage['enriched'] == 0
    assert coverage['not_scanned'] == 110


@pytest.mark.asyncio
async def test_selection_is_stable_under_input_reordering_and_rank_ties(monkeypatch):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', 4)

    async def selected(reverse):
        rows = [tools._row(f'skills:independent/catalog/{name}', name, 'skills-sh', '',
                           f'https://skills.sh/independent/catalog/{name}',
                           {'skill_id': f'independent/catalog/{name}',
                            'views': {'all-time': {'rank': 100}}})
                for name in ['sql-z', 'sql-a', 'ordinary-z', 'ordinary-a', 'ordinary-b', 'ordinary-c']]
        if reverse:
            rows.reverse()
        source = tools._source('skills-sh', 'https://skills.sh/', {})
        requested = []

        def handler(request):
            requested.append(str(request.url))
            return httpx.Response(200, text='<main><h1>Detail</h1></main>')

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await tools._skill_metadata(client, rows, source)
        return requested

    assert await selected(False) == await selected(True)


@pytest.mark.asyncio
async def test_selection_explanation_and_detail_provenance_survive_scratch_store(tmp_path):
    import hashlib
    from datetime import datetime, timezone
    from intel_sources.store import IntelStore

    names = [f'popular-{i}' for i in range(45)] + ['sql-analytics', 'pytest-workflow']
    detail = '<meta name="description" content="Observed SQL test workflow"><main><h1>Detail</h1><a href="/topic/testing">Testing</a><script>do-not-run</script></main>'

    def handler(request):
        if request.url.path in ('/', '/trending', '/hot'):
            return httpx.Response(200, text=board(names))
        if request.url.host == 'skills.sh':
            return httpx.Response(307, headers={'Location': str(request.url).replace('skills.sh', 'www.skills.sh')})
        return httpx.Response(200, text=detail)

    result = await collect(handler)
    report = result['sources'][0]['coverage']['metadata']
    assert report['selection']['candidate_count'] == 47
    assert report['selection']['selected'] == 40
    assert report['selection']['workflow_budget'] == 20
    assert report['selection']['workflow_selected'] == 2
    assert report['selection']['broad_selected'] == 38
    store = IntelStore(tmp_path / 'selection-state.json')
    store.update(result['items'], result['sources'])
    state = store.read()
    row = next(row for row in state['items'] if row['headline'] == 'sql-analytics')
    selection = row['evidence']['metadata_selection']
    assert selection['lane'] == 'workflow:data'
    assert selection['advisory_only'] is True
    assert {'group': 'data', 'field': 'headline', 'term': 'sql'} in selection['hint_matches']
    assert 'metadata_selection' not in row['fingerprint_data']
    metadata = row['evidence']['metadata']
    assert metadata['url'] == row['url']
    assert metadata['final_url'] == row['url'].replace('skills.sh', 'www.skills.sh')
    assert metadata['sha256'] == hashlib.sha256(detail.encode()).hexdigest()
    assert metadata['http_status'] == 200
    assert datetime.fromisoformat(metadata['checked_at']) <= datetime.now(timezone.utc)
    assert row['evidence']['topics'] == ['Testing']
    assert row['summary'] == 'Observed SQL test workflow'
    assert 'do-not-run' not in str(state)
    assert 'official_metadata' not in row['evidence']


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', ['503', 'timeout', 'login', 'missing-heading', 'wrong-identity', 'empty'])
async def test_selected_detail_failure_retains_inventory_and_usable_siblings(monkeypatch, failure):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', 6)
    names = [f'popular-{i}' for i in range(45)] + ['sql-analytics', 'security-audit']
    requested = []

    def handler(request):
        if request.url.path in ('/', '/trending', '/hot'):
            if request.url.path == '/trending':
                return httpx.Response(503)
            return httpx.Response(200, text=board(names))
        requested.append(request)
        if request.url.path.endswith('sql-analytics'):
            if failure == '503':
                return httpx.Response(503)
            if failure == 'timeout':
                raise httpx.ReadTimeout('controlled test failure', request=request)
            if failure == 'wrong-identity':
                return httpx.Response(307, headers={'Location': '/other/catalog/unrelated'})
            markup = {'login': '<title>Sign in</title><main><h1>SQL</h1></main>',
                      'missing-heading': '<meta name="description" content="Unattributed SQL">',
                      'empty': '<meta name="description" content="Install the sql-analytics skill"><main><h1>SQL</h1></main>'}
            return httpx.Response(200, text=markup[failure])
        return httpx.Response(200, text='<meta name="description" content="Observed useful metadata"><main><h1>Detail</h1></main>')

    result = await collect(handler)
    rows = {row['headline']: row for row in result['items']}
    assert len(rows) == 47
    assert rows['security-audit']['summary'] == 'Observed useful metadata'
    assert rows['sql-analytics']['summary'] == ''
    assert rows['sql-analytics']['evidence']['metadata']['status'] == ('empty' if failure == 'empty' else 'error')
    source = result['sources'][0]
    assert source['coverage']['views']['trending']['status'] == 'error'
    assert source['coverage']['views']['hot']['status'] == 'ok'
    assert source['status'] == 'partial'
    report = source['coverage']['metadata']
    assert report['attempted'] == report['enriched'] + report['errors'] + report['empty'] == 6
    assert report['enriched'] == 5
    assert report['not_scanned'] == 41
    assert report['selection']['selected'] == 6
    assert len(requested) == (7 if failure == 'wrong-identity' else 6)
    assert all('authorization' not in request.headers for request in requested)


@pytest.mark.asyncio
async def test_existing_api_descriptions_do_not_spend_detail_budget(monkeypatch):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', 1)
    requested = []

    def handler(request):
        requested.append(request)
        if request.url.path.startswith('/api/'):
            return httpx.Response(200, json={'data': [
                {'id': 'independent/catalog/security-audit', 'description': 'Existing observed description'},
                {'id': 'independent/catalog/sql-analytics', 'name': 'sql-analytics'}],
                'pagination': {'hasMore': False}})
        assert 'authorization' not in request.headers
        return httpx.Response(200, text='<meta name="description" content="New SQL metadata"><main><h1>SQL</h1></main>')

    result = await collect(handler, skills_token='synthetic-test-token')
    assert len(requested) == 4
    report = result['sources'][0]['coverage']['metadata']
    assert report['selection']['candidate_count'] == report['selection']['selected'] == 1
    assert report['not_scanned'] == 0 and report['attempted'] == report['enriched'] == 1
    described = next(row for row in result['items'] if row['id'].endswith('security-audit'))
    assert described['summary'] == 'Existing observed description'
    assert 'metadata' not in described['evidence']
    assert 'synthetic-test-token' not in str(result)


@pytest.mark.asyncio
@pytest.mark.parametrize('budget', [0, 1, 3, 40])
async def test_small_and_sparse_budgets_spill_unused_workflow_slots_without_duplicates(monkeypatch, budget):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', budget)
    requested = []
    # Owner/repository keyword noise must not make ordinary slugs relevant.
    names = ['ordinary-a', 'ordinary-b', 'ordinary-c']

    def handler(request):
        if request.url.path in ('/', '/trending', '/hot'):
            return httpx.Response(200, text=board(names, owner='security-python-tests'))
        requested.append(str(request.url))
        return httpx.Response(200, text='<main><h1>Detail</h1></main>')

    result = await collect(handler)
    report = result['sources'][0]['coverage']['metadata']
    assert len(requested) == len(set(requested)) == min(budget, 3)
    assert report['selection']['workflow_selected'] == 0
    assert report['selection']['broad_selected'] == min(budget, 3)
    assert report['attempted'] + report['not_scanned'] == 3
    assert len(result['items']) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(('broad_hint', 'specific'), [
    ('workflow', 'systematic-debugging'), ('quality', 'pytest-regression'),
    ('agent', 'sql-analytics'), ('audit', 'security-hardening'),
    ('frontend', 'python-camera-vision'),
])
async def test_specific_workflow_signals_outrank_generic_title_collisions(monkeypatch, broad_hint, specific):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', 10)
    names = [f'{broad_hint}-{i}' for i in range(30)] + [specific]

    def handler(request):
        if request.url.path in ('/', '/trending', '/hot'):
            return httpx.Response(200, text=board(names))
        return httpx.Response(200, text='<meta name="description" content="Observed useful metadata"><main><h1>Detail</h1></main>')

    result = await collect(handler)
    row = next(row for row in result['items'] if row['headline'] == specific)
    assert row['evidence']['metadata']['status'] == 'ok'
    assert row['evidence']['metadata_selection']['lane'].startswith('workflow:')
    report = result['sources'][0]['coverage']['metadata']
    assert report['attempted'] == 10 and report['not_scanned'] == 21


@pytest.mark.asyncio
async def test_one_large_workflow_group_cannot_starve_other_relevant_groups():
    names = [f'pytest-suite-{i}' for i in range(60)] + [
        'debug-refactor', 'sql-analytics', 'security-hardening', 'python-camera-vision']

    def handler(request):
        if request.url.path in ('/', '/trending', '/hot'):
            return httpx.Response(200, text=board(names))
        return httpx.Response(200, text='<main><h1>Detail</h1><a href="/topic/data">Data</a></main>')

    result = await collect(handler)
    rows = {row['headline']: row for row in result['items']}
    assert all(rows[name]['evidence']['metadata']['status'] == 'ok' for name in names[-4:])
    report = result['sources'][0]['coverage']['metadata']
    assert report['attempted'] == report['enriched'] == 40
    assert report['not_scanned'] == 24
    assert report['selection']['workflow_selected'] == report['selection']['broad_selected'] == 20


@pytest.mark.asyncio
async def test_missing_ranks_and_absent_views_use_identity_ties_not_invented_rank(monkeypatch):
    monkeypatch.setattr(tools, 'METADATA_LIMIT', 3)
    rows = [tools._row(f'skills:independent/catalog/{name}', name, 'skills-sh', '',
                       f'https://skills.sh/independent/catalog/{name}',
                       {'skill_id': f'independent/catalog/{name}', 'views': views})
            for name, views in [('z', {}), ('y', {'all-time': {'rank': True}}),
                                ('x', {'all-time': {'rank': -1}}),
                                ('v', {'all-time': {'rank': 4}})]]
    source = tools._source('skills-sh', 'https://skills.sh/', {})
    requested = []

    def handler(request):
        requested.append(request.url.path.rsplit('/', 1)[-1])
        return httpx.Response(200, text='<main><h1>Detail</h1></main>')

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await tools._skill_metadata(client, rows, source)
    assert requested == ['v', 'x', 'y']
    assert rows[0]['evidence']['metadata']['status'] == 'not-scanned'
    assert rows[1]['evidence']['views']['all-time']['rank'] is True
    assert source['coverage']['metadata']['attempted'] == 3
    assert source['coverage']['metadata']['not_scanned'] == 1
