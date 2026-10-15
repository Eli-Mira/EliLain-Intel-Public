"""Project shortlists are advisory metadata evaluations, not installation proof."""
from datetime import datetime, timezone
import importlib
import importlib.util
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
FIXTURES = "tests/fixtures"

NOW = datetime(2026, 10, 1, 0, 20, tzinfo=timezone.utc)
PROJECTS = {'desktop-app', 'vision-app', 'data-ai'}


def evaluate(items, now=NOW):
    assert importlib.util.find_spec('intel_sources.recommendations') is not None, 'Project evaluator is not implemented'
    return importlib.import_module('intel_sources.recommendations').evaluate_projects(items, now=now)


def test_empty_inventory_has_explicit_project_groups_without_invented_candidates():
    result = evaluate([])
    assert set(result['groups']) == PROJECTS
    assert result['inventory_count'] == 0
    assert all(not group['shortlist'] for group in result['groups'].values())
    assert result['evaluated_at'] == NOW.isoformat()
    assert result['coverage']['inventory_replaced'] is False


def row(identifier='skills:obra/superpowers/test-driven-development', **overrides):
    value = {'id': identifier, 'headline': identifier.rsplit('/', 1)[-1],
             'source': 'github-trending' if identifier.startswith('github:') else 'skills-sh', 'summary': '',
             'url': ('https://github.com/' + identifier.removeprefix('github:') if identifier.startswith('github:')
                     else 'https://skills.sh/' + identifier.removeprefix('skills:')),
             'checked_at': NOW.isoformat(), 'freshness': 'fresh',
             'project_types': [], 'evidence': {'origin': 'obra/superpowers'}}
    value.update(overrides)
    return value


def test_one_observed_skill_gets_distinct_project_reasons_and_multiple_fit_axes():
    result = evaluate([row()])
    reasons = []
    for project, group in result['groups'].items():
        selection = group['shortlist'][0]
        assert selection['id'] == row()['id']
        assert len(selection['axes']) == 3
        assert selection['reason']
        assert selection['constraints']
        assert selection['source_urls'] == [row()['url']]
        assert selection['evaluation_status'] == 'metadata-only'
        assert selection['integration_tested'] is False
        assert selection['confidence'] == 'low'
        reasons.append(selection['reason'])
    assert len(set(reasons)) == len(PROJECTS)


def metadata(repo='obra/superpowers', **overrides):
    url = 'https://api.github.com/repos/' + repo
    value = {'status': 'ok', 'repository': repo, 'url': url,
             'source_urls': [url], 'checked_at': NOW.isoformat(),
             'license': 'MIT', 'archived': False, 'disabled': False,
             'description': 'Development methodology',
             'capability_description': 'Test-first development guidance',
             'capability_url': 'https://raw.githubusercontent.com/' + repo + '/main/skills/test-driven-development/SKILL.md'}
    value['source_urls'].append(value['capability_url'])
    value.update(overrides)
    return value


def test_official_metadata_can_support_medium_suitability_but_not_integration_or_free_cost():
    observed = row(evidence={'official_metadata': metadata(license='NOASSERTION')})
    selected = evaluate([observed])['groups']['desktop-app']['shortlist'][0]
    assert selected['confidence'] == 'medium'
    assert selected['evaluation_status'] == 'metadata-only'
    assert selected['integration_tested'] is False
    assert selected['license'] == 'unknown'
    assert selected['cost'] == 'unknown'
    assert selected['support'] == 'unknown'
    assert selected['freshness'] == {'discovery': 'fresh', 'official_metadata': 'fresh'}
    assert selected['source_urls'] == [observed['url']] + metadata()['source_urls']
    assert selected['evidence']['discovery']['checked_at'] == NOW.isoformat()
    assert selected['evidence']['official_metadata']['repository'] == 'obra/superpowers'


def test_curated_profiles_route_known_observations_to_specific_project_workstreams():
    ids = [
        'skills:obra/superpowers/systematic-debugging',
        'skills:mattpocock/skills/domain-modeling',
        'skills:mattpocock/skills/codebase-design',
        'skills:supabase/agent-skills/supabase-postgres-best-practices',
        'github:pytorch/pytorch', 'github:vectifyai/pageindex',
        'github:mksglu/context-mode',
    ]
    result = evaluate([row(identifier) for identifier in ids])
    expected = {
        'desktop-app': {ids[0], ids[1]},
        'vision-app': {ids[0], ids[2]},
        'data-ai': {ids[3], ids[4], ids[5], ids[6]},
    }
    for project, selections in expected.items():
        actual = result['groups'][project]['shortlist']
        assert {selection['id'] for selection in actual} == selections
        assert all(selection['reason'] and selection['constraints'] and selection['next_validation'] for selection in actual)


