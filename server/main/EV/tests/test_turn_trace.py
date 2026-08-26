import tempfile
import unittest
from pathlib import Path
from unittest import mock

from devices.coding import turn_trace


class TurnTraceTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patches = [
            mock.patch.object(turn_trace, "ACTION_TRACE_PATH", root / "actions.jsonl"),
            mock.patch.object(turn_trace, "TRACE_PATH", root / "voice.jsonl"),
        ]
        for patch in self.patches:
            patch.start()
        turn_trace._TURN_STATE.clear()
        turn_trace._RECENT_USERS.clear()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        turn_trace._TURN_STATE.clear()
        turn_trace._RECENT_USERS.clear()
        self.temp.cleanup()

    def test_action_with_runtime_receipt_has_no_anomaly(self):
        turn = "ok-turn"
        turn_trace.record(turn, "user", {"text": "打开窗口"})
        turn_trace.record(turn, "decision", {
            "scope": "surface_manage", "addressed": True,
        })
        turn_trace.record(turn, "tool_call", {
            "name": "surface_manage", "arguments": {"action": "open"},
        })
        turn_trace.record(turn, "tool_result", {
            "name": "surface_manage", "result": {"ok": True, "rendered": True},
        })
        final = turn_trace.record(turn, "assistant", {
            "text": "窗口已打开", "tool_name": "surface_manage",
        })
        self.assertEqual(final["anomalies"], [])

    def test_action_without_tool_is_structurally_flagged(self):
        turn = "bad-turn"
        turn_trace.record(turn, "user", {"text": "打开窗口"})
        turn_trace.record(turn, "decision", {
            "scope": "surface_manage", "addressed": True,
        })
        final = turn_trace.record(turn, "assistant", {
            "text": "随便什么措辞", "tool_name": "none",
        })
        self.assertIn("action_scope_without_tool_call", final["anomalies"])

    def test_scene_action_inherits_voice_turn_context(self):
        with turn_trace.action_context("turn-7", actor="voice_tool:surface_manage"):
            item = turn_trace.record_runtime(
                "scene.upsert", {"surface_id": "timer"}, category="scene",
            )
        self.assertEqual(item["turn_id"], "turn-7")
        self.assertEqual(item["data"]["actor"], "voice_tool:surface_manage")

    def test_partial_and_final_asr_requests_are_correlated(self):
        turn_trace.record("partial", "user", {
            "agent_id": 1, "text": "现在按键不实时",
        })
        final = turn_trace.record("final", "user", {
            "agent_id": 1, "text": "现在按键不实时，你能检测并更改吗",
        })
        self.assertIn("overlapping_utterance_requests", final["anomalies"])
        self.assertEqual(final["related_turn_id"], "partial")

    def test_read_recent_executions_pairs_call_and_result(self):
        turn_trace.record("t-close", "tool_call", {
            "name": "surface_manage", "arguments": {"action": "close", "surface_id": "timer-3mo"},
        })
        turn_trace.record("t-close", "tool_result", {
            "name": "surface_manage", "result": {"ok": True, "action": "close", "surface_id": "timer-3mo"},
        })
        turn_trace.record("t-open", "tool_call", {
            "name": "surface_manage", "arguments": {"action": "open", "surface_id": "game"},
        })
        turn_trace.record("t-open", "tool_result", {
            "name": "surface_manage", "result": {"ok": True, "action": "open", "surface_id": "game"},
        })
        turn_trace.record("t-chat", "tool_call", {
            "name": "web_search", "arguments": {"query": "天气"},
        })
        executions = turn_trace.read_recent_executions(turns=3)
        self.assertEqual(len(executions), 3)
        # 最新在前：t-chat 的 web_search 应排最前
        self.assertEqual(executions[0]["name"], "web_search")
        self.assertIsNone(executions[0]["result"])
        # t-open 的 open 配对到了 ok:true
        open_entry = next(e for e in executions if e["name"] == "surface_manage" and
                          (e.get("arguments") or {}).get("action") == "open")
        self.assertTrue((open_entry["result"] or {}).get("ok"))

    def test_read_recent_executions_excludes_current_turn(self):
        turn_trace.record("old", "tool_call", {
            "name": "surface_manage", "arguments": {"action": "close", "surface_id": "a"},
        })
        turn_trace.record("old", "tool_result", {
            "name": "surface_manage", "result": {"ok": True},
        })
        turn_trace.record("now", "tool_call", {
            "name": "surface_manage", "arguments": {"action": "close", "surface_id": "b"},
        })
        turn_trace.record("now", "tool_result", {
            "name": "surface_manage", "result": {"ok": True},
        })
        executions = turn_trace.read_recent_executions(turns=3, exclude_turn_id="now")
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]["turn_id"], "old")


if __name__ == "__main__":
    unittest.main()
