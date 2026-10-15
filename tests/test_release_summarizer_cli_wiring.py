"""CLI opt-in wiring with a simulated LLM only; default must make zero requests."""
import json
from datetime import datetime, timezone
import httpx
import pytest
import main
from tracker_core.schema import NewsItem, SourceType


def prepare_cli(tmp_path, monkeypatch):
    monkeypatch.setenv('NOTIFICATION_CHANNEL', 'none')
    monkeypatch.setenv('SUMMARIZER_API_KEY', 'SYNTHETIC_SUMMARY_KEY')
    monkeypatch.setenv('SUMMARIZER_MODEL', 'vendor/fixture-model')
    monkeypatch.setenv('SUMMARIZER_API_URL', 'https://summarizer-fixture.example/v1/chat/completions')
    calls, instances = [], []
    original_tracker = main.NewsTracker
    original_client = httpx.AsyncClient

    class FixtureTracker(original_tracker):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, cache_dir=tmp_path / 'cache')
            instances.append(self)

        async def fetch_all(self):
            return [NewsItem(source=SourceType.GITHUB, title='Open source Python AI agents release',
                url='https://example.com/fixture', fetched_date=datetime.now(timezone.utc))]

    def handler(request):
        calls.append(request)
        assert str(request.url) == 'https://summarizer-fixture.example/v1/chat/completions'
        assert request.headers['authorization'] == 'Bearer SYNTHETIC_SUMMARY_KEY'
        assert json.loads(request.content)['model'] == 'vendor/fixture-model'
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {
            'content': json.dumps([{'summary': 'Validated fixture AI summary', 'dedup_key': 'fixture-ai'}])}}]})

    monkeypatch.setattr(main, 'NewsTracker', FixtureTracker)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original_client(**kwargs, transport=httpx.MockTransport(handler)))
    args = ['run', '--data-dir', str(tmp_path / 'data'), '--reports-dir', str(tmp_path / 'reports')]
    return args, calls, instances


def test_explicit_cli_opt_in_passes_config_to_production_summarizer(tmp_path, monkeypatch):
    args, calls, instances = prepare_cli(tmp_path, monkeypatch)
    main.main([*args, '--enable-ai-summary'])
    assert len(calls) == 1
    assert instances[0].summarizer_config == {
        'api_key': 'SYNTHETIC_SUMMARY_KEY', 'model': 'vendor/fixture-model',
        'base_url': 'https://summarizer-fixture.example/v1/chat/completions'}


def test_default_cli_ignores_even_populated_summarizer_environment(tmp_path, monkeypatch):
    args, calls, instances = prepare_cli(tmp_path, monkeypatch)
    main.main(args)
    assert calls == []
    assert instances[0].summarizer_config == {}


@pytest.mark.parametrize('missing', ['SUMMARIZER_API_KEY', 'SUMMARIZER_MODEL', 'SUMMARIZER_API_URL'])
def test_opt_in_with_missing_setting_fails_before_files_or_requests(tmp_path, monkeypatch, missing, capsys):
    args, calls, instances = prepare_cli(tmp_path, monkeypatch)
    monkeypatch.delenv(missing)
    with pytest.raises(SystemExit) as stopped:
        main.main([*args, '--enable-ai-summary'])
    assert stopped.value.code == 2
    assert calls == []
    assert instances == []
    assert 'SYNTHETIC_SUMMARY_KEY' not in capsys.readouterr().err
    assert not (tmp_path / 'data').exists()


@pytest.mark.parametrize('endpoint', [
    'ftp://summarizer-fixture.example/v1/chat/completions',
    'http://summarizer-fixture.example/v1/chat/completions',
    'https://SYNTHETIC_SUMMARY_KEY@summarizer-fixture.example/v1/chat/completions',
    'https://summarizer-fixture.example/v1/chat/completions?key=SYNTHETIC_SUMMARY_KEY',
    'https://summarizer-fixture.example/v1/chat/completions#SYNTHETIC_SUMMARY_KEY',
    'https:///v1/chat/completions',
    'https://summarizer-fixture.example:bad/v1/chat/completions',
    'https://summarizer-fixture.example/\nchat/completions',
])
def test_invalid_api_url_rejected_without_echoing_values(tmp_path, monkeypatch, endpoint, capsys):
    args, calls, instances = prepare_cli(tmp_path, monkeypatch)
    monkeypatch.setenv('SUMMARIZER_API_URL', endpoint)
    with pytest.raises(SystemExit) as stopped:
        main.main([*args, '--enable-ai-summary'])
    assert stopped.value.code == 2
    assert calls == []
    assert instances == []
    assert 'SYNTHETIC_SUMMARY_KEY' not in capsys.readouterr().err
    assert not (tmp_path / 'data').exists()
