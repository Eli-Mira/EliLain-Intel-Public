"""Portable release configuration; all service responses and paths are synthetic."""
from pathlib import Path

import httpx
import pytest

from intel_sources.report_delivery import DeliveryConfig, DeliveryError, verify_pages_report
from test_report_delivery import ENV, Protocol, artifact_dir


def test_delivery_accepts_an_explicit_operator_pages_origin():
    config = DeliveryConfig.from_env({**ENV, 'PUBLIC_REPORT_URL': 'https://operator-example.pages.dev/'})
    assert config.public_report_url == 'https://operator-example.pages.dev/'


@pytest.mark.parametrize('url', [
    'http://operator-example.pages.dev/',
    'https://operator-example.pages.dev/private',
    'https://operator-example.pages.dev/?token=synthetic',
    'https://user:synthetic@operator-example.pages.dev/',
    'https://operator-example.pages.dev.evil.example/',
    'https://127.0.0.1/',
    'https://operator-example.pages.dev:443/',
    'https://operator-example.pages.dev/#fragment',
])
def test_operator_origin_rejects_credential_redirect_and_nonroot_shapes(url):
    with pytest.raises(DeliveryError, match='unsafe_public_url'):
        DeliveryConfig.from_env({**ENV, 'PUBLIC_REPORT_URL': url})


def test_standalone_readback_uses_configured_origin_without_auth(artifact_dir, monkeypatch):
    url = 'https://operator-example.pages.dev/'
    monkeypatch.setenv('PUBLIC_REPORT_URL', url)
    raw = (artifact_dir / 'index.html').read_bytes()
    seen = []

    def respond(request):
        seen.append(request)
        assert request.headers['host'] == 'operator-example.pages.dev'
        assert request.url.path == '/'
        assert 'authorization' not in request.headers and 'cookie' not in request.headers
        return httpx.Response(200, content=raw)

    result = verify_pages_report(artifact_dir, transport=httpx.MockTransport(respond),
                                 resolver=lambda _: ['8.8.8.8'])
    assert result.url == url and len(seen) == 1


def test_optional_provider_database_is_local_or_explicit_environment(tmp_path, monkeypatch):
    import main
    monkeypatch.setenv('NOTIFICATION_CHANNEL', 'none')
    local = main.NewsTracker(data_dir=tmp_path / 'data', cache_dir=tmp_path / 'cache',
                             reports_dir=tmp_path / 'reports')
    assert Path(local.freellmapi_db_path) == tmp_path / 'data/freeapi.db'
    explicit = tmp_path / 'operator/freeapi.db'
    monkeypatch.setenv('FREELLMAPI_DB_PATH', str(explicit))
    configured = main.NewsTracker(data_dir=tmp_path / 'other-data', cache_dir=tmp_path / 'other-cache',
                                  reports_dir=tmp_path / 'other-reports')
    assert Path(configured.freellmapi_db_path) == explicit
