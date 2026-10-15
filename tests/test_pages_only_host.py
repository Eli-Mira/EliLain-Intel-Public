"""Cloudflare Pages is the only host; Netlify is opt-in and never required.

Eli removed the standing "retain Netlify as mirror" decision: Netlify's free
tier no longer funds deploys, so it must cost nothing and block nothing. These
tests pin that the delivery path works with NO Netlify credential at all, and
that a mirror is only attempted when explicitly enabled.
"""
from __future__ import annotations

import httpx
import pytest
from test_report_delivery import (ENV, MIRROR_ENV, MIRROR_URL, SITE, DEPLOY, Protocol,
                                   artifact_dir, module, parse_multipart)

PAGES = 'https://example.pages.dev/'
PAGES_ENV = {k: v for k, v in ENV.items() if not k.startswith('NETLIFY_')} | {'PUBLIC_REPORT_URL': PAGES}


class PagesOnly(Protocol):
    """Serves the public origin and both channels; no Netlify endpoint exists."""

    def __call__(self, request):
        if request.headers['host'] == 'example.pages.dev':
            self.calls.append(request)
            assert request.method == 'GET' and request.url.path == '/'
            assert 'authorization' not in request.headers
            assert 'cookie' not in request.headers
            return httpx.Response(200, content=self.data)
        return super().__call__(request)


def deliver(directory, protocol, *, env, **kwargs):
    return module().deliver_report(directory, environ=env, transport=httpx.MockTransport(protocol),
                                   resolver=lambda host: ['8.8.8.8'], sleep=lambda _: None, **kwargs)


def test_delivery_succeeds_with_no_netlify_credential_at_all(artifact_dir):
    """The real goal: dropping Netlify entirely must still publish and notify."""
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = PagesOnly(data)
    result = deliver(artifact_dir, protocol, env=PAGES_ENV)
    assert result.hosted.url == PAGES
    assert result.telegram.confirmation == 'receipt-confirmed'
    assert result.discord.confirmation == 'independently-read-back'
    assert result.success


def test_no_netlify_request_is_made_when_no_mirror_is_enabled(artifact_dir):
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = PagesOnly(data)
    deliver(artifact_dir, protocol, env=PAGES_ENV)
    assert not any(r.headers['host'] == 'api.netlify.com' for r in protocol.calls), \
        'Netlify must not be contacted unless a mirror is explicitly enabled'


def test_no_primary_url_still_publishes_to_pages_and_never_falls_back_to_netlify(artifact_dir):
    """Pages is the only host: absent PUBLIC_REPORT_URL is not a Netlify trigger."""
    env = {k: v for k, v in ENV.items() if not k.startswith('NETLIFY_')}
    env.pop('PUBLIC_REPORT_URL', None)
    protocol = PagesOnly((artifact_dir / 'index.html').read_bytes())
    result = deliver(artifact_dir, protocol, env=env)
    assert result.hosted.url == PAGES
    assert result.success
    assert not any(r.headers['host'] == 'api.netlify.com' for r in protocol.calls)


def test_netlify_credentials_present_do_not_re_enable_the_mirror(artifact_dir):
    """Credentials being present must not silently restore a Netlify dependency."""
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = PagesOnly(data)
    deliver(artifact_dir, protocol, env=PAGES_ENV)
    assert not any(r.headers['host'] == 'api.netlify.com' for r in protocol.calls)


def test_mirror_still_attempts_only_when_explicitly_enabled(artifact_dir):
    """Opt-in must actually work: an enabled mirror is still deployed best-effort."""
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = PagesOnly(data)
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true',
               NETLIFY_AUTH_TOKEN=MIRROR_ENV['NETLIFY_AUTH_TOKEN'],
               NETLIFY_SITE_ID=MIRROR_ENV['NETLIFY_SITE_ID'])
    result = deliver(artifact_dir, protocol, env=env)
    assert any(r.headers['host'] == 'api.netlify.com' for r in protocol.calls)
    assert result.mirror is not None and result.mirror.confirmed is True
    assert result.hosted.url == PAGES, 'Pages remains the published primary'
    assert result.success


