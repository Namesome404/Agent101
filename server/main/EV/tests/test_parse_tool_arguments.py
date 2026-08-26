# -*- coding: utf-8 -*-
"""工具参数 JSON 容错解析单测。

模型偶发输出损坏 JSON（长 HTML/内容里换行或引号没转义、多个 JSON 粘连、
尾部残留文本）。_parse_tool_arguments 应尽量降级解析出 dict，
而不是一失败就报错。写内容/写 HTML 窗口场景尤其重要。
"""
import unittest
from types import SimpleNamespace
from unittest import mock

import app as app_module


class SlowToolStarterTest(unittest.TestCase):
    def test_short_search_has_no_progress_preface(self):
        starter = app_module._action_progress_starter([{
            "action": "task_control",
            "args": {"kind": "web_search", "request": "任意问题"},
        }])
        self.assertEqual(starter, "")

    def test_long_search_can_say_it_will_search_carefully(self):
        natural = "阵列波导公开报价少，我多核对几家。"
        starter = app_module._action_progress_starter([{
            "action": "task_control",
            "args": {
                "kind": "web_search",
                "request": "对照多处资料认真核实",
                "speak_while": True,
                "progress_reply": natural,
            },
        }])
        self.assertEqual(starter, natural)

    def test_fast_action_progress_requires_explicit_flag(self):
        self.assertEqual(app_module._action_progress_starter([{
            "action": "device_control",
            "args": {"action": "color"},
        }]), "")
        self.assertTrue(app_module._action_progress_starter([{
            "action": "device_control",
            "args": {
                "action": "color",
                "speak_while": True,
                "progress_reply": "这组灯效参数多，我慢慢调准。",
            },
        }]))

    def test_progress_needs_model_wording_and_cannot_claim_completion(self):
        self.assertEqual(app_module._action_progress_starter([{
            "action": "task_control",
            "args": {"kind": "web_search", "speak_while": True},
        }]), "")
        self.assertEqual(app_module._action_progress_starter([{
            "action": "task_control",
            "args": {
                "kind": "web_search",
                "speak_while": True,
                "progress_reply": "已经查到了，我再整理。",
            },
        }]), "")

    def test_followup_prevents_single_tool_fast_finish(self):
        self.assertFalse(app_module._batch_requests_continuation([{
            "action": "device_control",
            "args": {"action": "color"},
        }]))
        self.assertTrue(app_module._batch_requests_continuation([{
            "action": "device_control",
            "args": {"action": "color", "continue_after": True},
        }]))

    def test_multi_action_batch_only_continues_when_declared(self):
        self.assertFalse(app_module._batch_requests_continuation([
            {"action": "device_control", "args": {"action": "color"}},
            {"action": "surface_control", "args": {"action": "show"}},
        ]))

    def test_multi_action_receipts_are_aggregated(self):
        reply = app_module._batch_direct_reply([
            {"ok": True, "meta": {"speech": "灯已调蓝"}},
            {"ok": True, "meta": {"speech": "页面已打开"}},
        ])
        self.assertEqual(reply, "灯已调蓝，页面已打开")

    def test_transaction_key_ignores_speech_controls_but_keeps_semantics(self):
        first = app_module._transaction_action_key({
            "action": "surface_control",
            "args": {
                "action": "create",
                "title": "记事本",
                "continue_after": True,
                "reply": "开始建",
            },
        })
        same_transaction = app_module._transaction_action_key({
            "action": "surface_control",
            "args": {
                "action": "create",
                "title": "记事本",
                "continue_after": False,
                "reply": "建好了",
            },
        })
        different_transaction = app_module._transaction_action_key({
            "action": "surface_control",
            "args": {
                "action": "update",
                "title": "记事本",
                "continue_after": False,
            },
        })
        self.assertEqual(first, same_transaction)
        self.assertNotEqual(first, different_transaction)

    def test_contextual_action_never_fast_finishes_with_fixed_template(self):
        self.assertEqual(app_module._receipt_direct_reply(
            "device_control",
            {"speech": "灯已打开"},
        ), "")
        self.assertEqual(app_module._receipt_direct_reply(
            "surface_control",
            {"speech": "页面已打开"},
        ), "")

    def test_contextual_action_uses_first_round_natural_reply_without_second_llm(self):
        natural = "我把页面的层级重新理顺了，你看看现在是否更清楚。"
        self.assertEqual(app_module._receipt_direct_reply(
            "surface_control",
            {"speech": "页面已打开", "direct_reply": natural},
        ), natural)

    def test_committed_contextual_batch_uses_verified_receipt_and_stops(self):
        self.assertEqual(app_module._batch_direct_reply([
            {"ok": True, "meta": {
                "name": "device_control",
                "speech": "灯已打开",
            }},
        ]), "灯已打开")

    def test_missing_deterministic_receipt_falls_back_to_model(self):
        self.assertEqual(app_module._batch_direct_reply([
            {"ok": True, "meta": {"speech": "灯已调蓝"}},
            {"ok": True, "meta": {"context": "搜索结果"}},
        ]), "")

    def test_empty_no_starter(self):
        self.assertEqual(app_module._action_progress_starter([]), "")

    def test_long_search_uses_model_sentence_instead_of_a_phrase_bank(self):
        natural = "几家厂商口径不一，我把报价逐个对齐。"
        self.assertEqual(app_module._action_progress_starter([{
            "action": "task_control",
            "args": {
                "kind": "web_search",
                "speak_while": True,
                "progress_reply": natural,
            },
        }]), natural)


