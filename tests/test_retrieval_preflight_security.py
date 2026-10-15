"""Focused security preflight tests for publication boundary.

Validates that load_artifact refuses private source material before any
network write or channel notification, exercising both the Windows path
canary and credential-shape canary paths.
"""
import hashlib
import re

import pytest
from test_report_delivery import artifact_dir, module
from test_daily_delivery import daily_export_dir, sample_daily_digest, sample_daily_html


@pytest.fixture(params=["daily_export_dir", "artifact_dir"], ids=["daily", "legacy"])
def publication_dir(request):
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize("canary", [
    b"<p>api_key&#61;CANARY_PRIVATE_TOKEN</p>",
    b"<p>sk-SYNTHETICCANARY123456789</p>",
    b"<p>C:/Projects/synthetic-canary.env</p>",
    b"<p>/home/synthetic-canary/.env</p>",
], ids=["encoded-assignment", "credential", "windows-path", "posix-path"])
def test_decoded_private_content_is_refused_in_both_formats(publication_dir, canary):
    index = publication_dir / "index.html"
    original = index.read_bytes()
    assert module().load_artifact(publication_dir).data == original
    assert b"</body>" in original
    index.write_bytes(original.replace(b"</body>", canary + b"</body>"))
    with pytest.raises(module().DeliveryError) as caught:
        module().load_artifact(publication_dir)
    assert caught.value.code == "private_artifact"
    assert str(caught.value) == "private_artifact"
    assert repr(caught.value) == "DeliveryError('private_artifact')"


def test_empty_credential_label_is_accepted(publication_dir):
    index = publication_dir / "index.html"
    data = index.read_bytes().replace(b"</body>", b"<p>token:</p></body>")
    index.write_bytes(data)
    assert module().load_artifact(publication_dir).data == data


def test_public_urls_and_explanatory_words_are_accepted(publication_dir):
    index = publication_dir / "index.html"
    benign = (
        b'<p><a href="https://github.com/home/example">Public repository</a> '
        b'https://github.com/users/example '
        b'email-and-password-best-practices sk-matt '
        b'API key rotation and token usage are explained here.</p>'
    )
    data = index.read_bytes().replace(b"</body>", benign + b"</body>")
    index.write_bytes(data)
    assert module().load_artifact(publication_dir).data == data


@pytest.mark.parametrize("newline", [b"\n", b"\r\n", b"\r"], ids=["lf", "crlf", "cr"])
def test_accepted_artifact_preserves_exact_input_bytes(publication_dir, newline):
    index = publication_dir / "index.html"
    data = index.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    data = data.replace(
        b"</body>", '<p title="public &amp; safe">café &#61; public</p>\n</body>'.encode("utf-8")
    ).replace(b"\n", newline)
    index.write_bytes(data)
    artifact = module().load_artifact(publication_dir)
    assert artifact.data == data
    assert index.read_bytes() == data
    assert artifact.sha1 == hashlib.sha1(data).hexdigest()
    assert artifact.sha256 == hashlib.sha256(data).hexdigest()


@pytest.mark.parametrize("malformation", ["invalid-utf8", "missing-csp", "missing-referrer", "missing-footer"])
def test_malformed_artifacts_remain_rejected(publication_dir, malformation):
    index = publication_dir / "index.html"
    original = index.read_bytes()
    assert module().load_artifact(publication_dir).data == original
    if malformation == "invalid-utf8":
        data = original + b"\xff"
    elif malformation == "missing-csp":
        data = re.sub(b'<meta http-equiv="Content-Security-Policy"[^>]*>', b"", original)
    elif malformation == "missing-referrer":
        data = re.sub(b'<meta name="referrer"[^>]*>', b"", original)
    else:
        data = re.sub(b"<footer\\b[^>]*>.*?</footer>", b"", original, flags=re.DOTALL)
    assert data != original
    index.write_bytes(data)
    with pytest.raises(module().DeliveryError) as caught:
        module().load_artifact(publication_dir)
    assert str(caught.value) == "invalid_artifact"


def test_daily_encoded_credential_assignment_is_refused(daily_export_dir):
    index = daily_export_dir / "index.html"
    index.write_bytes(index.read_bytes().replace(
        b"</body>", b"<p>api_key&#61;CANARY_PRIVATE_TOKEN</p></body>"
    ))
    with pytest.raises(module().DeliveryError):
        module().load_artifact(daily_export_dir)


@pytest.mark.parametrize("canary", [
    "<p>Private source: C:\\Users\\audit-canary\\private.env; token=CANARY_PRIVATE_TOKEN</p>",
])
def test_private_windows_path_and_token_are_refused(artifact_dir, canary):
    """A tampered artifact containing a local Windows path and a token=value
    credential assignment must raise DeliveryError, not be accepted for
    publication."""
    index = artifact_dir / "index.html"
    index.write_text(
        index.read_text(encoding="utf-8").replace("</body>", canary + "</body>"),
        encoding="utf-8",
    )
    with pytest.raises(module().DeliveryError):
        module().load_artifact(artifact_dir)


def test_public_owner_names_are_not_rejected(artifact_dir):
    """GitHub public repository paths that merely contain words like 'token'
    or 'password' in their slug must still publish successfully."""
    index = artifact_dir / "index.html"
    benign = (
        '<p>https://github.com/home/example '
        'email-and-password-best-practices sk-matt</p>'
    )
    index.write_text(
        index.read_text(encoding="utf-8").replace("</body>", benign + "</body>"),
        encoding="utf-8",
    )
    assert module().load_artifact(artifact_dir).data == index.read_bytes()


@pytest.mark.parametrize("credential_pattern", [
    "secret=SUPERSECRETVALUE123",
    "api_key = my-long-key-12345",
    "password:anothersecret",
])
def test_credential_shaped_assignments_are_refused(artifact_dir, credential_pattern):
    """Any token/key/password/secret followed by = or : and a non-empty value
    must be refused regardless of case or whitespace around the separator."""
    index = artifact_dir / "index.html"
    canary = f"<p>{credential_pattern}</p>"
    index.write_text(
        index.read_text(encoding="utf-8").replace("</body>", canary + "</body>"),
        encoding="utf-8",
    )
    with pytest.raises(module().DeliveryError):
        module().load_artifact(artifact_dir)
