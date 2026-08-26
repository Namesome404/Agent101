# -*- coding: utf-8 -*-
"""窗口尺寸跟着内容走。

以前 normalize 无条件补上 520x380，窗口层就再也分不清「模型指定了高度」
和「没人指定、只是默认值」——一行备忘和一长串清单拿到同样大的窗口。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from devices.coding import surface_layout as layout


def _window(definition, current=None):
    return layout.normalize_web_surface_definition(definition, current=current)["window"]


def test_window_height_follows_content_volume():
    one_line = _window({"content": {"text": "记得买牛奶"}})
    many_lines = _window({"content": {"items": ["第 %d 条待办" % n for n in range(12)]}})
    long_text = _window({"content": {"text": "很长的一段说明。" * 80}})
    assert one_line["fit"] == "content"
    assert one_line["height"] < many_lines["height"] < long_text["height"]
    # 估算值仍受上下限约束，不会给出荒唐的窗口
    assert 160 <= one_line["height"] <= 1400
    assert long_text["height"] <= 1400


def test_explicit_height_wins_and_stays_fixed():
    """模型/用户指定过高度的窗口不再被自适应覆盖。"""
    fixed = _window({"content": {"text": "短"}, "window": {"height": 600}})
    assert fixed["height"] == 600 and fixed["fit"] == "fixed"
    # 之后只改内容，也不该把它拉回估算高度
    later = _window({"content": {"text": "改了内容" * 50}}, current={"window": dict(fixed)})
    assert later["fit"] == "fixed" and later["height"] == 600


def test_unmeasurable_content_keeps_default_height():
    """网页/自定义 HTML 的高度服务端量不出来，交给窗口内实测，别瞎猜。"""
    assert layout.estimate_content_height({"content": {"type": "url", "url": "https://a"}}, 520) is None
    assert layout.estimate_content_height({"content": {"type": "html", "html": "<b>x</b>"}}, 520) is None
    assert _window({"content": {"type": "url", "url": "https://a"}})["height"] == 380


def test_measured_fit_declaration_overrides_height_inference():
    """实测回填带着 fit=content，不能因为它写了 height 就被判成 fixed。"""
    refit = _window({"window": {"height": 512, "fit": "content"}, "content": {"text": "x"}})
    assert refit["fit"] == "content"


def test_cjk_lines_count_wider_than_ascii():
    assert layout._wrapped_lines("中文一行", 20) >= layout._wrapped_lines("abcd", 20)


def test_measured_size_requires_explicit_fit_declaration(monkeypatch):
    """旧外壳报的是容器矩形，不带 fit 声明——放行它等于让窗口每报一次缩一截。"""
    calls = []

    class _Store:
        def get(self, surface_id):
            return {"kind": "web", "data": {"window": {"width": 520, "height": 380, "fit": "content"}}}

        def upsert(self, surface_id, **kwargs):
            calls.append(kwargs)

    import devices.coding.scene_store as store_module

    monkeypatch.setattr(store_module, "scene_store", _Store())
    assert layout.apply_measured_window_size("s1", height=334, declared_fit="") is False
    assert layout.apply_measured_window_size("s1", height=334, declared_fit="fixed") is False
    assert calls == []
    assert layout.apply_measured_window_size("s1", height=334, declared_fit="content") is True
    assert calls[0]["data"]["window"]["height"] == 334


def test_beacon_only_attaches_to_fit_content_html(monkeypatch):
    fitted = layout.attach_fit_beacon(
        {"window": {"fit": "content"}, "content": {"html": "<p>hi</p>"}}, surface_id="s-1"
    )
    assert "ev-fit-beacon" in fitted["content"]["html"]
    # 二次归一化不该重复注入
    again = layout.attach_fit_beacon(fitted, surface_id="s-1")
    assert again["content"]["html"].count("ev-fit-beacon") == 1
    # 用户定死尺寸的窗口不挂信标
    fixed = layout.attach_fit_beacon(
        {"window": {"fit": "fixed"}, "content": {"html": "<p>hi</p>"}}, surface_id="s-1"
    )
    assert "ev-fit-beacon" not in fixed["content"]["html"]


def test_create_keeps_content_written_in_natural_field_names():
    """模型把内容写在 content= 里也不能丢——窗口不该只剩一个标题。"""
    from tools import surface_control

    markup, _ = surface_control._structured_page(
        {"title": "传统节日", "content": "春节：正月初一\n元宵：正月十五\n中秋：八月十五"}
    )
    assert markup.count("<li>") == 3
    assert "春节：正月初一" in markup
    single, _ = surface_control._structured_page({"title": "提醒", "content": "明天九点开会"})
    assert "明天九点开会" in single
    # 已经用规范字段的调用不受影响
    formal, _ = surface_control._structured_page(
        {"title": "报告", "summary": "正文", "content": "不该覆盖 summary"}
    )
    assert "正文" in formal and "不该覆盖" not in formal
