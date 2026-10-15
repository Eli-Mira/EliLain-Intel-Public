"""Cloudflare Pages is the only host; the Netlify mirror is opt-in and best-effort."""
import httpx
import pytest
from test_report_delivery import artifact_dir, module, ENV, MIRROR_ENV, Protocol

PAGES = 'https://example.pages.dev/'


class PagesProtocol(Protocol):
    def __init__(self, data, *, pages_data=None, pages_status=200):
        super().__init__(data)
        self.pages_data = data if pages_data is None else pages_data
        self.pages_status = pages_status

    def __call__(self, request):
        if request.headers['host'] == 'example.pages.dev':
            self.calls.append(request)
            assert request.method == 'GET'
            assert request.url.path == '/'
            assert 'authorization' not in request.headers
            assert 'cookie' not in request.headers
            return httpx.Response(self.pages_status, content=self.pages_data)
        return super().__call__(request)


def deliver(directory, protocol, **kwargs):
    return module().deliver_report(directory, environ={**ENV, 'PUBLIC_REPORT_URL': PAGES},
        transport=httpx.MockTransport(protocol), resolver=lambda host: ['8.8.8.8'],
        sleep=lambda _: None, **kwargs)


def test_pages_is_the_only_host_unless_a_mirror_is_opted_in(artifact_dir):
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = PagesProtocol(data)
    result = deliver(artifact_dir, protocol)
    assert result.hosted.url == PAGES
    assert PAGES in result.brief
    assert result.success
    assert not any(r.headers['host'] == 'api.netlify.com' for r in protocol.calls), \
        'Netlify must stay out of the default path entirely'


def test_opted_in_mirror_is_still_deployed_best_effort(artifact_dir):
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = PagesProtocol(data)
    result = module().deliver_report(artifact_dir, environ={**MIRROR_ENV, 'PUBLIC_REPORT_URL': PAGES},
        transport=httpx.MockTransport(protocol), resolver=lambda host: ['8.8.8.8'],
        sleep=lambda _: None)
    assert any(r.headers['host'] == 'api.netlify.com' and r.method == 'POST' for r in protocol.calls)
    assert result.mirror is not None and result.mirror.confirmed is True
    assert result.hosted.url == PAGES
    pages_index = next(i for i, r in enumerate(protocol.calls) if r.headers['host'] == 'example.pages.dev')
    posts = [i for i, r in enumerate(protocol.calls) if r.method == 'POST' and r.headers['host'] in {'discord.com', 'api.telegram.org'}]
    assert posts and all(pages_index < i for i in posts)


@pytest.mark.parametrize('status,body,code', [
    (200, b'not-the-verified-report', 'html_mismatch'),
    (302, b'', 'http_status'),
    (503, b'PRIVATE_ERROR_CANARY', 'http_status'),
])
def test_pages_failure_blocks_every_channel(artifact_dir, status, body, code):
    protocol = PagesProtocol((artifact_dir / 'index.html').read_bytes(), pages_data=body, pages_status=status)
    with pytest.raises(module().DeliveryError, match=code):
        deliver(artifact_dir, protocol)
    assert not any(r.headers['host'] in {'discord.com', 'api.telegram.org'} for r in protocol.calls)


def test_switch_to_pages_requires_new_link_confirmation(artifact_dir, tmp_path):
    data = (artifact_dir / 'index.html').read_bytes()
    state = tmp_path / 'receipts.json'
    old = Protocol(data)
    old.override = lambda request: httpx.Response(200, content=data) if request.headers['host'] == 'previous-example.pages.dev' else None
    module().deliver_report(artifact_dir, environ={**ENV, 'PUBLIC_REPORT_URL': 'https://previous-example.pages.dev/'}, transport=httpx.MockTransport(old),
        resolver=lambda _: ['8.8.8.8'], sleep=lambda _: None, state_path=state)
    protocol = PagesProtocol(data)
    result = deliver(artifact_dir, protocol, state_path=state)
    assert result.telegram.confirmation == 'receipt-confirmed'
    assert result.discord.confirmation == 'independently-read-back'
    repeated = deliver(artifact_dir, PagesProtocol(data), state_path=state)
    assert repeated.telegram.confirmation == repeated.discord.confirmation == 'previously-confirmed'


def test_standalone_pages_readback_needs_no_deploy_or_channel_credentials(artifact_dir):
    verify = getattr(module(), 'verify_pages_report', None)
    assert callable(verify), 'standalone credential-free Pages verifier is missing'
    protocol = PagesProtocol((artifact_dir / 'index.html').read_bytes())
    result = verify(artifact_dir, transport=httpx.MockTransport(protocol), resolver=lambda _: ['8.8.8.8'])
    assert result.url == PAGES
    assert len(protocol.calls) == 1 and protocol.calls[0].headers['host'] == 'example.pages.dev'
