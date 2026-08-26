# -*- coding: utf-8 -*-
"""Constant-schema object protocol and verified-target receipts."""
import json
from unittest.mock import patch

import app
from control_plane.object_registry import ObjectCapabilityRegistry, object_registry
from tools import object_control


def _register_fake_pcb_provider(name="test.pcb"):
    object_registry.register_provider(
        name,
        discover=lambda: [{
            "target_id": "skill.pcb",
            "name": "PCB 设计技能",
            "kind": "skill",
            "owner": "assistant",
            "description": "创建和检查 PCB 工程",
            "aliases": ["画PCB", "pcb"],
            "properties": {"board": "object"},
            "commands": ["create_board", "route", "export_gerber"],
        }],
        execute=lambda op, target, payload, ctx: {
            "ok": True,
            "changed": True,
            "speech": "PCB 设计技能已执行",
            "echo": {"op": op, "target": target, "payload": payload},
        },
        target_prefixes=("skill.pcb",),
    )


def test_runtime_skill_registration_does_not_change_model_schema():
    before_tool = json.dumps(object_control.tool_definition(), ensure_ascii=False, sort_keys=True)
    before_voice = json.dumps(
        app._build_chat_tools("object-schema-before", voice_mode=True),
        ensure_ascii=False,
        sort_keys=True,
    )
    _register_fake_pcb_provider()
    try:
        after_tool = json.dumps(object_control.tool_definition(), ensure_ascii=False, sort_keys=True)
        after_voice = json.dumps(
            app._build_chat_tools("object-schema-after", voice_mode=True),
            ensure_ascii=False,
            sort_keys=True,
        )
        assert after_tool == before_tool
        assert after_voice == before_voice
        text, receipt = object_control.execute({
            "op": "inspect",
            "selector": {"kind": "skill", "query": "画 PCB"},
            "continue_after": True,
        })
        assert receipt["ok"] is True
        assert receipt["objects"][0]["target_id"] == "skill.pcb"
        assert "create_board" in text
    finally:
        object_registry.unregister_provider("test.pcb")


def test_known_target_routes_without_discovering_unrelated_skills():
    registry = ObjectCapabilityRegistry()
    calls = {"pcb": 0, "unrelated": 0}

    def pcb_discover():
        calls["pcb"] += 1
        return [{"target_id": "skill.pcb", "name": "PCB", "kind": "skill"}]

    def unrelated_discover():
        calls["unrelated"] += 1
        return [{"target_id": "skill.music", "name": "Music", "kind": "skill"}]

    registry.register_provider(
        "pcb", discover=pcb_discover,
        execute=lambda op, target, payload, ctx: {"ok": True, "changed": True},
        target_prefixes=("skill.pcb",),
    )
    registry.register_provider(
        "music", discover=unrelated_discover,
        execute=lambda op, target, payload, ctx: {"ok": True, "changed": True},
        target_prefixes=("skill.music",),
    )
    receipt = registry.execute("invoke", "skill.pcb", {"command": "create"})
    assert receipt["ok"] is True
    # 目标明确时不去发现无关 provider——这是本条的要点。
    # 目标自己的 provider 允许被查第二次：变更回执要带上「改完之后的现状」，
    # provider 没有自报 display 时只能回头重查一次（自报了就免掉，见设备路径）。
    assert calls["unrelated"] == 0
    assert calls["pcb"] <= 2


def test_skill_invocation_receipt_is_bound_to_verified_target():
    _register_fake_pcb_provider("test.pcb.receipt")
    try:
        _text, receipt = object_control.execute({
            "op": "invoke",
            "target": "skill.pcb",
            "command": "create_board",
            "args": {"name": "controller"},
            "continue_after": False,
        })
        assert receipt["ok"] is True
        assert receipt["target_id"] == "skill.pcb"
        assert receipt["target_name"] == "PCB 设计技能"
        assert receipt["target_kind"] == "skill"
        assert receipt["target_owner"] == "assistant"
        assert receipt["verified_target"] is True
    finally:
        object_registry.unregister_provider("test.pcb.receipt")


def test_assistant_self_has_a_stable_non_device_identity():
    _text, receipt = object_control.execute({
        "op": "inspect",
        "target": "agent.ui.status",
        "continue_after": False,
    })
    descriptor = receipt["object"]
    assert descriptor["target_id"] == "agent.ui.status"
    assert descriptor["owner"] == "assistant"
    assert descriptor["kind"] == "ui"


def test_status_theme_apply_cannot_report_a_different_target():
    with patch(
        "tools.object_control.surface_tools.set_status_timeline_theme",
        return_value={"ok": True, "changed": True},
    ), patch(
        "tools.object_control._status_state",
        return_value={"theme": {"accent": "#55aaff"}},
    ):
        _text, receipt = object_control.execute({
            "op": "apply",
            "target": "agent.ui.status",
            "patch": {"theme": {"accent": "#55aaff"}},
            "continue_after": False,
        })
    assert receipt["ok"] is True
    assert receipt["target_id"] == "agent.ui.status"
    assert receipt["target_name"] == "助手状态栏"
    assert receipt["target_owner"] == "assistant"
    assert receipt["verified_target"] is True
    # 播报现在由模型自己写（say），服务端不再替它组织语言。保证换了形式：
    # 不要求它念出目标全名（人说话本来就省略），但不许把动作说成别的对象。
    assert not receipt.get("direct_reply")      # 没写 say 就不替它说


def test_device_adapter_keeps_physical_target_in_receipt():
    with patch(
        "tools.object_control.device_control.execute",
        return_value=("ok", {
            "ok": True,
            "changed": True,
            "verified": True,
            "state": {"color": "cyan"},
        }),
    ) as execute_device:
        _text, receipt = object_control.execute({
            "op": "apply",
            "target": "iot.desk-light",
            "patch": {"color": "cyan"},
            "continue_after": False,
        })
    execute_device.assert_called_once()
    assert execute_device.call_args.args[0]["device_id"] == "desk-light"
    assert receipt["target_id"] == "iot.desk-light"
    assert receipt["target_owner"] == "physical"
    assert receipt["verified_target"] is True


def test_say_that_names_another_object_is_not_spoken():
    """模型自己写播报之后，不能用别的对象的名字冒充实际目标。"""
    from tools import device_control

    device_control.ensure_builtin_devices()
    with patch(
        "tools.object_control.surface_tools.set_status_timeline_theme",
        return_value={"ok": True, "changed": True},
    ), patch(
        "tools.object_control._status_state",
        return_value={"theme": {"accent": "#55aaff"}},
    ):
        _t, honest = object_control.execute({
            "op": "apply", "target": "agent.ui.status",
            "patch": {"theme": {"accent": "#55aaff"}},
            "say": "好了，换成蓝色了。",
        })
        _t, lying = object_control.execute({
            "op": "apply", "target": "agent.ui.status",
            "patch": {"theme": {"accent": "#55aaff"}},
            "say": "桌面灯带已经换成蓝色了。",
        })
    # 中性的说法照播；说成另一个对象的就不播，让模型看着回执重说
    assert honest["direct_reply"] == "好了，换成蓝色了。"
    assert not lying.get("direct_reply")
