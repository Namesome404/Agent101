"""通用表单：问用户几件事，答案回到发问的那一方。

为什么不用语音问：一次问五六项（工作目录、目标平台、要不要保留旧数据）用嘴念
一遍，用户记不住，模型也听不准。摆成一张表让人扫一眼填完，比来回对话快得多。

为什么不用 agent 手写 HTML：那种内容跑在 sandbox iframe 里（没有
allow-same-origin），够不着桌面壳的回传桥，答案根本传不出来。改成 EV 按字段声明
生成页面、用 url 窗口打开——那条挂的是子 webview，页面直接 fetch 回 EV，
桌面壳一行都不用改。
"""

import pytest

from control_plane import forms


FIELDS = [
    {"key": "platform", "type": "choice", "label": "先做哪个平台",
     "options": ["Android", "iOS", "两个都要"], "required": True},
    {"key": "reuse", "type": "bool", "label": "复用现有蓝牙协议层"},
    {"key": "note", "type": "textarea", "label": "还有什么要先说的"},
]


@pytest.fixture(autouse=True)
def clean():
    forms.forget_all()
    yield
    forms.forget_all()


def test_a_form_round_trips_from_declaration_to_answers():
    created = forms.declare("开工前确认", FIELDS, owner_kind="run", owner_id="work_1")
    assert created["fields"] == 3

    result = forms.submit(created["form_id"], {
        "platform": "两个都要", "reuse": True, "note": "先不动固件",
    })
    assert result["ok"] is True
    assert result["owner"] == {"kind": "run", "id": "work_1"}

    got = forms.answers_for("run", "work_1")
    assert len(got) == 1
    assert got[0]["answers"]["platform"] == "两个都要"
    assert got[0]["answers"]["reuse"] is True


def test_missing_required_fields_are_refused_whole():
    """缺必填就整张退回，不半截收下——半截答案比没答案更坏，
    发问的一方会以为问清楚了。"""
    created = forms.declare("开工前确认", FIELDS, owner_kind="run", owner_id="work_1")
    result = forms.submit(created["form_id"], {"note": "随便写点"})

    assert result["ok"] is False
    assert "先做哪个平台" in result["error"]
    assert forms.answers_for("run", "work_1") == [], "被拒绝的提交不该留下痕迹"


def test_submitting_twice_is_refused_and_keeps_the_first_answer():
    created = forms.declare("开工前确认", FIELDS, owner_kind="run", owner_id="work_1")
    forms.submit(created["form_id"], {"platform": "iOS"})
    again = forms.submit(created["form_id"], {"platform": "Android"})

    assert again["ok"] is False
    assert again["answers"]["platform"] == "iOS", "重复提交不能覆盖已收下的答案"


def test_answers_only_go_to_whoever_asked():
    """两次运行各问各的，别串台。"""
    a = forms.declare("A 的问题", FIELDS, owner_kind="run", owner_id="work_a")
    b = forms.declare("B 的问题", FIELDS, owner_kind="run", owner_id="work_b")
    forms.submit(a["form_id"], {"platform": "iOS"})
    forms.submit(b["form_id"], {"platform": "Android"})

    assert [i["answers"]["platform"] for i in forms.answers_for("run", "work_a")] == ["iOS"]
    assert [i["answers"]["platform"] for i in forms.answers_for("run", "work_b")] == ["Android"]
    assert forms.answers_for("voice") == []


def test_unanswered_forms_are_not_handed_out():
    """没填完的不算数，发问方不该拿到半张表。"""
    forms.declare("还没填", FIELDS, owner_kind="run", owner_id="work_1")
    assert forms.answers_for("run", "work_1") == []


def test_a_form_needs_at_least_one_field():
    with pytest.raises(ValueError):
        forms.declare("空表", [])


def test_unknown_field_types_degrade_instead_of_breaking():
    """字段类型只支持四种。给了没见过的就退回文本框，不是整张表报废。"""
    created = forms.declare("测试", [{"key": "x", "type": "colorpicker", "label": "颜色"}])
    assert forms.get(created["form_id"])["fields"][0]["type"] == "text"


def test_a_choice_without_options_degrades_to_text():
    created = forms.declare("测试", [{"key": "x", "type": "choice", "label": "选一个"}])
    assert forms.get(created["form_id"])["fields"][0]["type"] == "text"


def test_the_page_escapes_what_the_asker_wrote():
    """表单是别处声明的（工作 Agent 也能声明），标题和标签当数据处理，不当标记。"""
    created = forms.declare(
        "<script>alert(1)</script>",
        [{"key": "x", "label": "<img src=x onerror=alert(2)>"}],
    )
    page = forms.render_page(created["form_id"])
    # 看的是「有没有变成真的标签」，不是「危险字样在不在」——转义之后
    # onerror=alert(2) 这串字面量仍然在页面里，但它已经是纯文本。
    assert "<script>alert(1)" not in page
    assert "<img src=x" not in page
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;img src=x onerror=alert(2)&gt;" in page


def test_an_expired_form_says_so_instead_of_erroring():
    page = forms.render_page("nope")
    assert "过期" in page
