"""Current discovery state is a snapshot, not a concatenated local archive."""
from datetime import datetime, timedelta, timezone

from intel_sources.store import IntelStore

NOW = datetime(2026, 10, 3, tzinfo=timezone.utc)


def row(i):
    return {'id': f'catalog:{i}', 'source': 'catalog', 'fingerprint_data': {'version': 1}}


def sources(status='ok'):
    return [{'name': 'catalog', 'status': status, 'checked_at': NOW.isoformat()}]


def test_rotating_successful_inventory_is_current_not_accumulating(tmp_path):
    path = tmp_path / 'intel.json'
    sizes = []
    for i in range(80):
        state = IntelStore(path).update([row('stable'), row(i)], sources(), now=NOW + timedelta(hours=i))
        assert {r['id'] for r in state['items']} == {'catalog:stable', f'catalog:{i}'}
        assert state['items'][0]['first_seen'] == NOW.isoformat()
        sizes.append(path.stat().st_size)
    assert state['changes'][0]['id'] == 'catalog:79'
    assert len(list(tmp_path.iterdir())) == 1
    print({'runs': 80, 'current_items': len(state['items']), 'final_bytes': sizes[-1]})


def test_partial_inventory_has_bounded_unobserved_history_without_losing_current(tmp_path):
    path = tmp_path / 'intel.json'
    store = IntelStore(path)
    # A failed or absent source retains its last good inventory, including when
    # that full current inventory is larger than the historical-cache budget.
    store.update([row(i) for i in range(1100)], sources(), now=NOW)
    failed = IntelStore(path).update([], sources('error'), now=NOW)
    assert len(failed['items']) == 1100
    absent = IntelStore(path).update([], [], now=NOW)
    assert len(absent['items']) == 1100
    for i in range(20):
        state = IntelStore(path).update([row(f'new-{i}')], sources('partial'), now=NOW)
        assert any(r['id'] == f'catalog:new-{i}' for r in state['items'])
        assert len(state['items']) <= 1025
    assert state['retention']['unobserved_limit_per_source'] == 1024
    assert state['retention']['omitted_unobserved'] >= 1
    stable = next(r for r in state['items'] if r['id'] == 'catalog:new-19')
    restarted = IntelStore(path).update([row('new-19')], sources('partial'), now=NOW + timedelta(hours=1))
    assert restarted['changes'] == []
    assert restarted['items'][0]['first_seen'] == stable['first_seen']
    # Even an adapter with rows plus error/missing source outcome must not
    # accumulate a second historical archive indefinitely.
    for i in range(3):
        state = IntelStore(path).update([row(f'error-{i}')], sources('error'), now=NOW)
        assert len(state['items']) <= 1025


def test_recently_replaced_identity_keeps_first_seen_and_change_fingerprint(tmp_path):
    path = tmp_path / 'intel.json'
    IntelStore(path).update([row('returning')], sources(), now=NOW)
    IntelStore(path).update([row('other')], sources(), now=NOW + timedelta(hours=1))
    resumed = IntelStore(path).update([row('returning')], sources(), now=NOW + timedelta(hours=2))
    assert resumed['items'][0]['first_seen'] == NOW.isoformat()
    assert resumed['changes'] == []
    IntelStore(path).update([row('other')], sources(), now=NOW + timedelta(hours=3))
    changed = row('returning')
    changed['fingerprint_data'] = {'version': 2}
    resumed = IntelStore(path).update([changed], sources(), now=NOW + timedelta(hours=4))
    assert resumed['changes'][0]['change_type'] == 'updated'
    assert resumed['items'][0]['first_seen'] == NOW.isoformat()
    for i in range(140):
        state = IntelStore(path).update([row(i)], sources(), now=NOW + timedelta(hours=i + 5))
    assert len(state['identity_history']) == 64
    assert all(set(entry) == {'id', 'source', 'first_seen', 'fingerprint_sha256'} for entry in state['identity_history'])


def test_corrupt_identity_history_does_not_reset_a_discovery_baseline(tmp_path):
    import json
    import pytest
    path = tmp_path / 'intel.json'
    state = IntelStore(path).update([row('current')], sources(), now=NOW)
    state['identity_history'] = [{'id': 'catalog:old', 'source': 'catalog',
        'first_seen': NOW.isoformat(), 'fingerprint_sha256': 'not-a-digest'}]
    path.write_text(json.dumps(state))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        IntelStore(path).update([row('other')], sources(), now=NOW)
    assert path.read_bytes() == before
