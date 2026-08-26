"""护栏体检报告的读数不能骗人。

这个报告存在的意义是回答「哪层补偿层真管用」。它自己要是把轮次归并错了、
把注入类和触发类混为一谈，那就比没有更糟——会拿一个假数字去指导删代码。
"""

import json

from diagnostics import guard_report


def _write_trace(tmp_path, monkeypatch, events):
    path = tmp_path / "voice_tool_trace.jsonl"
    path.write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events),
        encoding="utf-8",
    )
    monkeypatch.setattr(guard_report, "TRACE_PATH", path)
    return path


def test_turns_are_grouped_by_turn_id_not_by_line(tmp_path, monkeypatch):
    """一轮里有 user / tool_call / assistant 好几条，必须按 turn_id 归并。

    之前统计工具调用时按行窗口数过两次，两次给出互相矛盾的数字。
    """
    _write_trace(tmp_path, monkeypatch, [
        {"turn_id": "a", "event": "user", "data": {"text": "把灯关了"}},
        {"turn_id": "a", "event": "tool_call", "data": {"name": "object_control"}},
        {"turn_id": "a", "event": "assistant", "data": {
            "text": "关了", "guards": {"had_tool_call": True, "action_rounds": 2},
        }},
        {"turn_id": "b", "event": "user", "data": {"text": "不错"}},
        {"turn_id": "b", "event": "assistant", "data": {
            "text": "嗯", "guards": {"had_tool_call": False, "action_rounds": 1},
        }},
    ])
    turns = guard_report._turns()
    assert len(turns) == 2
    assert turns[0]["user"] == "把灯关了"
    assert turns[0]["guards"]["had_tool_call"] is True
    assert turns[1]["guards"]["had_tool_call"] is False


def test_turns_without_a_guard_sheet_are_ignored(tmp_path, monkeypatch):
    """埋点上线之前的老轮次没有体检表，不能混进样本充数。"""
    _write_trace(tmp_path, monkeypatch, [
        {"turn_id": "old", "event": "user", "data": {"text": "旧的一轮"}},
        {"turn_id": "old", "event": "assistant", "data": {"text": "没有 guards"}},
        {"turn_id": "new", "event": "assistant", "data": {
            "text": "有", "guards": {"had_tool_call": True},
        }},
    ])
    turns = guard_report._turns()
    assert len(turns) == 1


def test_last_n_takes_the_most_recent(tmp_path, monkeypatch):
    _write_trace(tmp_path, monkeypatch, [
        {"turn_id": str(i), "event": "assistant", "data": {
            "text": str(i), "guards": {"action_rounds": i},
        }}
        for i in range(5)
    ])
    assert [t["guards"]["action_rounds"] for t in guard_report._turns(last=2)] == [3, 4]


def test_retry_reason_is_read_off_the_guard_sheet(tmp_path, monkeypatch):
    """回炉原因要能分门别类，否则「命中 12 次」说明不了是哪种毛病。"""
    _write_trace(tmp_path, monkeypatch, [
        {"turn_id": "a", "event": "answer_retry", "data": {"reason": "unbacked_claim"}},
        {"turn_id": "a", "event": "assistant", "data": {"text": "x", "guards": {
            "retry_reason": "unbacked_claim", "forced_required": True,
            "had_mutation_receipt": True,
        }}},
        {"turn_id": "b", "event": "assistant", "data": {"text": "y", "guards": {
            "retry_reason": "weak_evidence", "had_mutation_receipt": False,
        }}},
    ])
    turns = guard_report._turns()
    reasons = sorted(t["guards"]["retry_reason"] for t in turns)
    assert reasons == ["unbacked_claim", "weak_evidence"]
    # 回炉之后有没有真拿到回执，是判断这层管不管用的关键，不能只数命中
    assert [t["guards"].get("had_mutation_receipt") for t in turns] == [True, False]


def test_report_runs_on_an_empty_trace(tmp_path, monkeypatch, capsys):
    """没有样本时给一句人话，不是崩掉或者打印 0/0。"""
    _write_trace(tmp_path, monkeypatch, [])
    guard_report.report()
    assert "先正常说几句话" in capsys.readouterr().out
