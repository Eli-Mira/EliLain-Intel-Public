"""CI topology is security-relevant: preflight -> Pages -> verify -> notify."""
from pathlib import Path
import yaml
ROOT = Path(__file__).resolve().parents[1]


def test_pages_workflow_is_gated_preflighted_and_verified_before_notification():
    workflow = yaml.safe_load((ROOT / '.github/workflows/news-tracker-daily.yml').read_text(encoding='utf-8'))
    steps = workflow['jobs']['run-elilain-audit']['steps']
    pages = [(i, s) for i, s in enumerate(steps) if 'pages deploy' in s.get('with', {}).get('command', s.get('run', ''))]
    assert len(pages) == 1, 'daily Cloudflare deployment is missing'
    pages_i, step = pages[0]
    assert 'vars.CLOUDFLARE_PUBLISH_ENABLED' in step['if']
    assert 'secrets.' not in step['if']
    assert step['with']['apiToken'] == '${{ secrets.CLOUDFLARE_API_TOKEN }}'
    assert step['with']['accountId'] == '${{ secrets.CLOUDFLARE_ACCOUNT_ID }}'
    assert 'pages deploy ./public --project-name=${{ vars.CLOUDFLARE_PAGES_PROJECT }} --branch=main' == step['with']['command']
    assert workflow['env']['PUBLIC_REPORT_URL'] == '${{ vars.PUBLIC_REPORT_URL }}'
    preflight_i = next(i for i, s in enumerate(steps) if '--preflight-only' in s.get('run', ''))
    verify_i = next(i for i, s in enumerate(steps) if s.get('id') == 'verify-pages')
    publish_i = next(i for i, s in enumerate(steps) if 'main.py publish' in s.get('run', ''))
    assert preflight_i < pages_i < verify_i < publish_i
    assert steps[publish_i]['env']['PUBLIC_REPORT_URL'] == '${{ steps.verify-pages.outputs.url }}'
    assert workflow['permissions']['contents'] == 'read'
    for s in steps:
        assert 'continue-on-error' not in s if s.get('id') in {'deploy-pages', 'verify-pages'} else True
        assert 'secrets.' not in s.get('run', '')
