# -*- coding: utf-8 -*-
"""固定协议路由卡单测。"""
import unittest

import app
from devices.coding import surfaces as surface_skill


class SkillRoutingCardTest(unittest.TestCase):
    def test_fixed_protocol_header(self):
        card = app._skill_routing_card()
        self.assertIn("【固定动作协议】", card)

    def test_all_voice_tools_covered(self):
        card = app._skill_routing_card()
        for tool in ("conversation_reply", "task_control", "object_control"):
            self.assertIn(tool, card, "路由卡漏掉工具 %s" % tool)
        for legacy in ("surface_control", "device_control", "canvas_control"):
            self.assertNotIn(legacy, card)

    def test_conversation_reply_is_the_structured_non_action_branch(self):
        card = app._skill_routing_card()
        self.assertNotIn("respond", card)
        self.assertIn("conversation_reply", card)
        self.assertIn("mode=clarify", card)

    def test_priority_and_fallback_in_header(self):
        card = app._skill_routing_card()
        self.assertIn("变更动作", card)
        self.assertIn("优先于查询", card)
        self.assertIn("不得靠猜测调用 inspect 之外的动作", card)

    def test_pure_info_suppression_covers_web_search(self):
        # 纯信息判断已交给模型自己（is_pure_info 停用恒 False），
        # 路由卡仍需覆盖"只报名称不搜"的决策，防止模型因一个名词就触发搜索。
        self.assertFalse(surface_skill.is_pure_info("Deepseek Corporation."))
        card = app._skill_routing_card()
        self.assertIn("只报一个名称/名词(纯陈述)不搜", card)

    def test_short_workflow_protocol_is_explicit(self):
        card = app._skill_routing_card()
        self.assertIn("continue_after=true", card)
        self.assertIn("speak_while=true", card)
        self.assertIn("progress_reply", card)
        self.assertIn("不要套用", card)
        self.assertIn("同轮并行", card)

    def test_opinion_and_evaluation_are_not_device_or_surface_actions(self):
        card = app._skill_routing_card()
        self.assertIn("页面、灯、技能只是对象，不等于动作", card)
        self.assertIn("问看法、原因、建议时不得改变任何对象", card)
        self.assertIn("最终话术必须使用回执中的 target_name", card)

    def test_time_route_requires_current_time_intent_not_a_single_word(self):
        card = app._skill_routing_card()
        self.assertIn("现在几点用 current_time", card)
        self.assertIn("计时/倒计时绝不能用 current_time", card)
        self.assertIn("内置应用", card)
        self.assertIn("几点吃饭/几点开始/记录几点钟", card)
        self.assertIn("不是问当前时间", card)

    def test_uncertain_intent_clarifies_instead_of_querying_status(self):
        card = app._skill_routing_card()
        self.assertIn("conversation_reply(mode=clarify)", card)
        self.assertIn("只问一个关键问题", card)
        self.assertIn("只要 reply 是追问信息", card)
        self.assertIn("不得靠猜测调用 inspect 之外的动作", card)

    def test_self_and_physical_device_have_distinct_identity(self):
        card = app._skill_routing_card()
        self.assertIn("agent.ui.status", card)
        self.assertIn("owner=assistant", card)
        self.assertIn("iot.desk-light", card)
        self.assertIn("verified_target=true", card)

    def test_new_skill_does_not_require_a_new_tool_or_route_row(self):
        card = app._skill_routing_card()
        self.assertIn("新增 Skill 也是运行时对象", card)
        self.assertIn("不得要求新增常驻工具或在这里增加路由行", card)


if __name__ == "__main__":
    unittest.main()
