"""R08 protocol regressions; all requests are synthetic."""
import json

import httpx
import pytest

from test_report_delivery import ENV, Protocol, artifact_dir, module


class LinkProtocol(Protocol):
    def __call__(self, request):
        if request.headers['host'] == 'discord.com' and request.method == 'POST' and request.headers.get('content-type', '').startswith('application/json'):
            self.calls.append(request)
            payload = json.loads(request.content)
            assert payload['allowed_mentions'] == {'parse': [], 'replied_user': False}
            self.brief = payload['content']
            return httpx.Response(200, json={'id': '999999999999999999', 'channel_id': ENV['DISCORD_CHANNEL_ID'], 'content': self.brief})
        return super().__call__(request)


def send(directory, protocol, state=None, attachment=False):
    return module().deliver_report(directory, environ={**ENV, 'PUBLIC_REPORT_ATTACHMENT': str(attachment).lower()},
        transport=httpx.MockTransport(protocol), resolver=lambda _: ['8.8.8.8'], state_path=state)


def test_default_is_verified_hosted_link_without_html_upload(artifact_dir):
    protocol = LinkProtocol((artifact_dir / 'index.html').read_bytes())
    result = send(artifact_dir, protocol)
    assert result.success
    assert result.telegram.message_id == '42'
    assert result.brief.startswith('Open the public dashboard: https://example.pages.dev/')
    assert not any(r.url.path.endswith('/sendDocument') for r in protocol.calls)
    assert not any('multipart' in r.headers.get('content-type', '') for r in protocol.calls)
    assert [r.headers['host'] for r in protocol.calls] == ['example.pages.dev', 'api.telegram.org', 'discord.com', 'discord.com']


def test_modes_have_separate_confirmations_and_switch_back_reuses_each(artifact_dir, tmp_path):
    state = tmp_path / 'receipts.json'
    data = (artifact_dir / 'index.html').read_bytes()
    attached = send(artifact_dir, LinkProtocol(data), state, attachment=True)
    assert attached.telegram.message_id == '42/43'
    linked = send(artifact_dir, LinkProtocol(data), state)
    assert linked.telegram.confirmation == 'receipt-confirmed'
    assert linked.telegram.message_id == '42'
    for attachment in (True, False):
        protocol = LinkProtocol(data)
        restarted = send(artifact_dir, protocol, state, attachment=attachment)
        assert restarted.telegram.confirmation == restarted.discord.confirmation == 'previously-confirmed'
        assert len(protocol.calls) == 1


def test_partial_mode_change_keeps_original_confirmed_sibling(artifact_dir, tmp_path):
    state = tmp_path / 'receipts.json'
    data = (artifact_dir / 'index.html').read_bytes()
    first = LinkProtocol(data)
    first.override = lambda r: httpx.Response(403) if r.headers['host'] == 'discord.com' and r.method == 'POST' else None
    with pytest.raises(module().DeliveryError):
        send(artifact_dir, first, state, attachment=True)
    send(artifact_dir, LinkProtocol(data), state)
    protocol = LinkProtocol(data)
    restarted = send(artifact_dir, protocol, state, attachment=True)
    assert restarted.telegram.confirmation == 'previously-confirmed'
    assert restarted.discord.confirmation == 'independently-read-back'
    assert not any(r.headers['host'] == 'api.telegram.org' for r in protocol.calls)


def test_invalid_attachment_setting_fails_before_requests(artifact_dir):
    protocol = LinkProtocol((artifact_dir / 'index.html').read_bytes())
    with pytest.raises(module().DeliveryError, match='invalid_attachment_setting'):
        module().deliver_report(artifact_dir, environ={**ENV, 'PUBLIC_REPORT_ATTACHMENT': 'maybe'},
            transport=httpx.MockTransport(protocol))
    assert protocol.calls == []


def test_attachment_partial_restart_skips_confirmed_telegram_brief(artifact_dir, tmp_path):
    state = tmp_path / 'receipts.json'
    data = (artifact_dir / 'index.html').read_bytes()
    first = LinkProtocol(data)
    first.override = lambda r: httpx.Response(503) if r.url.path.endswith('/sendDocument') else None
    with pytest.raises(module().DeliveryError) as caught:
        send(artifact_dir, first, state, attachment=True)
    assert caught.value.result.telegram.message_id == '42'
    second = LinkProtocol(data)
    resumed = send(artifact_dir, second, state, attachment=True)
    assert resumed.telegram.message_id == '42/43'
    assert not any(r.url.path.endswith('/sendMessage') for r in second.calls)
    assert resumed.discord.confirmation == 'previously-confirmed'


def test_publisher_cli_persists_receipts_and_explicit_attachment_setting(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('publisher_options', Path(__file__).resolve().parents[1] / 'scripts/publish_report.py')
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    seen = {}
    def capture(*args, **kwargs):
        seen.update(kwargs)
        raise cli.DeliveryError('synthetic_stop')
    monkeypatch.setattr(cli, 'deliver_report', capture)
    assert cli.main(['--export-dir', str(tmp_path), '--json']) == 1
    assert seen.get('state_path') == Path(__file__).resolve().parents[1] / 'data/delivery_receipts.json'
    assert cli.main(['--export-dir', str(tmp_path), '--state-path', str(tmp_path / 'receipts.json'), '--attach-html', '--json']) == 1
    assert seen['state_path'] == tmp_path / 'receipts.json'
    assert seen['environ']['PUBLIC_REPORT_ATTACHMENT'] == 'true'
    assert cli.main(['--export-dir', str(tmp_path), '--link-only', '--json']) == 1
    assert seen['environ']['PUBLIC_REPORT_ATTACHMENT'] == 'false'


def test_invalid_optional_document_id_is_not_saved_as_confirmation(artifact_dir, tmp_path):
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = LinkProtocol(data)
    protocol.override = lambda r: httpx.Response(200, json={'ok': True, 'result': {
        'chat': {'id': int(ENV['TELEGRAM_CHAT_ID'])},
        'document': {'file_name': 'index.html', 'file_size': len(data)}}}) if r.url.path.endswith('/sendDocument') else None
    with pytest.raises(module().DeliveryError, match='channel_failure') as caught:
        send(artifact_dir, protocol, tmp_path / 'receipts.json', attachment=True)
    assert caught.value.result.telegram.message_id == '42'
    assert caught.value.result.telegram.error_code == 'telegram_attachment_unverified'


def test_workflow_default_is_link_only_and_artifact_is_latest_not_archive():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] / '.github/workflows/news-tracker-daily.yml').read_text())
    steps = workflow['jobs']['run-elilain-audit']['steps']
    publish = next(step for step in steps if 'main.py publish' in step.get('run', ''))
    assert publish['env'].get('PUBLIC_REPORT_ATTACHMENT') == 'false'
    # The sensitive reports/latest.html upload is removed; only the sanitized
    # public/index.html artifact is uploaded.
    report_artifacts = [s for s in steps if s.get('with', {}).get('name', '').startswith('elain-report-')]
    assert report_artifacts == [], "sensitive reports/latest.html upload must be removed"
