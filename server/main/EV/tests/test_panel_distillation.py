# -*- coding: utf-8 -*-
"""面板列的是回答里真正讲到的对象，不是搜索命中的文章页。

真实事故：用户问「找几个适合入门的开源机械臂项目」，回答讲的是 MeArm / Dummy /
Moveo，面板却列出「15+个值得收藏的开源机械臂大盘点 - 知乎」「有哪些开源机械臂
项目 – PingCode」这类盘点文章，结论区是口播稿被截断的第一句。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_ANSWER = (
    "给你说三个，按难度排。最入门的是 **MeArm**，四轴，亚克力件加微型舵机，"
    "太极创客有整套中文教程。再往上是 **Dummy**，稚晖君开源的极简六轴，"
    "3D 打印件多，BOM 压得很低。还有 **Moveo**，BCN3D 的五轴臂，零件多一些。"
)
_ITEMS = [
    {"title": "MeArm 篇 - 太极创客", "url": "https://taichi-maker.com/mearm",
     "snippet": "MeArm 是一款开源机械臂硬件"},
    {"title": "开源机械臂大盘点 - 知乎", "url": "https://zhuanlan.zhihu.com/p/204436189",
     "snippet": "稚晖君开源的极简六轴桌面机械臂 Dummy"},
]


def test_entries_come_from_the_objects_the_answer_recommends():
    import app

    entries = app._panel_entries_from_answer(_ANSWER, {"items": _ITEMS})
    assert [e["name"] for e in entries] == ["MeArm", "Dummy", "Moveo"]
    # 每条只留属于自己的那句，不带引出下一条的连接词
    assert entries[0]["note"] == "四轴，亚克力件加微型舵机，太极创客有整套中文教程"
    assert "再往上是" not in entries[0]["note"]
    # 链接由服务端按名字对回真实结果；对不上就没有链接，绝不编
    assert entries[0]["url"] == "https://taichi-maker.com/mearm"
    assert entries[1]["url"] == "https://zhuanlan.zhihu.com/p/204436189"
    assert entries[2]["url"] == ""


def test_single_object_answer_keeps_the_evidence_view():
    """只讲一个对象时列成清单没有意义，走原来的证据展示。"""
    import app

    assert app._panel_entries_from_answer("就用 **MeArm** 吧，最简单。", {"items": _ITEMS}) == []


def test_declared_entries_replace_raw_hits_in_the_live_panel():
    """条目要真的替换掉面板里那些盘点文章，并且引子不再复述条目本身。"""
    import app
    from control_plane import info_panel

    pushed = info_panel.push({
        "kind": "search",
        "query": "入门开源机械臂",
        "title": "入门开源机械臂",
        "summary": "检索到相关来源",
        "items": [
            {"title": "15+个值得收藏的开源机械臂大盘点 - 知乎",
             "url": "https://zhuanlan.zhihu.com/p/1", "snippet": "盘点文章"},
            {"title": "有哪些开源机械臂项目 – PingCode",
             "url": "https://pingcode.com/x", "snippet": "综述"},
        ],
    })
    tab_id = str(((pushed.get("document") or {}).get("id")) or "")
    assert tab_id
    entries = app._panel_entries_from_answer(_ANSWER, {"items": _ITEMS})
    info_panel.set_answer(tab_id, _ANSWER, entries=entries)
    document = (info_panel.snapshot() or {}).get("document") or {}
    nodes = document.get("nodes") or {}
    sources = [n for n in nodes.values() if n.get("type") == "source"]
    assert [n["title"] for n in sources] == ["MeArm", "Dummy", "Moveo"]
    assert not any("大盘点" in n["title"] or "PingCode" in n["title"] for n in sources)
    # 引子不再把条目内容再讲一遍
    lead = str((nodes.get("summary") or {}).get("text") or "")
    assert "MeArm" not in lead and "**" not in lead


def test_lead_never_shows_a_cut_off_fragment():
    """在条目名处切开后若没有句末标点，剩下的是半截话，不该摆出来。"""
    from control_plane import info_panel

    assert info_panel._compact_display_summary("") == ""
    # 粗体标记是给语音用的，面板上只会露出一对星号
    assert "**" not in info_panel._compact_display_summary("最入门的是 **MeArm**。")
