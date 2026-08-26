# -*- coding: utf-8 -*-
"""run_voice_turn 慢工具开始语（tool_progress）独立播放行为测试。"""
import queue
import unittest
from unittest import mock

from speech.voice_core.turn import run_voice_turn


class ToolProgressIndependentSpeakTest(unittest.TestCase):
    """开始语走 on_tool_progress 回调，不进 duplex 队列。"""

    def _run(self, items, on_tool_progress=None, use_duplex=False, non_duplex_speak=None):
        segments = queue.Queue()
        sink = mock.Mock()
        if use_duplex:
            worker_holder = {}

            def fake_duplex(segments, **kwargs):
                worker_holder["segments"] = segments
                # 模拟 duplex：直接消费队列（不真正合成）
                while True:
                    seg = segments.get()
                    if seg is None:
                        break

            outcome, reply = run_voice_turn(
                "测试命令",
                iter(items),
                "FakeTTS",
                {},
                sink,
                "http://127.0.0.1:8002",
                log=lambda *a: None,
                stage_log=lambda *a, **k: None,
                stage_log_at=lambda *a, **k: None,
                use_duplex=use_duplex,
                on_tool_progress=on_tool_progress,
            )
            return outcome, reply, worker_holder.get("segments")

        outcome, reply = run_voice_turn(
            "测试命令",
            iter(items),
            "FakeTTS",
            {},
            sink,
            "http://127.0.0.1:8002",
            log=lambda *a: None,
            stage_log=lambda *a, **k: None,
            stage_log_at=lambda *a, **k: None,
            use_duplex=use_duplex,
            non_duplex_speak=non_duplex_speak,
            on_tool_progress=on_tool_progress,
        )
        return outcome, reply, None

    def test_progress_goes_to_callback_not_duplex_queue(self):
        spoken = []
        outcome, reply, dup_segments = self._run(
            [
                {"kind": "tool_progress", "text": "好，我去搜一下，稍等。"},
                "今天的天气是晴天。",
            ],
            on_tool_progress=spoken.append,
            use_duplex=True,
        )
        # 开始语进回调，不进 duplex 队列
        self.assertEqual(spoken, ["好，我去搜一下，稍等。"])
        self.assertEqual(outcome, "completed")
        self.assertEqual(reply, "今天的天气是晴天。")

    def test_progress_without_callback_falls_back_to_queue(self):
        """未提供回调时保持旧行为：开始语进播放队列。"""
        spoken = []
        outcome, reply, dup_segments = self._run(
            [
                {"kind": "tool_progress", "text": "好，我去搜一下，稍等。"},
                "结果来了。",
            ],
            use_duplex=True,
        )
        self.assertEqual(outcome, "completed")
        self.assertEqual(reply, "结果来了。")

    def _drain(self, segments, *args):
        while True:
            seg = segments.get()
            if seg is None:
                break

    def test_progress_callback_error_does_not_kill_turn(self):
        """回调抛错不应中断整轮回复。"""
        def boom(text):
            raise RuntimeError("tts down")

        outcome, reply, _ = self._run(
            [
                {"kind": "tool_progress", "text": "好，我去搜一下，稍等。"},
                "结果来了。",
            ],
            on_tool_progress=boom,
            use_duplex=False,
            non_duplex_speak=self._drain,
        )
        self.assertEqual(outcome, "completed")
        self.assertEqual(reply, "结果来了。")


if __name__ == "__main__":
    unittest.main()
