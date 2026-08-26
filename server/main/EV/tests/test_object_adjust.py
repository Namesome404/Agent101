# -*- coding: utf-8 -*-
"""相对调整由服务端算，模型只声明方向和幅度。

真实事故：灯在 40%，用户说「稍微暗一点」，模型给了 60（更亮）；再说「更暗
一点」，它给 40（回到原点）。当前值就写在提示里，但让快模型在 2 秒预算内做
算术不可靠。而且这不是灯的问题——窗口大小、面板高度、位置都是同一类指令。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_any_provider_that_declares_dimensions_gets_relative_adjustment():
    """通用性验证：一个只会 apply 的陌生 provider，声明量纲后即可被 adjust。

    这是这次改动的要点——不是给灯加特例，而是让「相对调整」成为契约能力。
    """
    from control_plane.object_registry import ObjectCapabilityRegistry

    knob = {"level": 50}
    calls = []

    def discover():
        return [{
            "target_id": "fake.knob",
            "name": "测试旋钮",
            "kind": "test",
            "owner": "system",
            "commands": ["set"],
            "state": {"level": knob["level"]},
            "adjustable": {
                "level": {
                    "label": "档位", "min": 0, "max": 100, "step": 10, "unit": "档",
                    "read": ["level"],
                    "via": {"op": "apply", "path": ["level"]},
                },
            },
        }]

    def execute(op, target, payload, ctx):
        calls.append((op, payload))
        if op == "apply":
            knob["level"] = int((payload.get("patch") or {}).get("level"))
            return {"ok": True, "state": dict(knob)}
        return {"ok": False}

    registry = ObjectCapabilityRegistry()
    registry.register_provider(
        "fake", discover=discover, execute=execute, target_prefixes=("fake.",),
    )
    result = registry.execute(
        "adjust", "fake.knob",
        {"property": "level", "direction": "up", "amount": "medium"}, {},
    )
    assert result["ok"] and result["before_value"] == 50 and result["after_value"] == 70
    # provider 完全没改：它收到的仍是一个普通 apply
    assert calls == [("apply", {"patch": {"level": 70}, "base_rev": None})]
    assert result["speech"] == "测试旋钮档位调到70档了"


def _fake_light(level=40):
    """一个假灯：形状与真灯一致，但不碰硬件。

    这两条测试原本直接 iot_registry.execute('desk-light', ...)，跑一次测试套件
    就把用户桌上的灯真的调了（实测被设成 100% 留在那）。测试不许碰硬件。
    """
    from devices.iot import iot_registry

    # 形状要和真设备一致：真灯的回执一定带 power，缺了它适配器渲染不出人话
    box = {"power": True, "brightness": level}

    def executor(action, arguments):
        if action == "brightness":
            box["brightness"] = int(arguments.get("brightness"))
        return "", {"ok": True, "action": action, "state": dict(box)}

    iot_registry.register(
        "test-lamp", name="测试灯", kind="light",
        capabilities=("brightness", "status"), executor=executor,
        adjustable={
            "brightness": {
                "label": "亮度", "min": 0, "max": 100, "step": 10, "unit": "%",
                "read": ["brightness"],
                "via": {"op": "invoke", "command": "brightness", "arg": "brightness"},
            },
        },
    )
    iot_registry.execute("test-lamp", "brightness", {"brightness": level})
    return box


def test_adjust_reads_current_value_and_does_the_arithmetic_server_side():
    from devices.iot import iot_registry
    from tools import object_control

    _fake_light(40)
    try:
        result = object_control.execute({
            "op": "adjust", "target": "iot.test-lamp",
            "property": "brightness", "direction": "down", "amount": "small",
        })[1]
        assert result["ok"] and result["before_value"] == 40 and result["after_value"] == 30
        # 播报说人话，前后值留在回执里给面板与后续核对
        assert result["speech"] == "测试灯亮度调到30%了"
    finally:
        iot_registry.unregister("test-lamp")


def test_amount_scales_the_step_and_limits_are_respected():
    from devices.iot import iot_registry
    from tools import object_control

    _fake_light(40)
    try:
        big = object_control.execute({
            "op": "adjust", "target": "iot.test-lamp",
            "property": "brightness", "direction": "up", "amount": "large",
        })[1]
        assert big["after_value"] == 80          # step 10 × large(4)
        iot_registry.execute("test-lamp", "brightness", {"brightness": 100})
        capped = object_control.execute({
            "op": "adjust", "target": "iot.test-lamp",
            "property": "brightness", "direction": "up", "amount": "small",
        })[1]
        assert capped["changed"] is False and "最大" in capped["speech"]
    finally:
        iot_registry.unregister("test-lamp")


def test_unadjustable_property_says_what_is_adjustable():
    from tools import device_control, object_control

    from devices.iot import iot_registry

    _fake_light(40)
    try:
        result = object_control.execute({
            "op": "adjust", "target": "iot.test-lamp",
            "property": "音量", "direction": "up",
        })[1]
    finally:
        iot_registry.unregister("test-lamp")
    assert result["ok"] is False
    assert result["reason"] == "property_not_adjustable"
    assert "brightness" in result["detail"]


def test_wiring_fields_are_not_exposed_to_the_model():
    """read/via 是服务端接线，描述符里不该漏给模型。"""
    import json

    from tools import device_control, object_control

    device_control.ensure_builtin_devices()
    obj = json.loads(object_control.execute({"op": "inspect", "target": "iot.desk-light"})[0])["object"]
    assert set(obj["adjustable"]["brightness"]) <= {"min", "max", "step", "unit", "label"}


def test_adjust_refreshes_state_when_the_cache_is_cold():
    """状态是进程内缓存，重启后为空——这时最常见的一句就是「暗一点」。

    真实事故：服务重启后第一次说「稍微亮一点」，adjust 报 current_value_unknown，
    模型转去 inspect（同样是空的），最后蹦出一句兜底话术。
    """
    from control_plane.object_registry import ObjectCapabilityRegistry

    box = {"level": None, "real": 60}
    calls = []

    def discover():
        return [{
            "target_id": "cold.dial", "name": "冷启动旋钮", "kind": "test", "owner": "system",
            "commands": ["set", "status"],
            "state": ({"level": box["level"]} if box["level"] is not None else {}),
            "adjustable": {
                "level": {"label": "档位", "min": 0, "max": 100, "step": 10,
                          "read": ["level"], "via": {"op": "apply", "path": ["level"]}},
            },
        }]

    def execute(op, target, payload, ctx):
        calls.append((op, str(payload.get("command") or "")))
        if op == "invoke" and payload.get("command") == "status":
            box["level"] = box["real"]          # 读一次真值，填进缓存
            return {"ok": True, "state": {"level": box["level"]}}
        if op == "apply":
            box["level"] = int((payload.get("patch") or {}).get("level"))
            return {"ok": True, "state": {"level": box["level"]}}
        return {"ok": False}

    registry = ObjectCapabilityRegistry()
    registry.register_provider(
        "cold", discover=discover, execute=execute, target_prefixes=("cold.",),
    )
    result = registry.execute(
        "adjust", "cold.dial", {"property": "level", "direction": "down", "amount": "small"}, {},
    )
    assert result["ok"] and result["before_value"] == 60 and result["after_value"] == 50
    assert ("invoke", "status") in calls        # 确实先去读了真值


def test_every_mutation_receipt_carries_before_and_after_in_one_vocabulary():
    """调用前读的、回执里拿到的、播报出去的，必须是同一种说法。

    以前调用前读的是「窗口记忆」那套文案，回执只有 ok:true，播报靠模型自由
    发挥——三者对不上，也就无从核对，只能靠一堆「禁止声称已完成」去防。
    """
    from devices.iot import iot_registry
    from tools import object_control

    _fake_light(40)
    try:
        receipt = object_control.execute({
            "op": "invoke", "target": "iot.test-lamp",
            "command": "brightness", "args": {"brightness": 70},
        })[1]
        assert receipt["ok"]
        assert "亮度40%" in receipt["before"]
        assert "亮度70%" in receipt["after"]
        # after 用的是【世界现状】同一套渲染
        from control_plane import world_snapshot

        assert receipt["after"] in world_snapshot.render()
    finally:
        iot_registry.unregister("test-lamp")


def test_truth_rule_points_at_the_receipt_instead_of_listing_prohibitions():
    from devices.coding import surface_hints

    rule = surface_hints.truth_system()
    assert "after" in rule and "回执是唯一真相" in rule
    assert len(rule) < 240          # 禁令收敛后不该再膨胀回去


def test_named_colors_round_trip_through_the_state_description():
    """用户说「调成暖白」，回执里的现状就该是暖白色。

    真实事故：warm_white 的定义值 (255,180,96) 被色相近似判成「黄色」，
    于是模型播报「已调成暖白色」而回执写着「黄色」——刚下的指令和刚回的
    现状对不上，前后对照就成了误报源。
    """
    from devices.coding import led

    for _name, (rgb, label) in led.NAMED_COLORS.items():
        assert led._color_name(*rgb) == label
    # 表外的颜色仍按色相近似给个说法，不至于变成「彩色」
    assert led._color_name(200, 60, 60) == "红色"


def test_query_receipt_does_not_claim_an_update():
    """查状态却播报「已更新」是睁眼说瞎话。"""
    from devices.iot import iot_registry
    from tools import object_control

    _fake_light(40)
    try:
        text, receipt = object_control.execute({
            "op": "invoke", "target": "iot.test-lamp", "command": "status",
        })
        assert receipt["ok"] and not receipt.get("changed")
        # 服务端不再替模型说话：没写 say 就没有 direct_reply，
        # 回执回灌给模型，由它看着真实状态自己组织语言。
        assert not receipt.get("direct_reply")
        assert "亮度40%" in receipt["after"]
    finally:
        iot_registry.unregister("test-lamp")


def test_successful_mutation_is_recognised_even_with_a_cold_cache():
    """重启后 before 无从得知，不能因为「比不出差别」就判成没变。"""
    from devices.iot import iot_registry
    from tools import object_control
    import app

    box = _fake_light(40)
    try:
        receipt = object_control.execute({
            "op": "invoke", "target": "iot.test-lamp",
            "command": "brightness", "args": {"brightness": 90},
        })[1]
        assert receipt["changed"] is True
        assert app._receipt_is_mutation("object_control", receipt) is True
        # 只读命令不因此被算成动作
        query = object_control.execute({
            "op": "invoke", "target": "iot.test-lamp", "command": "status",
        })[1]
        assert app._receipt_is_mutation("object_control", query) is False
    finally:
        iot_registry.unregister("test-lamp")


def test_builtin_apps_can_be_closed_and_declare_their_arguments():
    """开得出来就该关得掉，参数形状也得写清楚。

    真实事故：用户「把记事本关上」，模型只能回「记事本这个应用没有关闭命令，
    我这边关不掉它，你直接点窗口右上角的叉」——app.notes 的命令表里确实没有
    close。同一段对话里「计时30分钟」也先猜了 {"minutes":30}，报错才改对
    duration_seconds。
    """
    import json

    from tools import object_control

    for target in ("app.timer", "app.notes"):
        obj = json.loads(object_control.execute({"op": "inspect", "target": target})[0])["object"]
        assert "close" in obj["commands"], target
    timer = json.loads(object_control.execute({"op": "inspect", "target": "app.timer"})[0])["object"]
    assert "duration_seconds" in timer["command_args"]["start"]
    assert "1800" in timer["command_args"]["start"]["duration_seconds"]
    notes = json.loads(object_control.execute({"op": "inspect", "target": "app.notes"})[0])["object"]
    assert "text" in notes["command_args"]["append"]


def test_world_snapshot_says_a_recent_action_does_not_excuse_this_one():
    """「刚做过」不等于「这次不用做」。

    这句原本长在 2888 字符的「窗口记忆」结尾，被压缩成世界现状投影时一并
    删掉了。后果用隔离实验测得很干净：历史里只要有一条「助手刚报告做过某事」，
    「把 YouTube 关上」就 3/3 零调用——模型判断「这事已经处理过了」；
    空历史时同一句 3/3 正常。放回去之后带历史组 4/4、历史累积组 6/6 全部真调。

    truth_system 里那句更泛的「每次要求都是独立动作」压不住：
    上下文里一条「刚做过」的示范，比泛泛的规则有力得多。
    """
    from control_plane import world_snapshot
    from tools import device_control

    device_control.ensure_builtin_devices()
    text = world_snapshot.render()
    assert "刚被动过" in text and "新动作" in text
    assert "历史里说过做过不算" in text
