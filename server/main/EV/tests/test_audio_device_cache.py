"""设备枚举要缓存，但不能因此让「切设备」看到过期的设备表。

背景：枚举一次要 fork 子进程并在里面 import sounddevice，实测 77ms。而
world() 每轮语音至少被调三次（capability_hint、render、_mentions_live_object），
有动作再加一次——每轮白等 230~310ms，比工具执行本身（中位 168ms）还贵。

缓存的安全边界只有一条：用户主动切设备时必须看当下的真值，否则刚插上的耳机
选不中。切设备会写 rescan_token，缓存拿它当钥匙。
"""

import time

from control_plane import audio_route


def _reset():
    audio_route._DEVICE_CACHE.update({"at": 0.0, "token": None, "value": None})


def test_repeated_reads_enumerate_only_once(monkeypatch):
    calls = []
    monkeypatch.setattr(audio_route, "_enumerate_devices",
                        lambda: (calls.append(1), (["麦A"], ["喇叭A"]))[1])
    monkeypatch.setattr(audio_route, "_db", lambda: type("D", (), {
        "get_setting": staticmethod(lambda *a: "tok-1"),
    })())
    _reset()

    for _ in range(10):
        assert audio_route._known_devices() == (["麦A"], ["喇叭A"])
    assert len(calls) == 1, "十次读只该真枚举一次"


def test_switching_devices_bumps_the_token_and_busts_the_cache(monkeypatch):
    """切设备写 rescan_token；token 一变，缓存立刻作废。"""
    calls = []
    devices = [(["麦A"], ["喇叭A"])]
    monkeypatch.setattr(audio_route, "_enumerate_devices",
                        lambda: (calls.append(1), devices[0])[1])
    token = ["tok-1"]
    monkeypatch.setattr(audio_route, "_db", lambda: type("D", (), {
        "get_setting": staticmethod(lambda *a: token[0]),
    })())
    _reset()

    audio_route._known_devices()
    audio_route._known_devices()
    assert len(calls) == 1

    # 用户插上耳机并切过去：token 变了
    devices[0] = (["麦A", "AirPods Pro"], ["喇叭A", "AirPods Pro"])
    token[0] = "tok-2"
    assert audio_route._known_devices() == (["麦A", "AirPods Pro"], ["喇叭A", "AirPods Pro"])
    assert len(calls) == 2, "token 变了必须重新枚举"


def test_max_age_zero_always_enumerates(monkeypatch):
    """切设备那条路强制取新的：拿错设备名比多等 77ms 严重得多。"""
    calls = []
    monkeypatch.setattr(audio_route, "_enumerate_devices",
                        lambda: (calls.append(1), (["麦A"], ["喇叭A"]))[1])
    monkeypatch.setattr(audio_route, "_db", lambda: type("D", (), {
        "get_setting": staticmethod(lambda *a: "tok-1"),
    })())
    _reset()

    audio_route._known_devices()
    audio_route._known_devices(max_age=0)
    audio_route._known_devices(max_age=0)
    assert len(calls) == 3


def test_cache_expires_on_its_own(monkeypatch):
    """没人切设备时也别一直用旧的——插拔不写 token，靠 TTL 兜底。"""
    calls = []
    monkeypatch.setattr(audio_route, "_enumerate_devices",
                        lambda: (calls.append(1), (["麦A"], ["喇叭A"]))[1])
    monkeypatch.setattr(audio_route, "_db", lambda: type("D", (), {
        "get_setting": staticmethod(lambda *a: "tok-1"),
    })())
    _reset()

    audio_route._known_devices()
    assert len(calls) == 1
    audio_route._DEVICE_CACHE["at"] = time.time() - audio_route._DEVICE_TTL_SECONDS - 1
    audio_route._known_devices()
    assert len(calls) == 2


def test_a_failed_enumeration_is_not_cached(monkeypatch):
    """枚举失败返回空，不能缓存——否则接下来几秒都以为这机器没有音频设备。"""
    results = [([], []), (["麦A"], ["喇叭A"])]
    calls = []

    def fake():
        calls.append(1)
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(audio_route, "_enumerate_devices", fake)
    monkeypatch.setattr(audio_route, "_db", lambda: type("D", (), {
        "get_setting": staticmethod(lambda *a: "tok-1"),
    })())
    _reset()

    assert audio_route._known_devices() == ([], [])
    assert audio_route._known_devices() == (["麦A"], ["喇叭A"])
    assert len(calls) == 2, "失败那次不该被缓存下来"
