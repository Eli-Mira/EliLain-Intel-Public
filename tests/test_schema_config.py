"""Model config migration must preserve contracts and import without deprecation."""
import subprocess
import sys
from pathlib import Path


def test_schema_import_and_examples_survive_with_deprecations_as_errors():
    code = '''
import warnings
from pydantic.warnings import PydanticDeprecatedSince20
warnings.simplefilter('error', PydanticDeprecatedSince20)
from tracker_core.schema import Provider, NewsItem, NewsBatch
for model in (Provider, NewsItem, NewsBatch):
    assert 'Config' not in model.__dict__
    example = model.model_json_schema()['example']
    parsed = model.model_validate(example)
    encoded = parsed.model_dump_json()
    assert model.model_validate_json(encoded) == parsed
assert Provider.model_json_schema()['example']['name'] == 'Google AI Studio'
assert NewsItem.model_json_schema()['example']['title'] == 'OpenAI releases GPT-4o'
assert NewsBatch.model_json_schema()['example']['total_count'] == 0
'''
    result = subprocess.run([sys.executable, '-c', code], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
