# -*- coding: utf-8 -*-
"""世界现状：对象契约的紧凑投影。

以前每个子系统各写一套文案往提示词里塞状态——窗口 2971 字符的
「target=… | legacy_id=… | visible=是 | bounds(…)」、设备另一套散文，
计时器和面板则完全没有。三套词汇互不相通，模型「调用前读的」和
「回执里拿到的」对不上，也就无从核对。

现在状态住在契约里（state + adjustable），这里只做投影：一行一个对象，
数值内联，量纲跟着数值走。新增子系统只要把 state 填进契约就自动出现，
不用再写第四套文案。
"""
from __future__ import annotations

from typing import Any, Dict, List

_MAX_ROWS = 12


def _num(value: Any):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if float(value).is_integer() else round(float(value), 2)


def _duration(seconds: Any) -> str:
    total = _num(seconds)
    if total is None or total < 0:
        return ""
    return "%d:%02d" % (int(total) // 60, int(total) % 60)


def _adjustable_marks(obj: Dict[str, Any]) -> set:
    return {
        str(name) for name in (obj.get("adjustable") or {})
        if isinstance(obj.get("adjustable"), dict)
    }


def _device_row(obj):
    """设备现状由 provider 自己渲染（display）：色名、单位这些只有它清楚。

    这里曾经自己写了一张 RGB→色名表，和 led.NAMED_COLORS 的真值对不上
    （紫、橙、粉都差）——重复一份必然漂移。投影层只负责摆放和 ↕ 标记。
    """
    display = str(obj.get("display") or "").strip()
    if not display:
        return ""
    if "brightness" in _adjustable_marks(obj) and "亮度" in display:
        display = display.replace("亮度", "亮度", 1)
        display += "↕"
    return display


def _geometry(obj: Dict[str, Any]) -> str:
    bounds = (obj.get("state") or {}).get("bounds") or {}
    width, height = _num(bounds.get("width")), _num(bounds.get("height"))
    if width is None or height is None:
        return ""
    mark = "↕" if {"width", "height"} & _adjustable_marks(obj) else ""
    return "%d×%d%s" % (width, height, mark)


def _app_row(obj: Dict[str, Any]) -> str:
    state = obj.get("state") or {}
    if not state:
        return ""
    status = str(state.get("status") or "")
    remaining = _duration(state.get("remaining_seconds"))
    if status or remaining:
        label = {"running": "计时中", "paused": "已暂停", "finished": "已结束"}.get(status, status)
        return " ".join(part for part in (label, ("剩" + remaining) if remaining else "") if part)
    items = state.get("items")
    if isinstance(items, list):
        return "%d 条" % len(items)
    return ""


def render(*, search_hint: str = "") -> str:
    """把契约里的真实状态渲染成一段紧凑的世界现状。"""
    try:
        # 懒导入：provider 注册发生在 tools 层，直接拿 registry 会拿到空目录
        from tools import object_control

        object_control.ensure_builtin_provider()
        from control_plane.object_registry import object_registry
    except Exception:
        return ""
    objects = object_registry.world()
    if not isinstance(objects, list):
        return ""

    rows: List[str] = []
    hidden = 0
    # 行序必须稳定：目录顺序跟着 provider 的发现顺序走，同样的世界渲染出不同
    # 字节，等于白白丢掉前缀缓存，模型读到的顺序也会莫名其妙地跳。
    _KIND_RANK = {"ui": 0, "agent_task": 1, "canvas": 2, "app": 3, "mcp": 4}
    objects = sorted(
        [obj for obj in objects if isinstance(obj, dict)],
        key=lambda obj: (
            _KIND_RANK.get(str(obj.get("kind") or ""), 5 if str(obj.get("kind") or "").startswith("iot.") else 6),
            not bool((obj.get("state") or {}).get("focused")),
            str(obj.get("target_id") or ""),
        ),
    )
    for obj in objects:
        if not isinstance(obj, dict):
            continue
        kind = str(obj.get("kind") or "")
        target = str(obj.get("target_id") or "")
        name = str(obj.get("name") or target)[:22]
        state = obj.get("state") if isinstance(obj.get("state"), dict) else {}
        if kind == "surface":
            if not state.get("visible"):
                hidden += 1
                continue
            detail = " ".join(part for part in (
                _geometry(obj), "聚焦" if state.get("focused") else "",
                "载入中" if str(state.get("content_status") or "") == "loading" else "",
            ) if part)
            rows.append("窗 %s「%s」%s" % (target, name, detail))
        elif kind.startswith("iot."):
            detail = _device_row(obj)
            if detail:
                rows.append("设备 %s「%s」%s" % (target, name, detail))
        elif kind == "ui":
            detail = " ".join(part for part in (
                _geometry(obj),
                "展开" if state.get("expanded") else "收起",
            ) if part)
            rows.append("你自己 %s「%s」%s" % (target, name, detail))
        elif kind == "app":
            detail = _app_row(obj)
            if detail:
                rows.append("应用 %s「%s」%s" % (target, name, detail))
        elif kind == "canvas" and state.get("available"):
            title = str(state.get("active_title") or state.get("query") or "")[:20]
            rows.append("画布 %s%s" % (target, ("：" + title) if title else ""))
        elif kind == "mcp":
            # 接进来的外部能力也要在场，否则它对模型是隐形的。
            # 实测：退掉 url 窗口之后说「打开 YouTube」，模型连 inspect 五次都在
            # kind=surface 里翻，始终没想到浏览器——因为世界现状里根本没提过它。
            if not state.get("reachable"):
                rows.append("外部 %s「%s」连不上，这轮别指望它" % (target, name))
            else:
                # 有现状就报现状（浏览器：当前开着哪些页面），没有才退回描述。
                # 描述是静态的，现状才是这一轮该知道的东西。
                detail = str(obj.get("display") or "").strip()
                rows.append("外部 %s「%s」%s" % (
                    target, name,
                    detail[:60] if detail else str(obj.get("description") or "")[:40],
                ))
        elif kind == "agent_task" and str(state.get("phase") or "idle") != "idle":
            rows.append("工程 %s 相位=%s %s" % (
                target, state.get("phase"), str(state.get("goal") or "")[:24]))
        if len(rows) >= _MAX_ROWS:
            break

    if not rows and not hidden:
        return ""
    lines = ["【世界现状】（背景，不是让你现在动手；要不要动作以用户最新这句为准）"]
    lines.extend(rows)
    if hidden:
        lines.append(
            "另有 %d 个隐藏窗口，需要时 object_control inspect（selector.query=关键词）" % hidden
        )
    lines.append(
        "带 ↕ 的数值可用 adjust 相对调整（暗一点/大一点/挪一挪），当前值由服务端读，你不要自己算。"
        "屏幕上实际有什么以用户看到的为准；这里没有的东西别臆测。"
        # 这句原本长在 2888 字符的「窗口记忆」结尾，被我压缩成投影时一并删掉了。
        # 隔离实验：历史里只要有一条「助手刚报告做过某事」，「把 YouTube 关上」
        # 就 3/3 零调用（空历史时 3/3 正常）——模型判断「这事已经处理过了」。
        "同一个对象刚被动过，不代表这一次不用再动："
        "用户这次要求关闭/打开/修改，就是一次新动作，必须当场重新调用工具拿回执，"
        "历史里说过做过不算。"
    )
    if search_hint:
        lines.append(search_hint.strip())
    return "\n".join(lines)


def capability_hint(*, max_chars: int = 2000) -> str:
    """凡是声明了参数形状的对象，签名都进提示。

    预算 2000 而不是原来的 1200：装不下的对象会被折叠成「用到时先 inspect」，
    而一次 inspect 就是一整轮模型（实测中位 1.8 秒）。多几百字符的提示词换掉
    那一轮，账很好算——Chrome 接进来之后它一直落在预算外，于是每次开网页都
    先白跑一轮。

    只写在描述符里不够：模型不 inspect 就看不到，而 inspect 是一整个 LLM 来回。
    灯当初就是这样一次调灯要三次调用；实测「计时30分钟」同样先猜 {"minutes":30}
    才改对 duration_seconds。设备、内置应用、以后接进来的 MCP 能力共用这一份投影。
    """
    try:
        from tools import object_control

        object_control.ensure_builtin_provider()
        from control_plane.object_registry import object_registry

        objects = object_registry.world()
    except Exception:
        return ""
    # 常用的排前面：预算装不下时先牺牲冷门的。按字母序取舍会让新接进来的
    # mcp.* 排最后、第一个被挤掉——接了个能力参数清单却进不了提示词。
    from control_plane.object_registry import usage_rank

    def _order(obj):
        target = str(obj.get("target_id") or "")
        # 常驻的排最前，其次按用量。
        return (0 if obj.get("pinned") else 1, -usage_rank(target), target)

    lines = []
    pinned_lines = set()
    for obj in sorted(objects or [], key=_order):
        shapes = obj.get("command_args") if isinstance(obj.get("command_args"), dict) else {}
        shapes = {k: v for k, v in shapes.items() if v}
        if not shapes:
            continue
        head = "- %s（%s）" % (obj.get("target_id"), str(obj.get("name") or "")[:16])
        if obj.get("pinned"):
            pinned_lines.add(head)
        lines.append(head)
        for command, args in shapes.items():
            lines.append("  %s：%s" % (
                command, "；".join("%s=%s" % (k, v) for k, v in args.items()),
            ))
    if not lines:
        return ""
    # 按字符硬切会把最后一条切成半句（实测接一个 6 工具的 MCP 就越界：
    # 「red=integer；必填」之后直接断掉，led_effect / led_set 整个消失），
    # 而抬头还写着「已给全」——模型于是以为自己拿到了全部签名，转头自造参数。
    # 改成按对象为单位取，装不下的整块不要，并在抬头如实说还有几个没列。
    kept, used, dropped = [], 0, 0
    block: List[str] = []
    for line in lines + [""]:
        if line.startswith("- ") or line == "":
            if block:
                size = sum(len(item) + 1 for item in block)
                # 常驻的一定留下，哪怕超预算：它被折叠成「用到时先 inspect」，
                # 就等于每次用都白烧一整轮模型，比多几百字符贵得多。
                if used + size <= max_chars or block[0] in pinned_lines:
                    kept.extend(block)
                    used += size
                else:
                    dropped += 1
            block = []
        if line:
            block.append(line)
    if not kept:
        return ""
    if dropped:
        head = (
            "【对象命令的参数形状】（列出来的直接 invoke，不必先 inspect；"
            "另有 %d 个对象没列出来，用到时先 inspect 拿参数，别自造参数名）\n" % dropped
        )
    else:
        head = (
            "【对象命令的参数形状】（已给全，直接 invoke，不必先 inspect；"
            "不要自造命令名或参数名）\n"
        )
    return head + "\n".join(kept)
