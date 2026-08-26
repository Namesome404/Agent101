# -*- coding: utf-8 -*-
"""慢工具开始语去重 guard 行为测试。"""
import unittest

from devices.voice import terminal_state as terminal_mod


class ToolProgressGuardTest(unittest.TestCase):
    def setUp(self):
        self._saved = terminal_mod._TOOL_ACK_GUARD
        terminal_mod._TOOL_ACK_GUARD = {
            "key": "", "at": 0.0, "text": "", "progress_key": "",
        }

    def tearDown(self):
        terminal_mod._TOOL_ACK_GUARD = self._saved

    def test_first_progress_claims(self):
        self.assertTrue(
            terminal_mod._claim_tool_progress("查一下天气", "好，我去搜一下，稍等。")
        )

    def test_same_command_progress_deduped(self):
        self.assertTrue(
            terminal_mod._claim_tool_progress("查一下天气", "好，我去搜一下，稍等。")
        )
        # 同一 command 的第二个 progress 应被丢弃
        self.assertFalse(
            terminal_mod._claim_tool_progress("查一下天气", "再等一下，马上好。")
        )

    def test_different_command_progress_not_deduped(self):
        self.assertTrue(
            terminal_mod._claim_tool_progress("查一下天气", "好，我去搜一下，稍等。")
        )
        # 不同 command 的 progress 不受影响
        self.assertTrue(
            terminal_mod._claim_tool_progress("看看新闻", "好，我去查一下，稍等。")
        )

    def test_progress_does_not_block_tool_ack(self):
        """开始语 claim 不应吞掉同 command 的工具完成 ack。"""
        self.assertTrue(
            terminal_mod._claim_tool_progress("查一下天气", "好，我去搜一下，稍等。")
        )
        self.assertTrue(
            terminal_mod._claim_tool_ack("查一下天气", "查完了，结果如下。")
        )

    def test_tool_ack_does_not_block_progress(self):
        self.assertTrue(
            terminal_mod._claim_tool_ack("查一下天气", "查完了，结果如下。")
        )
        self.assertTrue(
            terminal_mod._claim_tool_progress("查一下天气", "好，我去搜一下，稍等。")
        )


if __name__ == "__main__":
    unittest.main()
