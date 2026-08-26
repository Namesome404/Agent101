import threading
import time
from unittest import mock

from devices.iot.registry import DeviceCapabilityRegistry
from tools import device_control


def test_registry_requires_explicit_receipt():
    registry = DeviceCapabilityRegistry()
    registry.register(
        "lamp-1",
        name="Lamp",
        kind="light",
        capabilities=["power"],
        executor=lambda action, args: ("done", {"action": action}),
    )
    result = registry.execute("lamp-1", "power", {"on": True})
    assert result["ok"] is False
    assert "meta.ok" in result["meta"]["error"]


def test_registry_rejects_unknown_capability_without_calling_adapter():
    called = []
    registry = DeviceCapabilityRegistry()
    registry.register(
        "lamp-1",
        name="Lamp",
        kind="light",
        capabilities=["power"],
        executor=lambda action, args: called.append((action, args)),
    )
    result = registry.execute("lamp-1", "unlock", {})
    assert result["ok"] is False
    assert called == []


def test_same_device_commands_are_serialized():
    registry = DeviceCapabilityRegistry()
    active = 0
    peak = 0
    state_lock = threading.Lock()

    def run(action, args):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.03)
        with state_lock:
            active -= 1
        return "ok", {"ok": True}

    registry.register(
        "lamp-1", name="Lamp", kind="light",
        capabilities=["power"], executor=run,
    )
    threads = [
        threading.Thread(target=lambda: registry.execute("lamp-1", "power", {}))
        for _ in range(3)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1


def test_device_control_preserves_led_verified_receipt():
    device_control.iot_registry.unregister("desk-light")
    try:
        with mock.patch.object(
            device_control.led,
            "execute",
            return_value=("lamp changed", {"ok": True, "verified": True, "speech": "灯已打开"}),
        ):
            text, meta = device_control.execute({
                "device_id": "desk-light",
                "action": "power",
                "on": True,
                "reply": "这盏灯已经亮起来了。",
            }, request_id="turn-1")
    finally:
        device_control.iot_registry.unregister("desk-light")
    assert text == "lamp changed"
    assert meta["ok"] is True
    assert meta["verified"] is True
    assert meta["device_id"] == "desk-light"
    assert meta["capability"] == "power"
    assert meta["correlation_id"] == "turn-1"
    assert meta["direct_reply"] == "这盏灯已经亮起来了。"


def test_world_state_remembers_last_receipt_state_without_polling():
    registry = DeviceCapabilityRegistry()
    registry.register(
        "lamp-1",
        name="桌面灯带",
        kind="light",
        capabilities=["color"],
        executor=lambda action, args: (
            "ok",
            {"ok": True, "action": action, "state": {
                "power": True, "red": 128, "green": 0, "blue": 255,
                "brightness": 30, "effect": "solid",
            }},
        ),
    )
    assert registry.world_state() == []
    registry.execute("lamp-1", "color", {"color_name": "purple"})
    snapshot = registry.world_state()
    assert len(snapshot) == 1
    assert snapshot[0]["device_id"] == "lamp-1"
    assert snapshot[0]["name"] == "桌面灯带"
    assert snapshot[0]["state"]["blue"] == 255


def test_known_state_hint_describes_world_without_tool_names():
    registry = device_control.iot_registry
    registry.unregister("desk-light")
    try:
        registry.register(
            "desk-light",
            name="桌面灯带",
            kind="light",
            capabilities=["color"],
            executor=lambda action, args: (
                "ok",
                {"ok": True, "action": action, "state": {
                    "power": True, "red": 255, "green": 0, "blue": 0,
                    "brightness": 30, "effect": "solid",
                }},
            ),
        )
        registry.execute("desk-light", "color", {})
        hint = device_control.known_state_hint()
    finally:
        registry.unregister("desk-light")
    assert "桌面灯带" in hint
    assert "红色" in hint
    assert "亮度30%" in hint
    assert "device_control" not in hint
    assert "ok:true" not in hint
