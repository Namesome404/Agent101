# -*- coding: utf-8 -*-
from control_plane import info_panel
from tools import canvas_control


def setup_function():
    info_panel.clear()


def teardown_function():
    info_panel.clear()


def test_tool_is_inspect_plus_flat_stable_view_fields():
    function = canvas_control.tool_definition()["function"]
    assert function["name"] == "canvas_control"
    properties = function["parameters"]["properties"]
    assert properties["action"]["enum"] == ["inspect", "apply"]
    assert {"focus_id", "selected_id", "zoom", "fullscreen"} <= set(properties)
    assert "changes" not in properties
    schema = str(function)
    assert "focus_index" not in schema
    assert "figure" not in schema
    assert "JSON Patch" not in schema


def test_inspect_returns_stable_nodes_then_apply_can_focus_a_node():
    pushed = info_panel.push({
        "query": "找两张图",
        "title": "图片研究",
        "images": [
            {"url": "https://example.com/a.jpg"},
            {"url": "https://example.com/b.jpg"},
        ],
        "items": [{"title": "来源", "url": "https://example.com"}],
    }, kind="search")
    text, inspected = canvas_control.execute({"action": "inspect"})
    assert inspected["ok"] is True
    assert "image-1:image" in text
    assert inspected["rev"] == pushed["rev"]

    _text, applied = canvas_control.execute({
        "action": "apply",
        "tab_id": inspected["tab_id"],
        "base_rev": inspected["rev"],
        "focus_id": "image-2",
        "zoom": 1.5,
    })
    assert applied["ok"] is True and applied["changed"] is True
    assert applied["document"]["view"]["focus_id"] == "image-2"
    assert applied["document"]["view"]["zoom"] == 1.5


def test_open_and_enlarge_image_uses_top_level_view_without_touching_node():
    pushed = info_panel.push({
        "query": "故宫",
        "summary": "故宫简介",
        "images": [{"url": "https://example.com/one.jpg"}],
    }, kind="search")
    before = pushed["document"]["nodes"]["image-1"].copy()
    _text, result = canvas_control.execute({
        "action": "apply",
        "base_rev": pushed["rev"],
        "focus_id": "image-1", "selected_id": "image-1", "zoom": 2,
    })
    assert result["ok"] is True
    assert result["document"]["view"]["focus_id"] == "image-1"
    assert result["document"]["view"]["zoom"] == 2
    assert result["document"]["nodes"]["image-1"] == before


def test_unknown_fields_are_rejected_without_deleting_an_image():
    pushed = info_panel.push({
        "query": "故宫",
        "images": [{"url": "https://example.com/one.jpg"}],
    }, kind="search")
    _text, result = canvas_control.execute({
        "action": "apply",
        "base_rev": pushed["rev"],
        "changes": {"view": {"scale": 2}},
    })
    assert result["ok"] is False
    assert result["error"] == "invalid_changes"
    snapshot = info_panel.snapshot()
    assert "image-1" in snapshot["document"]["nodes"]
    assert snapshot["rev"] == pushed["rev"]


def test_common_model_aliases_do_not_create_another_retry_loop():
    pushed = info_panel.push({
        "query": "故宫",
        "images": [{"url": "https://example.com/one.jpg"}],
    }, kind="search")
    _text, result = canvas_control.execute({
        "action": "apply",
        "base_rev": pushed["rev"],
        "changes": {"view": {"focus": "image-1", "zoom": "fill"}},
    })
    assert result["ok"] is True
    assert result["document"]["view"]["focus_id"] == "image-1"
    assert result["document"]["view"]["zoom"] == 2

    _text, closed = canvas_control.execute({
        "action": "apply",
        "base_rev": result["rev"],
        "focus_id": "",
        "zoom": 1,
    })
    assert closed["ok"] is True
    assert closed["document"]["view"]["focus_id"] == ""


def test_apply_requires_inspected_revision():
    info_panel.push({"query": "故宫", "summary": "A"}, kind="search")
    _text, result = canvas_control.execute({
        "action": "apply", "zoom": 2,
    })
    assert result["ok"] is False
    assert result["error"] == "invalid_changes"


def test_final_answer_replaces_scraped_summary_and_survives_background_enrichment():
    pushed = info_panel.push({
        "query": "故宫", "summary": "检索到相关来源：一堆标题",
        "items": [{"title": "旧来源", "url": "https://example.com/old"}],
    }, kind="search", pending=True)
    answered = info_panel.set_answer(
        pushed["active_tab_id"],
        "故宫是明清两代皇宫，参观北京故宫需要提前预约，通常周一闭馆。",
    )
    summary = answered["document"]["nodes"]["summary"]
    assert summary["title"] == "故宫"
    assert summary["text"] != "故宫是明清两代皇宫，参观北京故宫需要提前预约，通常周一闭馆。"
    assert summary["text"] == "故宫是明清两代皇宫，参观北京故宫需要提前预约。"
    enriched = info_panel.push({
        "query": "故宫", "summary": "抓取正文菜单和无关导航",
        "items": [{"title": "故宫博物院", "url": "https://www.dpm.org.cn"}],
    }, kind="search", expand=False)
    assert enriched["document"]["nodes"]["summary"]["text"] == summary["text"]
    assert enriched["document"]["nodes"]["source-1"]["title"] == "故宫博物院"
    assert enriched["document"]["answer_locked"] is True


def test_background_enrichment_does_not_replace_the_latest_visible_summary():
    older = info_panel.push({"query": "旧搜索", "summary": "A"}, kind="search")
    newest = info_panel.push({"query": "新搜索", "summary": "B"}, kind="search")
    refreshed = info_panel.push(
        {"query": "旧搜索", "summary": "补充内容"},
        kind="search",
        expand=False,
        activate=False,
    )
    assert refreshed["active_tab_id"] == newest["active_tab_id"]
    assert refreshed["active_tab_id"] != older["active_tab_id"]


def test_display_summary_title_removes_search_only_suffixes():
    assert info_panel._compact_summary_title(
        "故宫参观要求 预约 门票 注意事项"
    ) == "故宫参观要求"


def test_tabs_are_preserved_and_activated_through_the_same_patch_transaction():
    first = info_panel.push({"query": "第一项", "summary": "A"}, kind="search")
    first_id = first["active_tab_id"]
    second = info_panel.push({"query": "第二项", "summary": "B"}, kind="search")
    second_id = second["active_tab_id"]
    assert first_id != second_id
    assert len(second["tabs"]) == 2

    switched = info_panel.apply(
        base_rev=second["rev"],
        patches=[{"op": "replace", "path": "/active_tab_id", "value": first_id}],
    )
    assert switched["ok"] is True
    assert switched["active_tab_id"] == first_id
    summary = switched["document"]["nodes"]["summary"]
    assert summary["title"] == "第一项"
    assert summary["text"] == "A"