def test_ranking_is_bounded_deterministic_deduplicated_and_independent_of_popularity():
    from copy import deepcopy
    import json
    candidates = [row(identifier) for identifier in (
        'skills:supabase/agent-skills/supabase-postgres-best-practices',
        'skills:obra/superpowers/test-driven-development',
        'github:pytorch/pytorch', 'github:vectifyai/pageindex',
        'github:mksglu/context-mode',
    )]
    irrelevant = row('skills:unknown/catalog/top-ranked', project_types=['game', 'data', 'design', 'agents'],
                     evidence={'total_stars': 99999999, 'views': {'hot': {'rank': 1}}})
    items = candidates + [deepcopy(candidates[0]), irrelevant]
    before = deepcopy(items)
    first = evaluate(items)
    for item in items:
        item['evidence']['total_stars'] = 100000000
        item['evidence']['periods'] = {'daily': {'rank': 1}}
    second = evaluate(list(reversed(items)))
    selected = first['groups']['data-ai']['shortlist']
    assert len(selected) == 4
    assert len({item['id'] for item in selected}) == 4
    assert [item['id'] for item in selected] == [item['id'] for item in second['groups']['data-ai']['shortlist']]
    assert [item['score'] for item in selected] == sorted([item['score'] for item in selected], reverse=True)
    assert first['groups']['data-ai']['deferred'][0]['id'] == candidates[-1]['id']
    assert first['coverage']['unreviewed_ids'] == [irrelevant['id']]
    assert first['coverage']['discovery_count'] == 6
    assert first['inventory_count'] == 7
    assert json.dumps(evaluate(before)) == json.dumps(first)


@pytest.mark.parametrize('target,changes,expected', [
    ('discovery', {'checked_at': '2026-09-28T00:00:00Z'}, 'stale'),
    ('discovery', {'checked_at': '2026-10-02T00:00:00Z'}, 'unverified'),
    ('discovery', {'checked_at': '2026-10-01T00:00:00'}, 'unverified'),
    ('discovery', {'not_observed_in_latest_fetch': True}, 'stale'),
    ('discovery', {'freshness': 'stale'}, 'stale'),
    ('official_metadata', {'checked_at': '2026-09-28T00:00:00Z'}, 'stale'),
    ('official_metadata', {'status': 'error'}, 'unverified'),
    ('official_metadata', {'checked_at': None}, 'unverified'),
])
def test_stale_unknown_or_future_evidence_cannot_gain_medium_confidence(target, changes, expected):
    official = metadata()
    observed = row(evidence={'official_metadata': official})
    (observed if target == 'discovery' else official).update(changes)
    selected = evaluate([observed])['groups']['desktop-app']['shortlist'][0]
    assert selected['freshness'][target] == expected
    assert selected['confidence'] == 'low'
    assert selected['confidence_reason']
    assert selected['evaluation_status'] == 'metadata-only'


def test_incompatible_frameworks_are_explained_not_offered_as_native_project_dependencies():
    browser = 'skills:vercel-labs/agent-browser/agent-browser'
    react = 'skills:vercel-labs/agent-skills/vercel-react-best-practices'
    postgres = 'skills:supabase/agent-skills/supabase-postgres-best-practices'
    result = evaluate([row(browser), row(react), row(postgres)])
    desktop = result['groups']['desktop-app']
    assert not desktop['shortlist']
    excluded = {entry['id']: entry for entry in desktop['excluded']}
    assert 'PySide6' in excluded[browser]['reason']
    assert 'React' in excluded[react]['reason']
    assert 'PostgreSQL' in excluded[postgres]['reason']
    assert all(entry['source_urls'] and entry['evaluation_status'] == 'metadata-only' for entry in excluded.values())
    gesture = result['groups']['vision-app']
    assert 'camera' in next(entry for entry in gesture['excluded'] if entry['id'] == browser)['reason']
    assert postgres not in result['coverage']['unreviewed_ids']


@pytest.mark.parametrize('changes', [
    {'repository': 'other/owner'},
    {'url': 'https://api.github.com/repos/other/owner'},
    {'url': 'https://api.github.com.evil.test/repos/obra/superpowers'},
    {'capability_url': 'https://raw.githubusercontent.com/other/owner/main/SKILL.md'},
    {'source_urls': ['javascript:alert(1)']},
])
def test_unbound_official_metadata_cannot_elevate_confidence_or_publish_untrusted_links(changes):
    observed = row(evidence={'official_metadata': metadata(**changes)})
    selected = evaluate([observed])['groups']['desktop-app']['shortlist'][0]
    assert selected['confidence'] == 'low'
    assert selected['source_urls'] == [observed['url']]
    assert selected['license'] == 'unknown'
    assert selected['evidence']['official_metadata']['status'] == 'rejected'