class ConversationResponseContractTest(unittest.TestCase):
    def test_answer_and_clarify_are_typed_readonly_receipts(self):
        for mode in ("answer", "clarify"):
            text, meta = app_module._execute_chat_tool(
                "conversation_reply",
                {"mode": mode, "reply": "一句话"},
                aid=1,
                voice_mode=True,
            )
            self.assertEqual(text, "一句话")
            self.assertTrue(meta["ok"])
            self.assertEqual(meta["response_mode"], mode)
            self.assertFalse(
                app_module._receipt_is_mutation("conversation_reply", meta)
            )

    def test_missing_mode_is_rejected_instead_of_guessing(self):
        _text, meta = app_module._execute_chat_tool(
            "conversation_reply",
            {"reply": "我猜一下"},
            aid=1,
            voice_mode=True,
        )
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["response_mode"], "")

    def test_no_tool_call_uses_model_own_text(self):
        text, mode = app_module._voice_no_tool_response([
            "现在几点了？",
        ])
        self.assertEqual(mode, "answer")
        self.assertEqual(text, "现在几点了？")

    def test_no_tool_call_blocks_leaked_tool_protocol(self):
        text, mode = app_module._voice_no_tool_response([
            '<tool_calls><invoke name="canvas_control.apply">{}</invoke></tool_calls>',
        ])
        self.assertEqual(mode, "answer")
        self.assertEqual(text, "")

    def test_no_tool_call_recovers_reply_from_deepseek_dsml(self):
        text, mode = app_module._voice_no_tool_response([
            '<｜DSML｜tool_calls><｜DSML｜invoke name="conversation_reply">'
            '<｜DSML｜parameter name="mode" string="true">answer</｜DSML｜parameter>'
            '<｜DSML｜parameter name="reply" string="true">公开资料没写这个功能。</｜DSML｜parameter>'
            '</｜DSML｜invoke></｜DSML｜tool_calls>',
        ])
        self.assertEqual(mode, "answer")
        self.assertEqual(text, "公开资料没写这个功能。")

    def test_forced_answer_instruction_does_not_request_a_removed_tool(self):
        instruction = app_module._forced_text_answer_instruction()
        self.assertIn("不会提供任何工具", instruction)
        self.assertIn("直接输出", instruction)
        self.assertIn("不要调用 conversation_reply", instruction)
        self.assertNotIn("只能调用 conversation_reply", instruction)

    def test_world_snapshot_describes_state_not_tool_names(self):
        # 状态注入的是世界现状，不是「最近调过哪些工具」的日志——工具名会
        # 对快模型形成示范效应（满屏 status 回执会诱导它跟调 status）。
        # 现状此前由 device_control.known_state_hint 单独注入一套文案，
        # 现已并入【世界现状】：同一份契约投影，窗口/设备/计时器共用一套词汇。
        from control_plane import world_snapshot
        from devices.iot import iot_registry

        def executor(action, arguments):
            return "", {"ok": True, "action": action,
                        "state": {"power": True, "red": 255, "green": 0,
                                  "blue": 0, "brightness": 30}}

        iot_registry.register(
            "snapshot-lamp", name="快照测试灯", kind="light",
            capabilities=("brightness", "status"), executor=executor,
        )
        try:
            iot_registry.execute("snapshot-lamp", "status", {})
            text = world_snapshot.render()
        finally:
            iot_registry.unregister("snapshot-lamp")
        self.assertIn("快照测试灯", text)
        self.assertIn("亮度30%", text)
        self.assertNotIn("device_control", text)
        self.assertNotIn("known_state_hint", text)

    def test_failed_receipt_cannot_authorize_model_supplied_completion(self):
        result = {
            "ok": False,
            "meta": {
                "name": "device_control",
                "direct_reply": "灯已经打开了。",
            },
        }
        self.assertEqual(
            app_module._verified_receipt_direct_reply(
                "device_control", result
            ),
            "",
        )

    def test_successful_mutation_receipt_can_authorize_natural_reply(self):
        result = {
            "ok": True,
            "meta": {
                "name": "device_control",
                "action": "power",
                "direct_reply": "灯打开了。",
            },
        }
        self.assertEqual(
            app_module._verified_receipt_direct_reply(
                "device_control", result
            ),
            "灯打开了。",
        )