def test_enabled_but_broken_mirror_still_cannot_block_delivery(artifact_dir):
    data = (artifact_dir / 'index.html').read_bytes()
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true',
               NETLIFY_AUTH_TOKEN=MIRROR_ENV['NETLIFY_AUTH_TOKEN'],
               NETLIFY_SITE_ID=MIRROR_ENV['NETLIFY_SITE_ID'])

    class BrokenMirror(PagesOnly):
        def __call__(self, request):
            if (request.headers['host'] == 'api.netlify.com' and request.method == 'POST'
                    and request.url.path.endswith('/deploys')):
                self.calls.append(request)
                return httpx.Response(403, json={'error': 'Account credit usage exceeded'})
            return super().__call__(request)

    protocol = BrokenMirror(data)
    result = deliver(artifact_dir, protocol, env=env)
    assert result.mirror.confirmation == 'mirror-unavailable'
    assert result.mirror.error_code == 'http_status'
    assert result.hosted.url == PAGES
    assert result.telegram.confirmation == 'receipt-confirmed'
    assert result.discord.confirmation == 'independently-read-back'
    assert result.success


def test_pages_failure_still_blocks_delivery_without_a_mirror(artifact_dir):
    data = (artifact_dir / 'index.html').read_bytes()

    class BrokenPages(PagesOnly):
        def __call__(self, request):
            if request.headers['host'] == 'example.pages.dev':
                self.calls.append(request)
                return httpx.Response(200, content=b'not-the-verified-report')
            return super().__call__(request)

    protocol = BrokenPages(data)
    with pytest.raises(module().DeliveryError, match='html_mismatch'):
        deliver(artifact_dir, protocol, env=PAGES_ENV)
    assert not any(r.headers['host'] in {'api.telegram.org', 'discord.com'} for r in protocol.calls)


def test_explicit_optional_attachment_is_still_delivered(artifact_dir):
    data = (artifact_dir / 'index.html').read_bytes()
    protocol = PagesOnly(data)
    deliver(artifact_dir, protocol, env={**PAGES_ENV, 'PUBLIC_REPORT_ATTACHMENT': 'true'})
    uploads = [r for r in protocol.calls if r.method == 'POST' and r.headers['host'] == 'discord.com']
    assert uploads
    _, files = parse_multipart(uploads[-1])
    assert files['files[0]'][0] == 'index.html'
    assert files['files[0]'][1] == data


def test_idempotent_rerun_reuses_receipts_with_no_mirror(artifact_dir, tmp_path):
    data = (artifact_dir / 'index.html').read_bytes()
    state = tmp_path / 'receipts.json'
    first = deliver(artifact_dir, PagesOnly(data), env=PAGES_ENV, state_path=state)
    assert first.success
    repeated = deliver(artifact_dir, PagesOnly(data), env=PAGES_ENV, state_path=state)
    assert repeated.telegram.confirmation == 'previously-confirmed'
    assert repeated.discord.confirmation == 'previously-confirmed'
    assert repeated.success

def test_malformed_2xx_mirror_response_cannot_abort_the_publish(artifact_dir):
    """A 200 without 'id'/'ssl_url' raises KeyError inside session.host().

    That must stay inside the mirror block: an unguarded KeyError escaping to the
    outer handler became delivery_failed and killed the whole publish, which is
    the exact incident class this change removes.
    """
    data = (artifact_dir / 'index.html').read_bytes()
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true',
               NETLIFY_AUTH_TOKEN=MIRROR_ENV['NETLIFY_AUTH_TOKEN'],
               NETLIFY_SITE_ID=MIRROR_ENV['NETLIFY_SITE_ID'])

    class Malformed(PagesOnly):
        def __call__(self, request):
            host = request.headers['host']
            if host == 'api.netlify.com' and request.url.path.endswith('/deploys'):
                self.calls.append(request)
                return httpx.Response(200, json={'unexpected': 'shape'})
            if host == 'api.netlify.com' and request.url.path == f'/api/v1/deploys/{DEPLOY}':
                self.calls.append(request)
                return httpx.Response(200, json={'id': DEPLOY, 'site_id': SITE, 'state': 'ready'})
            return super().__call__(request)

    result = deliver(artifact_dir, Malformed(data), env=env)
    assert result.mirror.confirmation == 'mirror-unavailable'
    assert result.mirror.error_code in {'mirror_failed', 'invalid_response', 'html_mismatch', 'unsafe_url'}
    assert result.hosted.url == PAGES
    assert result.telegram.confirmation == 'receipt-confirmed'
    assert result.discord.confirmation == 'independently-read-back'
    assert result.success


