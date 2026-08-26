# -*- coding: utf-8 -*-
from pathlib import Path

from devices.coding.agent_runtime import evidence
from devices.coding.agent_runtime.protocol import event, public_event
from devices.coding.agent_runtime.codex_app_server import CodexAppServerRun


def test_filesystem_evidence_tracks_content_not_mtime(tmp_path: Path):
    target = tmp_path / "main.py"
    target.write_text("one", encoding="utf-8")
    before = evidence.file_manifest(tmp_path)
    target.write_text("two", encoding="utf-8")
    after = evidence.file_manifest(tmp_path)
    assert evidence.changed_paths(before, after) == ["main.py"]
    artifacts = evidence.artifacts(tmp_path, ["main.py"], after)
    assert artifacts[0]["path"] == "main.py"
    assert artifacts[0]["deleted"] is False


def test_public_event_never_exposes_reasoning_payload():
    item = event("activity", detail="正在检查", phase="reading", reasoning="private", raw="private")
    visible = public_event(item)
    assert visible["detail"] == "正在检查"
    assert "reasoning" not in visible
    assert "raw" not in visible


def test_codex_transient_reconnect_is_not_terminal(tmp_path: Path):
    seen = []
    run = CodexAppServerRun(run_id="test", cwd=tmp_path, base_url="http://127.0.0.1:8002", on_event=seen.append)
    status = run._handle_notification({
        "method": "error",
        "params": {"willRetry": True, "error": {"message": "Reconnecting... 1/5"}},
    })
    assert status is None
    assert seen[-1]["kind"] == "runtime.retrying"
    assert seen[-1]["phase"] == "working"


def test_codex_fatal_error_ends_turn(tmp_path: Path):
    run = CodexAppServerRun(run_id="test", cwd=tmp_path, base_url="http://127.0.0.1:8002")
    status = run._handle_notification({
        "method": "error",
        "params": {"willRetry": False, "error": {"message": "authentication failed"}},
    })
    assert status == "failed"
    assert run._last_error == "authentication failed"
