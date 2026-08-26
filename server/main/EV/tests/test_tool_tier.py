# -*- coding: utf-8 -*-
"""固定语音协议与兼容工具层单测。"""
import json
import unittest

import app
from devices.coding import surfaces as surface_skill
from devices.coding import led as led_skill
from devices.coding import claude_code as claude_code_skill
from tools import deep_search
from tools import device_control, object_control, task_control


def _size(tool):
    return len(json.dumps(tool, ensure_ascii=False))


class ToolTierTest(unittest.TestCase):
    def test_slim_definitions_are_smaller(self):
        for label, slim_fn, full_fn in (
            ("surface_expect_input",
             lambda: surface_skill.surface_expect_input_tool_definition(slim=True),
             surface_skill.surface_expect_input_tool_definition),
            ("led_control",
             lambda: led_skill.led_control_tool_definition(slim=True),
             led_skill.led_control_tool_definition),
            ("device_control",
             lambda: device_control.tool_definition(slim=True),
             device_control.tool_definition),
            ("coding_flow",
             lambda: app._coding_flow_tool_definition(slim=True),
             app._coding_flow_tool_definition),
            ("web_search",
             lambda: deep_search.tool_definition(slim=True),
             deep_search.tool_definition),
            ("web_extract",
             lambda: deep_search.extract_tool_definition(slim=True),
             deep_search.extract_tool_definition),
            ("claude_code_run",
             lambda: claude_code_skill.tool_definition(slim=True),
             claude_code_skill.tool_definition),
        ):
            slim = slim_fn()
            full = full_fn()
            self.assertEqual(slim["function"]["name"], full["function"]["name"])
            self.assertLess(
                _size(slim), _size(full),
                "%s slim 应比 full 小" % label,
            )

    def test_slim_description_is_one_line(self):
        # 低频工具 description 压成一行，无 \n 分段讲解。
        for name, slim_fn in (
            ("led_control", lambda: led_skill.led_control_tool_definition(slim=True)),
            ("device_control", lambda: device_control.tool_definition(slim=True)),
            ("surface_expect_input", lambda: surface_skill.surface_expect_input_tool_definition(slim=True)),
            ("web_search", lambda: deep_search.tool_definition(slim=True)),
            ("web_extract", lambda: deep_search.extract_tool_definition(slim=True)),
            ("coding_flow", lambda: app._coding_flow_tool_definition(slim=True)),
            ("claude_code_run", lambda: claude_code_skill.tool_definition(slim=True)),
        ):
            desc = slim_fn()["function"]["description"]
            self.assertNotIn("\n", desc, "%s slim description 应为一行" % name)

    def test_slim_keeps_enums_and_required(self):
        led = led_skill.led_control_tool_definition(slim=True)
        props = led["function"]["parameters"]["properties"]
        self.assertEqual(
            props["action"]["enum"],
            ["power", "color", "brightness", "effect", "set", "status"],
        )
        self.assertEqual(led["function"]["parameters"]["required"], ["action"])
        expect = surface_skill.surface_expect_input_tool_definition(slim=True)
        self.assertEqual(expect["function"]["parameters"]["required"], ["action", "surface_id"])

    def test_resident_tools_stay_full(self):
        # 高频窗口工具不被精简，完整规则保留。
        mgr = surface_skill.surface_manage_tool_definition()
        desc = mgr["function"]["description"]
        self.assertIn("窗口=代码", desc)
        self.assertIn("相对描述", desc)
        insp = surface_skill.surface_inspect_tool_definition()
        self.assertIn("只读查询", insp["function"]["description"])


