"""Read-only CI preflight/readback CLI regression."""
from pathlib import Path
import hashlib
import json
import os
import subprocess
import sys
from test_report_delivery import artifact_dir
ROOT = Path(__file__).resolve().parents[1]


def test_preflight_cli_preserves_bytes_and_writes_no_hosted_url(artifact_dir, tmp_path):
    script = ROOT / 'scripts/verify_pages_report.py'
    assert script.is_file(), 'safe Pages preflight CLI is missing'
    output = tmp_path / 'github-output'
    data = (artifact_dir / 'index.html').read_bytes()
    run = subprocess.run([sys.executable, str(script), '--public-dir', str(artifact_dir), '--preflight-only'],
        cwd=ROOT, env={**os.environ, 'GITHUB_OUTPUT': str(output)}, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    receipt = json.loads(run.stdout)
    assert receipt['sha256'] == hashlib.sha256(data).hexdigest()
    assert receipt['bytes'] == len(data)
    assert receipt['verified'] is False
    assert not output.exists(), 'unverified preflight must not emit a hosted URL'
    assert (artifact_dir / 'index.html').read_bytes() == data
    assert sorted(p.name for p in artifact_dir.iterdir()) == ['index.html']