def test_a_different_skill_in_the_same_repository_cannot_supply_capability_evidence():
    wrong_url = 'https://raw.githubusercontent.com/obra/superpowers/main/skills/systematic-debugging/SKILL.md'
    official = metadata(capability_url=wrong_url)
    official['source_urls'] = [official['url'], wrong_url]
    observed = row(evidence={'official_metadata': official})
    selection = evaluate([observed])['groups']['desktop-app']['shortlist'][0]
    assert selection['confidence'] == 'low'
    assert selection['source_urls'] == [observed['url']]


@pytest.mark.parametrize('changes,term', [
    ({'archived': True}, 'archived'),
    ({'disabled': True}, 'disabled'),
    ({'capability_error': 'HTTP 404'}, 'unavailable'),
])
def test_official_incompatibilities_withhold_adoption_even_when_discovery_is_popular(changes, term):
    observed = row(evidence={'official_metadata': metadata(**changes), 'total_stars': 9999999})
    group = evaluate([observed])['groups']['desktop-app']
    assert group['shortlist'] == []
    assert term in group['excluded'][0]['reason']
    assert group['excluded'][0]['integration_tested'] is False


def test_naive_evaluation_time_is_rejected_instead_of_guessing_a_timezone():
    with pytest.raises(ValueError, match='timezone-aware'):
        evaluate([], now=datetime(2026, 10, 1))


@pytest.mark.parametrize('changes', [
    {'id': 'skills:obra/superpowers/../test-driven-development'},
    {'id': 'skills:obra/superpowers/test-driven-development '},
    {'url': 'javascript:alert(1)'},
    {'url': 'https://skills.sh/other/repo/test-driven-development'},
    {'url': 'https://skills.sh/obra/superpowers/test-driven-development?token=secret'},
    {'source': 'openrouter-models'},
])
def test_malformed_discovery_identity_is_reported_without_repair_or_unsafe_links(changes):
    observed = row(**changes)
    result = evaluate([observed])
    assert all(not group['shortlist'] for group in result['groups'].values())
    assert result['coverage']['invalid_rows'][0]['id'] == observed['id']
    assert result['coverage']['invalid_rows'][0]['reason']


def test_provider_catalog_rows_and_malformed_rows_do_not_become_project_discovery():
    result = evaluate([{'id': 'openrouter:model:x'}, {'id': None}, None])
    assert result['inventory_count'] == 3
    assert result['coverage']['discovery_count'] == 0


def test_saved_discovery_evidence_is_preserved_but_not_promoted_to_experimental_proof():
    from copy import deepcopy
    observed = row(evidence={'origin': 'obra/superpowers', 'views': {'hot': {'rank': 1, 'installs': 42}},
                             'metadata': {'status': 'ok'}, 'topics': ['agents'],
                             'official_metadata': metadata(), 'tier': 'S-tier', 'experimentally_verified': True})
    original = deepcopy(observed)
    result = evaluate([observed])
    selected = result['groups']['desktop-app']['shortlist'][0]
    assert selected['evidence']['discovery']['fields']['views'] == observed['evidence']['views']
    assert selected['evidence']['discovery']['semantics'] == 'observed metadata; not capability or integration proof'
    assert 'tier' not in selected and 'experimentally_verified' not in selected
    assert selected['integration_tested'] is False
    selected['evidence']['official_metadata']['status'] = 'changed-by-consumer'
    selected['evidence']['discovery']['fields']['views']['hot']['rank'] = 99
    assert observed == original
    assert result['groups']['vision-app']['shortlist'][0]['evidence']['official_metadata']['status'] == 'ok'


def test_real_saved_shortlist_artifact_replays_exactly_from_metadata_only_snapshot():
    import json
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    path = root / 'data' / 'project_evaluations.json'
    if not path.exists():
        path = root / FIXTURES / 'project_evaluations_sample.json'
    assert path.exists(), 'No project shortlist artifact or committed fixture is available'
    artifact = json.loads(path.read_text(encoding='utf-8'))
    snapshot = artifact['discovery_snapshot']
    at = datetime.fromisoformat(artifact['result']['evaluated_at'])
    assert evaluate(snapshot, now=at) == artifact['result']
    assert len(snapshot) == artifact['result']['inventory_count']
    # Discovery candidates are the subset this evaluator considers; it is not
    # every inventory row. Scale-relative so a bounded fixture is valid too.
    candidates = {item['id'] for item in snapshot
                  if item.get('source') in ('github-trending', 'skills-sh')}
    assert artifact['result']['coverage']['discovery_count'] == len(candidates)
    assert 0 < artifact['result']['coverage']['discovery_count'] <= len(snapshot)
    assert artifact['provenance']['source_inventory_count'] >= len(snapshot)
    assert len(artifact['provenance']['input_state_sha256']) == 64
    ids = {item['id'] for item in snapshot}
    for group in artifact['result']['groups'].values():
        for selection in group['shortlist']:
            assert selection['id'] in ids
            assert selection['source_urls']
            assert selection['evaluation_status'] == 'metadata-only'
            assert selection['integration_tested'] is False
            assert selection['confidence'] in ('low', 'medium')
