# -*- coding: utf-8 -*-
"""声音通道是一个可被 agent 操作的对象。

以前「从哪儿出声、用哪个麦」只能在设备页上点，或者去 macOS 的声音设置里切；
用户说「把声音切到耳机」时，agent 手里没有任何能干这件事的能力。
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def test_spoken_names_match_real_devices():
    """用户说「耳机」「扬声器」，设备名却是 AirPods Pro / MacBook Air Speakers。"""
    from control_plane import audio_route

    outs = ["MacBook Air Speakers", "AirPods Pro"]
    assert audio_route._match("扬声器", outs) == "MacBook Air Speakers"
    assert audio_route._match("耳机", outs) == "AirPods Pro"
    assert audio_route._match("airpods", outs) == "AirPods Pro"
    assert audio_route._match("AirPods Pro", outs) == "AirPods Pro"
    assert audio_route._match("不存在的设备", outs) == ""


def test_selecting_an_absent_device_says_what_is_available(monkeypatch):
    """目标设备不在本机时如实报告，不假装切成功。"""
    from control_plane import audio_route

    monkeypatch.setattr(audio_route, "_known_devices",
                        lambda **_: (["MacBook Air Microphone"], ["MacBook Air Speakers"]))
    result = audio_route.execute("use_output", {"device": "外星音箱"})
    assert result["ok"] is False and result["reason"] == "device_not_found"
    assert "MacBook Air Speakers" in result["detail"]


def test_selecting_writes_the_preference(monkeypatch):
    from control_plane import audio_route

    store = {}

    class _DB:
        @staticmethod
        def get_setting(key, default=""):
            return store.get(key, default)

        @staticmethod
        def set_setting(key, value):
            store[key] = value

    monkeypatch.setattr(audio_route, "_db", lambda: _DB())
    monkeypatch.setattr(audio_route, "_known_devices",
                        lambda **_: (["MacBook Air Microphone"], ["AirPods Pro", "MacBook Air Speakers"]))

    out = audio_route.execute("use_output", {"device": "耳机"})
    assert out["ok"] and out["device"] == "AirPods Pro"
    assert store["host.audio.spk_label"] == "AirPods Pro"

    store["host.audio.disabled_mic_labels"] = json.dumps(["MacBook Air Microphone"])
    mic = audio_route.execute("use_input", {"device": "内置麦"})
    assert mic["ok"] and mic["device"] == "MacBook Air Microphone"
    assert json.loads(store["host.audio.active_mic_labels"]) == ["MacBook Air Microphone"]
    assert json.loads(store["host.audio.disabled_mic_labels"]) == []

    # 只有明说「跟随系统」才清空偏好。「默认」曾经也算，结果模型拿它当万能答案：
    # 「把声音切到扬声器」被传成「默认」，回执说「改回跟随系统」，它却播报
    # 「切到扬声器了」——说的和做的对不上。
    back = audio_route.execute("use_output", {"device": "跟随系统"})
    assert back["ok"] and store["host.audio.spk_label"] == ""


def test_object_contract_exposes_the_route():
    from tools import object_control

    obj = json.loads(object_control.execute({"op": "inspect", "target": "agent.audio"})[0])["object"]
    assert set(obj["commands"]) == {"use_output", "use_input", "status"}
    assert "device" in obj["command_args"]["use_output"]
    # 用户会说「耳机」「麦克风」，别名要认得
    assert "耳机" in obj["aliases"] and "麦克风" in obj["aliases"]


def test_switching_asks_the_terminal_to_re_enumerate(monkeypatch):
    """写完偏好还要敲令牌，否则语音终端根本看不见新设备。

    终端的设备表是 PortAudio 在进程启动时建的：新插的耳机不在表里，偏好里
    写什么都落不了地——用户表现为「agent 说切好了，声音还是从扬声器出」。
    令牌是跨进程信号；终端读到变化后自己停麦→重扫→开麦（实测 0.3 秒），
    而不是在麦克风流底下抽地毯（那版麦克风有时 8 秒才回来，有时再也回不来）。
    """
    from control_plane import audio_route

    store = {}

    class _DB:
        @staticmethod
        def get_setting(key, default=""):
            return store.get(key, default)

        @staticmethod
        def set_setting(key, value):
            store[key] = value

    monkeypatch.setattr(audio_route, "_db", lambda: _DB())
    monkeypatch.setattr(audio_route, "_known_devices",
                        lambda **_: (["MacBook Air Microphone"], ["AirPods Pro"]))

    audio_route.execute("use_output", {"device": "耳机"})
    assert store["host.audio.rescan_token"]          # 敲过了
    first = store["host.audio.rescan_token"]

    audio_route.execute("use_output", {"device": "跟随系统"})
    assert store["host.audio.rescan_token"] != first  # 改回默认也要重扫

    # 没切成功就不该惊动终端
    store.pop("host.audio.rescan_token")
    audio_route.execute("use_output", {"device": "不存在的设备"})
    assert "host.audio.rescan_token" not in store


def test_host_audio_api_never_persists_more_than_one_active_mic(monkeypatch):
    """旧前端或异常请求传入多项时，服务端也必须把输入选择截成一项。"""
    import routes_devices

    store = {}

    class _DB:
        @staticmethod
        def get_setting(key, default=""):
            return store.get(key, default)

        @staticmethod
        def set_setting(key, value):
            store[key] = value

    monkeypatch.setattr(routes_devices, "db", _DB())
    routes_devices.api_host_audio_put({
        "active_mic_ids": ["pa-in-2", "pa-in-8"],
        "active_mic_labels": ["Desk Microphone", "Headset Microphone"],
    })

    assert json.loads(store["host.audio.active_mic_ids"]) == ["pa-in-2"]
    assert json.loads(store["host.audio.active_mic_labels"]) == ["Desk Microphone"]

    # 兼容数据库里已经存在的旧多选值：读取给客户端时同样只暴露第一项。
    store["host.audio.active_mic_ids"] = json.dumps(["pa-in-2", "pa-in-8"])
    store["host.audio.active_mic_labels"] = json.dumps(
        ["Desk Microphone", "Headset Microphone"]
    )
    prefs = routes_devices.api_host_audio_get()
    assert prefs["active_mic_ids"] == ["pa-in-2"]
    assert prefs["active_mic_labels"] == ["Desk Microphone"]
