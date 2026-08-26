# -*- coding: utf-8 -*-
"""声音通道：把「从哪儿出声、用哪个麦」做成一个可被 agent 操作的对象。

以前只能在设备页上点，或者去 macOS 的声音设置里切。用户说「把声音切到耳机」
时，agent 手里没有任何能干这件事的能力。现在它是一个普通对象，走 object_control
invoke——和开关灯、关窗口同一条路。

真正的音频枚举在语音终端进程里（PortAudio 在进程启动时枚举一次），这里只负责
写偏好；终端在下一次开流时读到并切过去。目标设备不在终端的设备表里时如实报告，
不假装切成功了。
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Tuple

_SPK_KEY = "host.audio.spk_label"
_ACTIVE_MIC_KEY = "host.audio.active_mic_labels"
_DISABLED_MIC_KEY = "host.audio.disabled_mic_labels"
_DISABLED_SPK_KEY = "host.audio.disabled_spk_labels"
_RESCAN_KEY = "host.audio.rescan_token"


def _db():
    from control_plane import database as db

    return db


def _known_devices() -> Tuple[List[str], List[str]]:
    """本机可见的（输入, 输出）设备名。用独立进程枚举，拿到的是当下的真值。"""
    import subprocess
    import sys

    try:
        out = subprocess.run(
            [sys.executable, "-c",
             "import sounddevice as sd,json;d=sd.query_devices();"
             "print(json.dumps([[x['name'] for x in d if x['max_input_channels']>0],"
             "[x['name'] for x in d if x['max_output_channels']>0]]))"],
            capture_output=True, text=True, timeout=8,
        )
        ins, outs = json.loads((out.stdout or "[[],[]]").strip().splitlines()[-1])
        return list(dict.fromkeys(ins)), list(dict.fromkeys(outs))
    except Exception:
        return [], []


# 用户说的是「耳机」「扬声器」，设备名却是 AirPods Pro / MacBook Air Speakers。
# 一张类别别名表：说法 → 设备名里可能出现的片段。
_KIND_HINTS = (
    (("耳机", "耳麦", "headphone", "airpods", "earbud"), ("airpods", "headphone", "耳机", "buds")),
    (("扬声器", "音箱", "外放", "喇叭", "speaker"), ("speaker", "扬声器")),
    (("内置麦", "电脑麦", "笔记本麦", "本机麦", "built-in"), ("macbook", "built-in", "internal")),
)


def _match(name: str, pool: List[str]) -> str:
    needle = "".join(str(name or "").lower().split())
    if not needle:
        return ""
    for item in pool:
        if needle == "".join(item.lower().split()):
            return item
    for item in pool:
        flat = "".join(item.lower().split())
        if needle in flat or flat in needle:
            return item
    for spoken, fragments in _KIND_HINTS:
        # 整词相等才套类别别名：用子串会让「外星音箱」也命中「音箱」
        if needle not in spoken:
            continue
        for item in pool:
            flat = item.lower()
            if any(fragment in flat for fragment in fragments):
                return item
    return ""


def snapshot() -> Dict[str, Any]:
    ins, outs = _known_devices()
    db = _db()
    return {
        "output": str(db.get_setting(_SPK_KEY, "") or "").strip() or "（跟随系统默认）",
        "input": ", ".join(json.loads(db.get_setting(_ACTIVE_MIC_KEY, "[]") or "[]") or [])
        or "（跟随系统默认）",
        "available_outputs": outs,
        "available_inputs": ins,
    }


def execute(command: str, args: Dict[str, Any]) -> Dict[str, Any]:
    command = str(command or "").strip()
    wanted = str((args or {}).get("device") or "").strip()
    ins, outs = _known_devices()
    db = _db()

    if command == "status":
        state = snapshot()
        return {"ok": True, "state": state,
                "display": "出声=%s，收音=%s" % (state["output"], state["input"])}

    if command in ("use_output", "use_input"):
        pool = outs if command == "use_output" else ins
        if not wanted or wanted in ("auto", "系统默认", "跟随系统"):
            if command == "use_output":
                db.set_setting(_SPK_KEY, "")
            else:
                db.set_setting(_ACTIVE_MIC_KEY, "[]")
            db.set_setting(_RESCAN_KEY, str(time.time()))
            return {"ok": True, "changed": True,
                    "display": "已改回跟随系统默认"}
        hit = _match(wanted, pool)
        if not hit:
            return {
                "ok": False, "reason": "device_not_found",
                "detail": "本机没有叫「%s」的%s设备。现在能选的：%s" % (
                    wanted, "输出" if command == "use_output" else "输入",
                    "、".join(pool) or "（枚举不到）",
                ),
            }
        if command == "use_output":
            db.set_setting(_SPK_KEY, hit)
            disabled = json.loads(db.get_setting(_DISABLED_SPK_KEY, "[]") or "[]")
            if hit in disabled:
                disabled = [x for x in disabled if x != hit]
                db.set_setting(_DISABLED_SPK_KEY, json.dumps(disabled, ensure_ascii=False))
        else:
            db.set_setting(_ACTIVE_MIC_KEY, json.dumps([hit], ensure_ascii=False))
            # 设备页可能曾把这只麦明确关闭；用户现在点名切到它，就应同时解除禁用，
            # 否则 active 白名单会在枚举前被 disabled 列表挡掉，表面回执成功但没切换。
            disabled = json.loads(db.get_setting(_DISABLED_MIC_KEY, "[]") or "[]")
            if hit in disabled:
                disabled = [x for x in disabled if x != hit]
                db.set_setting(_DISABLED_MIC_KEY, json.dumps(disabled, ensure_ascii=False))
        # 敲一下令牌：语音终端的设备表是启动时枚举的，看不见新设备。
        # 它读到令牌变化后会自己停麦→重扫→开麦，把新设备认出来。
        db.set_setting(_RESCAN_KEY, str(time.time()))
        return {
            "ok": True, "changed": True, "device": hit,
            "display": "%s=%s" % ("出声" if command == "use_output" else "收音", hit),
            # 令牌已敲：终端会自己停麦→重扫→开麦，实测 0.3 秒完成
            "note": "已经切过去了",
        }
    return {"ok": False, "reason": "unknown_command",
            "detail": "声音通道支持：use_output / use_input / status"}
