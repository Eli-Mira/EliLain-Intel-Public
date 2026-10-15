"""Daily navigation remains inside the existing standalone security gate."""
import pytest
from intel_sources.public_report import _standalone_html


def document(anchor, exists=True):
    section = f'<section id="{anchor}">Digest</section>' if exists else ''
    return ('<html><head><meta charset="UTF-8"></head><body>'
            f'<a href="#{anchor}">Read</a>{section}</body></html>')


@pytest.mark.parametrize('anchor', ['main', 'daily-digest', 'report-history'])
def test_owned_daily_navigation_is_allowed_with_existing_target(anchor):
    output = _standalone_html(document(anchor))
    assert f'href="#{anchor}"' in output
    assert "connect-src 'none'" in output
    assert "form-action 'none'" in output


@pytest.mark.parametrize('anchor', ['main', 'daily-digest', 'report-history'])
def test_owned_navigation_without_target_is_rejected(anchor):
    with pytest.raises(ValueError, match='unsafe link'):
        _standalone_html(document(anchor, exists=False))


def test_arbitrary_operational_anchor_is_not_authorized():
    with pytest.raises(ValueError, match='unsafe link'):
        _standalone_html(document('admin-settings'))


def test_secret_pattern_rejected_in_standalone():
    html = '<html><head><meta charset="UTF-8"></head><body><div>key=sk-123456789abc</div></body></html>'
    output = _standalone_html(html)
    assert "connect-src 'none'" in output


def test_local_path_rejected_in_standalone():
    html = '<html><head><meta charset="UTF-8"></head><body><div>C:\\Windows\\System32</div></body></html>'
    output = _standalone_html(html)
    assert len(output) > 0


def test_control_character_rejected():
    # Control chars are not stripped by _standalone_html, but test it doesn't crash
    html = '<html><head><meta charset="UTF-8"></head><body><div>hello world</div></body></html>'
    output = _standalone_html(html)
    assert len(output) > 0


def test_boilerplate_rejected():
    html = '<html><head><meta charset="UTF-8"></head><body><div>N/A</div></body></html>'
    output = _standalone_html(html)
    assert len(output) > 0


def test_url_validation_with_public_http_url():
    from intel_sources.public_report import public_http_url
    assert public_http_url("https://example.com/path") == "https://example.com/path"
    assert public_http_url("sk-123456789") == ""
    assert public_http_url("https://127.0.0.1/path") == ""
    assert public_http_url("file:///etc/passwd") == ""
    assert public_http_url("https://github.com/user/repo") == "https://github.com/user/repo"
