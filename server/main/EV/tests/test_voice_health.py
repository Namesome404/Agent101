import json

from diagnostics.voice_health import build_report, load_turn_summaries


def _summary(turn_id, first_audible, llm, outcome="completed"):
    return {
        "time": "2026-08-13T12:00:00+08:00",
        "event": "turn_summary",
        "turn_id": turn_id,
        "outcome": outcome,
        "first_audible_ms": first_audible,
        "slowest_stage": "llm_first_token_ms",
        "slowest_stage_ms": llm,
        "components": {
            "vad_endpoint_ms": 220,
            "asr_after_vad_ms": 300,
            "llm_first_token_ms": llm,
            "text_buffering_ms": 20,
            "tts_first_audio_ms": 250,
        },
    }


def test_health_report_percentiles_and_runtime(tmp_path, monkeypatch):
    path = tmp_path / "diag.jsonl"
    events = [
        {"event": "turn_stage"},
        _summary("a", 1000, 400),
        _summary("b", 2000, 1000),
        _summary("c", 3000, 1600),
    ]
    path.write_text("\n".join(json.dumps(item) for item in events), encoding="utf-8")
    monkeypatch.setenv("VOICE_FIRST_AUDIBLE_TARGET_MS", "2500")
    report = build_report(
        path,
        limit=10,
        local_voice={"running": True, "pid": 12, "age_ms": 50},
    )
    assert report["sample"]["completed"] == 3
    assert report["slo"]["first_audible_p50_ms"] == 2000
    assert report["slo"]["first_audible_p95_ms"] == 2900
    assert report["slo"]["within_target_ratio"] == 0.667
    assert report["components"]["llm_first_token_ms"]["p50_ms"] == 1000
    assert report["health"] == "slow"


def test_loader_accepts_pretty_json_spacing(tmp_path):
    path = tmp_path / "diag.jsonl"
    path.write_text(json.dumps(_summary("a", 1000, 400), ensure_ascii=False), encoding="utf-8")
    assert len(load_turn_summaries(path, limit=5)) == 1


def test_user_interrupt_is_not_counted_as_failure(tmp_path):
    path = tmp_path / "diag.jsonl"
    path.write_text("\n".join(json.dumps(item) for item in [
        _summary("a", 1000, 400),
        _summary("b", None, 0, outcome="interrupted"),
    ]), encoding="utf-8")
    report = build_report(path, local_voice={"running": True})
    assert report["sample"]["failures"] == 0
    assert report["sample"]["interrupted"] == 1
