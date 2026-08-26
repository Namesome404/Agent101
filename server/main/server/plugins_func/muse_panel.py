# -*- coding: utf-8 -*-
"""构造 Muse 终端浮动窗口 payload，供技能插件与 connection 自动上屏。"""


def skill_panel(panel, title, data=None, **kwargs):
    payload = {
        "panel": panel,
        "title": title or "",
        "position": kwargs.get("position") or "right-top",
        "width": int(kwargs.get("width") or 440),
        "height": int(kwargs.get("height") or 380),
    }
    if data is not None:
        payload["data"] = data
    if kwargs.get("url"):
        payload["url"] = kwargs["url"]
    if kwargs.get("content"):
        payload["content"] = kwargs["content"]
    return payload