class DesktopShellLifecycleTest(unittest.TestCase):
    def test_alive_check_uses_exact_process_prefix_without_pgrep(self):
        target = "/tmp/ev-tauri-shell"
        with mock.patch.object(
            app_module, "_desktop_shell_process_prefix", return_value=target,
        ), mock.patch.object(
            app_module.subprocess,
            "run",
            return_value=SimpleNamespace(stdout=target + "\n"),
        ) as run:
            self.assertTrue(app_module._desktop_shell_alive())
        self.assertEqual(run.call_args.args[0], ["ps", "-axo", "command="])

    def test_alive_check_does_not_match_shell_command_containing_target(self):
        target = "/tmp/ev-tauri-shell"
        shell_line = "/bin/zsh -lc inspect " + target
        with mock.patch.object(
            app_module, "_desktop_shell_process_prefix", return_value=target,
        ), mock.patch.object(
            app_module.subprocess,
            "run",
            return_value=SimpleNamespace(stdout=shell_line + "\n"),
        ):
            self.assertFalse(app_module._desktop_shell_alive())


class ParseToolArgumentsTest(unittest.TestCase):
    def test_standard_json(self):
        self.assertEqual(
            app_module._parse_tool_arguments('{"action": "open", "surface_id": "notes"}'),
            {"action": "open", "surface_id": "notes"},
        )

    def test_empty_returns_empty_dict(self):
        self.assertEqual(app_module._parse_tool_arguments(""), {})
        self.assertEqual(app_module._parse_tool_arguments(None), {})

    def test_strict_false_recovers_bare_newline(self):
        # HTML 内容里的裸换行：严格 JSON 拒绝，strict=False 可解析
        raw = '{"action": "append", "text": "line1\nline2"}'
        out = app_module._parse_tool_arguments(raw)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("action"), "append")
        self.assertEqual(out.get("text"), "line1\nline2")

    def test_raw_decode_recovers_trailing_garbage(self):
        # 尾部残留/粘连：取第一个完整 JSON 对象
        raw = '{"action": "set", "surface_id": "notes"} trailing <garbage>'
        out = app_module._parse_tool_arguments(raw)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("action"), "set")

    def test_truncated_at_closing_brace(self):
        # 尾部多了一个未闭合片段：截到配平的 } 再解析
        raw = '{"action": "open", "definition": {"title": "t"}} extra {bad'
        out = app_module._parse_tool_arguments(raw)
        self.assertIsInstance(out, dict)
        self.assertEqual(out.get("action"), "open")
        self.assertEqual((out.get("definition") or {}).get("title"), "t")

    def test_completely_invalid_returns_none(self):
        self.assertIsNone(app_module._parse_tool_arguments("{not json at all"))
        self.assertIsNone(app_module._parse_tool_arguments("hello"))

    def test_missing_surrounding_braces_recovered(self):
        # 模型偶发输出参数但不带外层大括号的变体，也应尽量恢复
        out = app_module._parse_tool_arguments('"action": "close", "surface_id": "notes"')
        self.assertIsNone(out)  # 不带外层对象视为无法安全恢复，宁可不执行


if __name__ == "__main__":
    unittest.main()