def test_mirror_enabled_without_credentials_degrades_instead_of_aborting(artifact_dir):
    """The documented recovery step must not be able to reproduce the incident."""
    data = (artifact_dir / 'index.html').read_bytes()
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true')
    protocol = PagesOnly(data)
    result = deliver(artifact_dir, protocol, env=env)
    assert result.mirror.confirmation == 'mirror-unavailable'
    assert result.mirror.error_code == 'missing_config'
    assert result.success


def test_netlify_token_never_appears_in_mirror_receipts_logs_or_brief(artifact_dir, caplog):
    """Restores the credential coverage the default ENV no longer provides."""
    import logging
    data = (artifact_dir / 'index.html').read_bytes()
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true',
               NETLIFY_AUTH_TOKEN=MIRROR_ENV['NETLIFY_AUTH_TOKEN'],
               NETLIFY_SITE_ID=MIRROR_ENV['NETLIFY_SITE_ID'])

    class BrokenMirror(PagesOnly):
        def __call__(self, request):
            if (request.headers['host'] == 'api.netlify.com' and request.method == 'POST'
                    and request.url.path.endswith('/deploys')):
                self.calls.append(request)
                return httpx.Response(403, json={'error': 'Account credit usage exceeded'})
            return super().__call__(request)

    protocol = BrokenMirror(data)
    with caplog.at_level(logging.DEBUG):
        result = deliver(artifact_dir, protocol, env=env)
    combined = repr(result) + str(result.mirror) + caplog.text + protocol.brief
    assert MIRROR_ENV['NETLIFY_AUTH_TOKEN'] not in combined
    assert 'credit usage exceeded' not in combined.lower()
    for name in ('TELEGRAM_BOT_TOKEN', 'DISCORD_BOT_TOKEN'):
        assert ENV[name] not in combined


def test_mirror_url_never_reaches_the_customer_brief(artifact_dir):
    data = (artifact_dir / 'index.html').read_bytes()
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true',
               NETLIFY_AUTH_TOKEN=MIRROR_ENV['NETLIFY_AUTH_TOKEN'],
               NETLIFY_SITE_ID=MIRROR_ENV['NETLIFY_SITE_ID'])
    protocol = PagesOnly(data)
    result = deliver(artifact_dir, protocol, env=env)
    assert result.mirror.confirmed is True
    assert MIRROR_URL not in result.brief
    assert protocol.brief is not None and MIRROR_URL not in protocol.brief
    assert PAGES in result.brief


def test_pages_is_verified_before_any_mirror_deploy_is_attempted(artifact_dir):
    """A run that will abort must not create a third-party deploy first."""
    data = (artifact_dir / 'index.html').read_bytes()
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true',
               NETLIFY_AUTH_TOKEN=MIRROR_ENV['NETLIFY_AUTH_TOKEN'],
               NETLIFY_SITE_ID=MIRROR_ENV['NETLIFY_SITE_ID'])

    class BrokenPages(PagesOnly):
        def __call__(self, request):
            if request.headers['host'] == 'example.pages.dev':
                self.calls.append(request)
                return httpx.Response(200, content=b'not-the-verified-report')
            return super().__call__(request)

    protocol = BrokenPages(data)
    with pytest.raises(module().DeliveryError, match='html_mismatch'):
        deliver(artifact_dir, protocol, env=env)
    assert not any(r.headers['host'] == 'api.netlify.com' for r in protocol.calls), \
        'Netlify must not be touched when the required host has already failed'


def test_toggling_the_mirror_does_not_change_receipt_bindings(artifact_dir, tmp_path):
    """A byte-identical artifact must not resend when the mirror flag flips."""
    data = (artifact_dir / 'index.html').read_bytes()
    state = tmp_path / 'receipts.json'
    first = deliver(artifact_dir, PagesOnly(data), env=PAGES_ENV, state_path=state)
    assert first.success
    env = dict(PAGES_ENV, NETLIFY_MIRROR_ENABLED='true',
               NETLIFY_AUTH_TOKEN=MIRROR_ENV['NETLIFY_AUTH_TOKEN'],
               NETLIFY_SITE_ID=MIRROR_ENV['NETLIFY_SITE_ID'])
    second = deliver(artifact_dir, PagesOnly(data), env=env, state_path=state)
    assert second.telegram.confirmation == 'previously-confirmed'
    assert second.discord.confirmation == 'previously-confirmed'
    assert second.success
