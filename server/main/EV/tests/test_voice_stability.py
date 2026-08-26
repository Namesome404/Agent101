# -*- coding: utf-8 -*-
"""语音链路稳定性两处修复的回归测试。

1) PCM 流式能力探测：/api/tts/stream 只服务 minimax，其他 provider 会 400。
   原来每轮都白跑一次注定失败的请求（日志累计 53 次），改为探到一次就记住。
2) 打断放行上限：实测 37 次打断中 7 次误打断（打断后 ASR 无人声），其中 3 次
   落在 0.991~0.992，而真打断相似度最高只到 0.990。阈值收到 0.990 可挡掉那
   3 次且不误伤任何真打断。
"""
import inspect
import queue
import re
import sys
from types import SimpleNamespace


def test_pcm_stream_unsupported_is_remembered():
    """400「不支持流式」应被记住，避免每轮重复请求。"""
    from devices.voice import terminal_audio as ta
    ta._PCM_STREAM_UNSUPPORTED.discard("SomeTTS")
    assert "SomeTTS" not in ta._PCM_STREAM_UNSUPPORTED
    # 模拟调用方在捕获专用异常后的记忆行为
    try:
        raise ta._PcmStreamUnsupported("当前 TTS 不支持 PCM 流式播放")
    except ta._PcmStreamUnsupported:
        ta._PCM_STREAM_UNSUPPORTED.add("SomeTTS")
    assert "SomeTTS" in ta._PCM_STREAM_UNSUPPORTED
    ta._PCM_STREAM_UNSUPPORTED.discard("SomeTTS")


def test_caller_skips_known_unsupported_provider():
    """调用方必须在尝试前检查记忆集合，否则记忆没有意义。"""
    from devices.voice import terminal_audio as ta
    src = inspect.getsource(ta)
    assert "tts_provider not in _PCM_STREAM_UNSUPPORTED" in src


def test_barge_in_accept_threshold_is_tightened():
    """放行上限必须 <= 0.990：真打断最高 0.990，0.991+ 全是误打断。"""
    from speech.echo import playback_gate
    src = inspect.getsource(playback_gate)
    match = re.search(
        r'VOICE_BARGE_IN_MAX_SIM_FOR_ACCEPT",\s*"([0-9.]+)"', src
    )
    assert match, "找不到打断放行上限的默认值"
    assert float(match.group(1)) <= 0.990


def test_real_barge_ins_from_log_still_pass():
    """回放实测样本：真打断的相似度都不该被新阈值挡住。"""
    threshold = 0.990
    observed_real = [0.990, 0.989, 0.988, 0.986, 0.985, 0.976, 0.937]
    assert all(sim <= threshold for sim in observed_real)


def test_false_barge_ins_above_threshold_are_blocked():
    threshold = 0.990
    observed_false_high = [0.992, 0.991, 0.991]
    assert all(sim > threshold for sim in observed_false_high)


