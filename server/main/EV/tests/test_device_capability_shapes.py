# -*- coding: utf-8 -*-
"""设备能力的参数形状必须可见。

真实事故：语音下「把灯调成黄色」要三次工具调用才成——
set_color（不存在）→ color 传 {"color":"yellow"}（参数名错）→ 才对。
4.4 秒的轮次里 3.3 秒花在两次猜错上，而设备本身只要 25~37ms。
根因是描述符只写 "adapter-validated"：告诉模型有这个命令，却不告诉它传什么。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_registry_carries_command_argument_shapes():
    from devices.iot import iot_registry
    from tools import device_control

    device_control.ensure_builtin_devices()
    descriptor = iot_registry.descriptor("desk-light")
    shapes = descriptor.get("command_args") or {}
    assert set(shapes) >= {"power", "color", "brightness", "effect"}
    # 模型当初猜错的两处，现在都有明确形状
    assert "color_name" in shapes["color"] and "red" in shapes["color"]
    assert "on" in shapes["power"]


def test_inspect_no_longer_answers_adapter_validated():
    import json

    from tools import device_control, object_control

    device_control.ensure_builtin_devices()
    out = object_control.execute({"op": "inspect", "target": "iot.desk-light"})
    obj = json.loads(out[0] if isinstance(out, tuple) else out)["object"]
    assert "adapter-validated" not in json.dumps(obj, ensure_ascii=False)
    assert "color_name" in obj["properties"]["color"]
    assert obj["command_args"]["effect"]["effect"]


def test_prompt_hint_lists_signatures_so_no_inspect_round_trip_is_needed():
    from control_plane import world_snapshot
    from tools import device_control

    device_control.ensure_builtin_devices()
    hint = world_snapshot.capability_hint()
    # 不再是设备专用：内置应用同享一份投影。实测「计时30分钟」曾先猜
    # {"minutes":30}，报错才改对 duration_seconds。
    assert "app.timer" in hint and "duration_seconds" in hint
    assert "iot.desk-light" in hint
    # 命令签名与取值范围都在，模型第一次 invoke 就该写对
    assert "color_name" in hint and "0-255" in hint and "0-100" in hint
    assert "breathing" in hint and "wipe" in hint
    assert len(hint) <= 1300  # 提示词预算：设备签名不许无限膨胀


def test_hint_is_generated_from_the_registry_not_hardcoded():
    """新接进来的设备（含 MCP）只要注册时带上形状，就自动出现在提示里。"""
    from devices.iot import iot_registry
    from tools import device_control

    iot_registry.register(
        "test-fan",
        name="测试风扇",
        kind="fan",
        capabilities=("speed",),
        executor=lambda action, args: ("", {"ok": True}),
        command_args={"speed": {"level": "1-5 整数（必填）"}},
    )
    try:
        from control_plane import world_snapshot

        hint = world_snapshot.capability_hint()
        assert "iot.test-fan" in hint and "level" in hint
        assert "1-5 整数" in hint
    finally:
        iot_registry.unregister("test-fan")
