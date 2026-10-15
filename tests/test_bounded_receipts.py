"""Bounded restart metadata on disk, not fake archive measurements."""
from dataclasses import replace
import hashlib

from intel_sources import report_delivery as rd
from test_receipt_durability import _artifact, _config, _result


def test_receipt_history_caps_completed_artifacts_but_preserves_partial_sibling(tmp_path):
    state = tmp_path / 'receipts.json'
    config = _config(PUBLIC_REPORT_ATTACHMENT='true')
    partial = replace(_result(), discord=rd.ChannelReceipt('discord', None, 'unconfirmed', 'http_status'))
    rd.save_receipts(state, _artifact('f' * 64), partial, config)
    sizes = []
    for i in range(140):
        digest = hashlib.sha256(str(i).encode()).hexdigest()
        rd.save_receipts(state, _artifact(digest), _result(), config)
        sizes.append(state.stat().st_size)
    records = rd.load_receipts(state)
    assert len(records) <= 64
    assert records['f' * 64]['telegram'] == partial.telegram.message_id
    assert digest in records
    assert max(sizes[70:]) <= 40000
    assert len(list(tmp_path.iterdir())) == 1
    print({'runs': 140, 'retained': len(records), 'last_bytes': sizes[-1], 'plateau_max_bytes': max(sizes[70:])})


def test_full_partial_budget_refuses_new_send_before_network(artifact_dir, tmp_path):
    import httpx
    import pytest
    from test_report_delivery import ENV, Protocol
    path = tmp_path / 'receipts.json'
    config = _config(PUBLIC_REPORT_ATTACHMENT='true')
    partial = replace(_result(), discord=rd.ChannelReceipt('discord', None, 'unconfirmed', 'http_status'))
    for i in range(64):
        rd.save_receipts(path, _artifact(hashlib.sha256(str(i).encode()).hexdigest()), partial, config)
    previous = path.read_bytes()
    protocol = Protocol((artifact_dir / 'index.html').read_bytes())
    with pytest.raises(rd.DeliveryError, match='receipt_capacity'):
        rd.deliver_report(artifact_dir, environ=ENV, transport=httpx.MockTransport(protocol), state_path=path)
    assert protocol.calls == []
    assert path.read_bytes() == previous


# The shared synthetic artifact fixture; no live state is read or overwritten.
from test_report_delivery import artifact_dir


def test_receipt_metadata_rejects_unbounded_or_unknown_fields(tmp_path):
    import json
    import pytest
    path = tmp_path / 'receipts.json'
    for extra in ({'raw_response': 'PRIVATE_RESPONSE'}, {'url': 'x' * 4096}, {'telegram_link': '1' * 1000}):
        path.write_text(json.dumps({'artifacts': {'a' * 64: extra}}))
        before = path.read_bytes()
        with pytest.raises(rd.DeliveryError, match='invalid_receipt_state'):
            rd.load_receipts(path)
        assert path.read_bytes() == before