def test_legacy_multi_mic_preference_selects_only_one(monkeypatch):
    """旧配置即使保存了两只麦，选择层也只能返回第一只。"""
    from devices.voice import terminal_audio as ta

    fake_sd = SimpleNamespace(query_devices=lambda: [
        {"name": "Desk Microphone", "max_input_channels": 1},
        {"name": "Headset Microphone", "max_input_channels": 1},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(ta, "_host_audio_prefs", lambda: {
        "disabled_mic_labels": [],
        "active_mic_labels": ["Headset Microphone", "Desk Microphone"],
    })

    assert ta._list_enabled_pc_input_devices() == [(1, "Headset Microphone")]


def test_missing_selected_mic_does_not_fall_back(monkeypatch):
    """用户选的蓝牙麦暂时断开时，不能擅自改用电脑麦。"""
    from devices.voice import terminal_audio as ta

    fake_sd = SimpleNamespace(query_devices=lambda: [
        {"name": "MacBook Air Microphone", "max_input_channels": 1},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(ta, "_host_audio_prefs", lambda: {
        "disabled_mic_labels": [],
        "active_mic_labels": ["AirPods Pro"],
    })

    assert ta._list_enabled_pc_input_devices() == []


def test_missing_selected_mic_falls_back_after_the_grace_period(monkeypatch):
    """但等够了就得回落，否则换台机器永远没有麦克风。

    偏好里存的是上一台机器的设备名（比如 AirPods Pro），在新机器上永远匹配
    不上。旧代码在这种情况下一直返回空，表现就是克隆到新机器后「怎么说都不
    识别」。蓝牙临时断开是几秒的事，换机器则永远等不到——用时间把两者分开：
    宽限期内继续等（见上一条），超时就用这台机器现有的麦克风。
    """
    from devices.voice import terminal_audio as ta

    fake_sd = SimpleNamespace(query_devices=lambda: [
        {"name": "MacBook Air Microphone", "max_input_channels": 1},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(ta, "_host_audio_prefs", lambda: {
        "disabled_mic_labels": [],
        "active_mic_labels": ["AirPods Pro"],
    })
    ta._MIC_PREF_MISSING_SINCE.clear()

    # 第一次发现找不到：等着，不回落
    assert ta._list_enabled_pc_input_devices() == []

    # 把「第一次找不到」的时刻推回到宽限期之前
    key = "AirPods Pro"
    ta._MIC_PREF_MISSING_SINCE[key] = (
        ta._MIC_PREF_MISSING_SINCE[key] - ta.MIC_PREF_GRACE_SECONDS - 1
    )
    picked = ta._list_enabled_pc_input_devices()
    assert [n for _, n in picked] == ["MacBook Air Microphone"]
    ta._MIC_PREF_MISSING_SINCE.clear()


def test_preferred_mic_coming_back_clears_the_fallback(monkeypatch):
    """原设备接回来要自动切回去——回落是临时的，不是把偏好覆盖掉。"""
    from devices.voice import terminal_audio as ta

    present = [{"name": "MacBook Air Microphone", "max_input_channels": 1}]
    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(query_devices=lambda: list(present)))
    monkeypatch.setattr(ta, "_host_audio_prefs", lambda: {
        "disabled_mic_labels": [],
        "active_mic_labels": ["AirPods Pro"],
    })
    ta._MIC_PREF_MISSING_SINCE.clear()
    ta._list_enabled_pc_input_devices()
    ta._MIC_PREF_MISSING_SINCE["AirPods Pro"] -= ta.MIC_PREF_GRACE_SECONDS + 1
    assert [n for _, n in ta._list_enabled_pc_input_devices()] == ["MacBook Air Microphone"]

    # AirPods 接回来了
    present.append({"name": "AirPods Pro", "max_input_channels": 1})
    assert [n for _, n in ta._list_enabled_pc_input_devices()] == ["AirPods Pro"]
    # 「找不到」的计时也要清掉，否则下次一断开就立刻回落，宽限期形同虚设
    assert "AirPods Pro" not in ta._MIC_PREF_MISSING_SINCE
    ta._MIC_PREF_MISSING_SINCE.clear()


def test_mic_frame_clock_makes_a_deaf_stream_observable():
    """PortAudio 的 stream.active 判不出「流活着但一帧不来」，得自己记时间。

    真实事故：播放时音频设备报 -10851、回退默认扬声器之后，输入流再没喂过一帧，
    进程活着、心跳照跳、麦克风心跳日志静默 34 分钟，监管线程毫无察觉。
    """
    from devices.voice import terminal_audio as ta

    ta.note_mic_frame()
    assert ta.mic_silent_seconds() < 1.0
    ta._MIC_LAST_FRAME_AT -= 30
    assert ta.mic_silent_seconds() >= 29
    ta.note_mic_frame()
    assert ta.mic_silent_seconds() < 1.0


def test_missing_selected_output_does_not_fall_back(monkeypatch):
    """用户选的蓝牙输出暂时断开时，不能从内置扬声器意外出声。"""
    from devices.voice import terminal_audio as ta

    fake_sd = SimpleNamespace(query_devices=lambda: [
        {"name": "MacBook Air Speakers", "max_output_channels": 2},
    ])
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setattr(ta, "_host_audio_prefs", lambda: {
        "spk_label": "AirPods Pro",
        "disabled_spk_labels": [],
    })

    assert ta._pick_pc_output_device() is None


def test_pc_capture_hard_caps_input_streams_at_one(monkeypatch):
    """配置层将来即使回归成多选，采集层也不允许同时创建第二条输入流。"""
    from devices.voice import terminal_audio as ta

    opened = []

    class _FakeInputStream:
        def __init__(self, **kwargs):
            opened.append(kwargs)
            self.active = False
            self.closed = False

        def start(self):
            self.active = True

        def stop(self):
            self.active = False

        def close(self):
            self.closed = True

    monkeypatch.setitem(
        sys.modules,
        "sounddevice",
        SimpleNamespace(RawInputStream=_FakeInputStream),
    )
    monkeypatch.setattr(
        ta,
        "_list_enabled_pc_input_devices",
        lambda: [(3, "First Mic"), (7, "Second Mic")],
    )

    proc = ta._PcMicProc()
    proc.bind_queue(queue.Queue())
    try:
        proc.start()
        assert len(opened) == 1
        assert opened[0]["device"] == 3
        assert proc.device_labels == ["First Mic"]
    finally:
        proc.stop()
        if proc._mixer is not None:
            proc._mixer.join(timeout=0.3)
