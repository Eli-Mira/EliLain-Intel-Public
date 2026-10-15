import json
from datetime import datetime, timezone

from freellmapi_tracker.models import FreeProvider, ProviderProbeResult


def test_result_and_provider_evidence_are_json_safe_and_secret_free():
    checked = datetime(2026, 9, 30, 10, tzinfo=timezone.utc)
    result = ProviderProbeResult("groq", False, "tiny", timestamp=checked)
    result.error_message = "Authorization: Bearer opaque-fixture-secret https://host/a?api_key=url-fixture-secret sk-fixture-secret"
    result.evidence = [{"at": checked, "Authorization": "opaque-fixture-secret", "nested": {"api_key": "opaque-fixture-secret"}, "message": result.error_message}]
    result.attempted_models = ["tiny"]
    result.response_category = "auth"
    encoded = json.dumps(result.to_dict())
    for secret in ("opaque-fixture-secret", "url-fixture-secret", "sk-fixture-secret"):
        assert secret not in encoded
    assert result.checked_at == checked == result.timestamp
    assert result.to_dict()["probe_source"] == "local:freellmapi"
    assert result.to_dict()["attempted_models"] == ["tiny"]
    assert result.to_dict()["response_category"] == "auth"
    provider = FreeProvider("Groq", "groq", checked_at=checked, probe_evidence=result.evidence)
    assert "opaque-fixture-secret" not in json.dumps(provider.to_dict())


def test_probe_reason_is_sanitized_before_application_or_rendering(tmp_path):
    from freellmapi_tracker.tracker import FreeProviderTracker
    result = ProviderProbeResult("groq", False, "tiny", http_status=401,
                                error_message="Authorization: Bearer opaque-fixture-secret")
    assert "opaque-fixture-secret" not in result.to_status()[1]
    tracker = FreeProviderTracker(tmp_path / "missing.db", tmp_path / "state.json")
    tracker.providers["groq"] = FreeProvider("Groq", "groq")
    p = tracker.apply_probe_result("groq", result)
    assert "opaque-fixture-secret" not in p.status_reason
    assert p.last_verified == p.checked_at == result.checked_at
    assert "opaque-fixture-secret" not in p.freellmapi_action


def test_malformed_secret_urls_cannot_break_evidence_serialization():
    result = ProviderProbeResult("groq", False, "tiny", evidence=[{"message": "https://[invalid?token=fixture-url-secret"}])
    encoded = json.dumps(result.to_dict())
    assert "fixture-url-secret" not in encoded
    assert "REDACTED_URL" in encoded
