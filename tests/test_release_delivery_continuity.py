"""Customer regressions for receipt continuity; no real deploys or sends."""
from __future__ import annotations

import json

import httpx
import pytest

from test_report_delivery import ENV, Guard, Protocol, artifact_dir, module, run


def test_changed_telegram_destination_does_not_reuse_old_confirmation(artifact_dir, tmp_path):
    state = tmp_path / "delivery_receipts.json"
    data = (artifact_dir / "index.html").read_bytes()
    run(artifact_dir, Protocol(data), state_path=state)
    other_env = dict(ENV, TELEGRAM_CHAT_ID="-1009999999999")

    def other_chat(request):
        if request.headers["host"] != "api.telegram.org":
            return None
        if request.url.path.endswith("/sendMessage"):
            body = json.loads(request.content)
            assert body["chat_id"] == other_env["TELEGRAM_CHAT_ID"]
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 52,
                "chat": {"id": int(other_env["TELEGRAM_CHAT_ID"])}, "text": body["text"]}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 53,
            "chat": {"id": int(other_env["TELEGRAM_CHAT_ID"])},
            "document": {"file_name": "index.html", "file_size": len(data)}}})

    protocol = Guard(data, other_chat)
    result = module().deliver_report(artifact_dir, environ=other_env,
        transport=httpx.MockTransport(protocol), resolver=lambda host: ["8.8.8.8"],
        sleep=lambda seconds: None, state_path=state)
    assert result.telegram.confirmation == "receipt-confirmed"
    assert result.telegram.message_id == "52"
    assert result.discord.confirmation == "previously-confirmed"
    assert sum(request.headers["host"] == "api.telegram.org" for request in protocol.calls) == 1
    persisted = state.read_text(encoding="utf-8")
    assert other_env["TELEGRAM_CHAT_ID"] not in persisted
    assert all(ENV[name] not in persisted for name in
               ("TELEGRAM_BOT_TOKEN", "DISCORD_BOT_TOKEN"))


@pytest.mark.parametrize("contents", [
    "{not-json", json.dumps({"artifacts": []}),
    json.dumps({"artifacts": {"0" * 64: "wrong-record"}}),
    json.dumps({"artifacts": {"0" * 64: {"telegram": ["42/43"]}}}),
])
def test_corrupt_receipts_refused_before_any_external_write(artifact_dir, tmp_path, contents):
    state = tmp_path / "delivery_receipts.json"
    state.write_text(contents, encoding="utf-8")
    data = (artifact_dir / "index.html").read_bytes()
    protocol = Protocol(data)
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, protocol, state_path=state)
    assert caught.value.code == "invalid_receipt_state"
    assert protocol.calls == []
    assert state.read_text(encoding="utf-8") == contents


def test_workflow_preserves_confirmed_receipts_even_if_publish_fails():
    from pathlib import Path
    import yaml
    workflow = yaml.safe_load((Path(__file__).resolve().parents[1] /
        ".github/workflows/news-tracker-daily.yml").read_text(encoding="utf-8"))
    steps = workflow["jobs"]["run-elilain-audit"]["steps"]
    restores = [step for step in steps if step.get("uses", "").startswith("actions/cache/restore@")]
    saves = [step for step in steps if step.get("uses", "").startswith("actions/cache/save@")]
    assert len(restores) == len(saves) == 1, "success-only post-cache loses receipts on channel failure"
    assert "always()" in saves[0].get("if", "")
    # refresh-intel (today/audit) step was removed; save condition simplified.
    assert "steps.refresh-intel" not in saves[0].get("if", "")
    assert set(restores[0]["with"]["path"].splitlines()) == set(saves[0]["with"]["path"].splitlines())
    # Daily state paths are cached; delivery_receipts.json and cloudflare_publication.json
    # are also cached to preserve delivery continuity across runs.
    assert "data/daily_state/history.jsonl" in saves[0]["with"]["path"].splitlines()
    assert "data/delivery_receipts.json" in saves[0]["with"]["path"].splitlines()
    assert "data/cloudflare_publication.json" in saves[0]["with"]["path"].splitlines()
    publish_index = next(index for index, step in enumerate(steps) if "main.py publish" in step.get("run", ""))
    assert steps.index(saves[0]) > publish_index


def test_partial_failure_restart_does_not_resend_confirmed_channel(artifact_dir, tmp_path):
    state = tmp_path / "delivery_receipts.json"
    data = (artifact_dir / "index.html").read_bytes()
    first = Guard(data, lambda request: request.headers["host"] == "discord.com"
                  and request.method == "POST" and httpx.Response(403, json={"message": "denied"}))
    with pytest.raises(module().DeliveryError) as caught:
        run(artifact_dir, first, state_path=state)
    assert caught.value.code == "channel_failure"
    assert caught.value.result.telegram.confirmed

    retry = Protocol(data)
    result = run(artifact_dir, retry, state_path=state)
    assert result.success
    assert result.telegram.confirmation == "previously-confirmed"
    assert result.discord.confirmation == "independently-read-back"
    assert not any(request.headers["host"] == "api.telegram.org" for request in retry.calls)
    assert sum(request.method == "POST" and request.headers["host"] == "discord.com"
               for request in retry.calls) == 1