class ToolsetAssemblyTest(unittest.TestCase):
    def test_voice_toolset_contains_expected_tools(self):
        tools = app._build_chat_tools("tier-test", voice_mode=True) or []
        names = [t["function"]["name"] for t in tools]
        self.assertEqual(
            names,
            ["conversation_reply", "task_control", "object_control"],
        )
        for low_level in (
            "surface_manage", "surface_inspect", "surface_expect_input", "led_control",
            "realtime_info", "coding_flow", "web_search", "web_extract",
            "surface_control", "device_control", "canvas_control",
        ):
            self.assertNotIn(low_level, names)

    def test_text_toolset_keeps_slim_claude(self):
        tools = app._build_chat_tools("tier-test", voice_mode=False) or []
        names = [t["function"]["name"] for t in tools]
        if "claude_code_run" in names:
            t = next(t for t in tools if t["function"]["name"] == "claude_code_run")
            self.assertNotIn("\n", t["function"]["description"])

    def test_voice_toolset_smaller_than_full(self):
        # 同一批工具：分级组装后字符总数必须小于全量版本。
        slim_tools = app._build_chat_tools("tier-test", voice_mode=True) or []
        low_level_tools = [
            app._conversation_reply_tool_definition(),
            surface_skill.surface_manage_tool_definition(),
            surface_skill.surface_inspect_tool_definition(),
            surface_skill.surface_expect_input_tool_definition(),
            app._realtime_info_tool_definition(),
            led_skill.led_control_tool_definition(),
            app._coding_flow_tool_definition(),
            deep_search.tool_definition(),
            deep_search.extract_tool_definition(),
        ]
        slim_total = sum(_size(t) for t in slim_tools)
        self.assertLess(slim_total, sum(_size(t) for t in low_level_tools))

    def test_voice_capabilities_keep_typed_enums(self):
        object_fn = object_control.tool_definition()["function"]
        # say 也必填：话和指令同一次生成，省掉「发指令→回执→再跑一趟组织语言」
        # 那个多余的来回（实测中位 1460ms）。
        self.assertEqual(
            object_fn["parameters"]["required"],
            ["op", "continue_after", "say"],
        )
        self.assertEqual(
            object_fn["parameters"]["properties"]["op"]["enum"],
            # adjust：相对调整（暗一点/大一点/往左挪挪）。算术在服务端，
            # 模型只声明方向和幅度——不是给灯加的特例，任何声明了量纲的对象都通用。
            ["inspect", "apply", "invoke", "adjust"],
        )
        self.assertNotIn("reply", object_fn["parameters"]["properties"])
        # 对象、设备、Skill 和能力名都不能长进常驻 schema。
        encoded = json.dumps(object_fn, ensure_ascii=False)
        self.assertNotIn("desk-light", encoded)
        self.assertNotIn("pcb", encoded.lower())
        task = task_control.tool_definition()["function"]
        self.assertIn("current_time", task["parameters"]["properties"]["kind"]["enum"])
        self.assertNotIn("time", task["parameters"]["properties"]["kind"]["enum"])
        self.assertIn("web_search", task["parameters"]["properties"]["kind"]["enum"])
        self.assertIn("coding_plan", task["parameters"]["properties"]["kind"]["enum"])
        self.assertEqual(
            task["parameters"]["required"],
            ["kind", "request", "continue_after"],
        )
        conversation = app._conversation_reply_tool_definition()["function"]
        self.assertEqual(
            conversation["parameters"]["properties"]["mode"]["enum"],
            ["answer", "clarify"],
        )
        self.assertEqual(
            conversation["parameters"]["required"],
            ["mode", "reply"],
        )
        self.assertIn(
            "reply 本身在向用户追问",
            conversation["parameters"]["properties"]["mode"]["description"],
        )

    def test_voice_toolset_is_below_previous_phase_budget(self):
        tools = app._build_chat_tools("tier-test", voice_mode=True) or []
        self.assertLess(len(json.dumps(tools, ensure_ascii=False)), 4000)


class RoutingCardTierTest(unittest.TestCase):
    def test_object_protocol_replaces_feature_rows(self):
        card = app._skill_routing_card()
        self.assertIn("inspect", card)
        self.assertIn("apply", card)
        self.assertIn("invoke", card)
        self.assertIn("新增 Skill 也是运行时对象", card)
        self.assertNotIn("surface_control", card)
        self.assertNotIn("device_control", card)
        self.assertNotIn("canvas_control", card)

    def test_public_rules_kept(self):
        card = app._skill_routing_card()
        self.assertIn("变更动作", card)
        self.assertIn("mode=clarify", card)

    def test_conversation_uses_no_pseudo_action(self):
        card = app._skill_routing_card()
        self.assertIn("conversation_reply", card)
        self.assertNotIn("respond", card)


class VoiceProtocolTest(unittest.TestCase):
    def test_not_acting_is_the_default_path_in_voice(self):
        """不动手是默认，不是需要主动选中的一个出口。

        以前语音每轮 required：用户只说「不错」，模型也必须产出一次工具调用，
        分类一抖就是误动作。防「光说不做」的担子已经转移到回执——播报锚在
        after 上，没调工具就没有 after 可复述。

        原先这条走 _tool_choice_for_action_round，那函数把两个入参 del 掉直接
        返回 "auto"，是个永远拨在同一档的空开关，已删。真正会临时改成
        required 的只有抓到无凭据声称之后那一轮，由调用方就地设置。
        """
        kwargs = app._tool_request_kwargs(
            [{"function": {"name": "conversation_reply"}}]
        )
        self.assertEqual(kwargs["tool_choice"], "auto")

    def test_no_tools_means_a_real_text_only_boundary(self):
        """收尾轮撤掉 tools，连 tool_choice 一起不发。"""
        self.assertEqual(app._tool_request_kwargs(None), {})


if __name__ == "__main__":
    unittest.main()
