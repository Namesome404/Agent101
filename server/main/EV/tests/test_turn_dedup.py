# -*- coding: utf-8 -*-
"""run_voice_turn 播放前防重复（原句/近义句重复丢弃）行为测试。"""
import unittest
from unittest import mock

from speech.voice_core.turn import run_voice_turn


class PlaybackDedupTest(unittest.TestCase):
    """模型偶发把同一句话重复两遍时，播放段去重但 reply 保留原文。"""

    def _collect(self, items):
        collected = []

        def collector(segments, provider, overrides):
            while True:
                seg = segments.get()
                if seg is None:
                    break
                collected.append(seg)

        outcome, reply = run_voice_turn(
            "测试命令",
            iter(items),
            "FakeTTS",
            {},
            mock.Mock(),
            "http://127.0.0.1:8002",
            log=lambda *a: None,
            stage_log=lambda *a, **k: None,
            stage_log_at=lambda *a, **k: None,
            use_duplex=False,
            non_duplex_speak=collector,
        )
        return outcome, reply, collected

    def test_exact_duplicate_dropped(self):
        outcome, reply, collected = self._collect(
            ["温度计窗口开了。", "温度计窗口开了。"]
        )
        self.assertEqual(outcome, "completed")
        # reply 保留模型原文（文本记录不改写），播放段去重
        self.assertEqual(reply, "温度计窗口开了。温度计窗口开了。")
        self.assertEqual(collected, ["温度计窗口开了。"])

    def test_near_duplicate_dropped(self):
        """第二段包含第一段且长度相近 → 高度疑似复述，丢弃。"""
        outcome, reply, collected = self._collect(
            ["窗口开了，但内容渲染报错。", "窗口开了，但内容渲染报错，显示不出来。"]
        )
        self.assertEqual(outcome, "completed")
        self.assertEqual(len(collected), 1)

    def test_distinct_segments_kept(self):
        outcome, reply, collected = self._collect(["关了。", "还有别的吗？"])
        self.assertEqual(outcome, "completed")
        self.assertEqual(collected, ["关了。", "还有别的吗？"])

    def test_repetition_resets_with_new_content(self):
        """新内容插入后，不再拦截旧的重复判定。"""
        outcome, reply, collected = self._collect(
            ["温度计开了。", "你要看吗？", "温度计开了。"]
        )
        self.assertEqual(outcome, "completed")
        # 第三段与第一段相同，但中间隔了第二段 → 仍被判重复（最近3段含第一段）
        self.assertEqual(collected, ["温度计开了。", "你要看吗？"])


if __name__ == "__main__":
    unittest.main()
