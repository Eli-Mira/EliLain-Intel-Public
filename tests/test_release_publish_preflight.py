"""Unsafe-file regressions: loader only, never deployment or notification."""
from pathlib import Path
import pytest
from test_report_delivery import artifact_dir, module


@pytest.mark.parametrize('canary', [
    '<script src="relative-canary.js"></script>',
    '<form action="https://example.com/canary"><button>submit</button></form>',
    '<p>Private source: C:\\Users\\audit-canary\\private.env; token=CANARY_PRIVATE_TOKEN</p>',
])
def test_publication_refuses_export_boundary_bypasses(artifact_dir, canary):
    index = artifact_dir / 'index.html'
    index.write_text(index.read_text(encoding='utf-8').replace('</body>', canary + '</body>'), encoding='utf-8')
    with pytest.raises(module().DeliveryError):
        module().load_artifact(artifact_dir)


def test_oversized_artifact_is_refused_before_read(artifact_dir, monkeypatch):
    index = artifact_dir / 'index.html'
    with index.open('ab') as handle:
        handle.write(b' ' * module().MAX_HTML_BYTES)
    original_open = Path.open

    def no_oversized_read(self, *args, **kwargs):
        if self == index:
            raise AssertionError('an oversized file was opened before refusal')
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', no_oversized_read)
    with pytest.raises(module().DeliveryError, match='artifact_too_large'):
        module().load_artifact(artifact_dir)


def test_publication_refuses_missing_csp(artifact_dir):
    import re
    index = artifact_dir / 'index.html'
    text = re.sub(r'<meta http-equiv="Content-Security-Policy"[^>]*>', '',
                  index.read_text(encoding='utf-8'))
    index.write_text(text, encoding='utf-8')
    with pytest.raises(module().DeliveryError):
        module().load_artifact(artifact_dir)


def test_private_path_scan_does_not_reject_public_owner_names(artifact_dir):
    index = artifact_dir / 'index.html'
    benign = '<p>https://github.com/home/example email-and-password-best-practices sk-matt</p>'
    index.write_text(index.read_text(encoding='utf-8').replace('</body>', benign + '</body>'), encoding='utf-8')
    assert module().load_artifact(artifact_dir).data == index.read_bytes()


def test_publication_refuses_a_reparse_export_directory(artifact_dir, monkeypatch):
    from types import SimpleNamespace
    original = Path.lstat

    def attributes(self, *args, **kwargs):
        if self == artifact_dir:
            return SimpleNamespace(st_file_attributes=0x400)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'lstat', attributes)
    with pytest.raises(module().DeliveryError, match='unexpected_files'):
        module().load_artifact(artifact_dir)
