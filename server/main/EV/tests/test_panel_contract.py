# -*- coding: utf-8 -*-
"""面板呈现契约：新增能力不该要求 surface_control 长大。

关键性质：
- 我们自己的能力带 panel 字段 → 按它渲染；
- 第三方 MCP 不按约定返回 → 按数据形状推断，零改造也能显示；
- kind 是数据不是 schema → 未知 kind 照样出结构，前端兜底渲染。
"""
from control_plane import panel_contract as pc
from control_plane import info_panel


def test_our_own_tool_result_with_panel_field():
    """deep_search 那种 {panel:{kind:search,...}} 直接可用。"""
    out = pc.normalize({
        "ok": True,
        "panel": {"kind": "search", "title": "光波导",
                  "items": [{"title": "A", "url": "https://a.com", "snippet": "s"}],
                  "images": [{"url": "https://i/x.png"}]},
    })
    assert out["kind"] == "search"
    assert out["title"] == "光波导"
    assert len(out["items"]) == 1 and out["items"][0]["url"] == "https://a.com"


def test_third_party_mcp_without_panel_field_is_inferred():
    """别人的 MCP 只返回 results 数组，也要能显示（零改造）。"""
    out = pc.normalize({"results": [
        {"name": "邮件一", "link": "https://m/1", "preview": "内容"},
        {"name": "邮件二", "link": "https://m/2"},
    ]}, kind="mail")
    assert out["kind"] == "mail"
    assert out["layout"] == "list"
    assert [i["title"] for i in out["items"]] == ["邮件一", "邮件二"]


def test_unknown_kind_still_produces_renderable_structure():
    """未知 kind（比如 pcb）不该被拒绝——前端有兜底渲染器。"""
    out = pc.normalize({"panel": {"kind": "pcb", "title": "ESP32 底板",
                                  "images": [{"url": "https://x/board.svg"}]}})
    assert out["kind"] == "pcb"
    assert out["layout"] == "figure"     # 有图无条目 → 大图
    assert out["images"][0]["url"].endswith("board.svg")


def test_layout_can_be_forced_by_caller():
    """用户问接线图 → 模型指定 figure，即使有一堆条目也按大图摆。"""
    out = pc.normalize(
        {"items": [{"title": "A", "url": "https://a.com"}],
         "images": [{"url": "https://i/pin.png"}]},
        layout="figure",
    )
    assert out["layout"] == "figure"


def test_layout_inference_by_shape():
    assert pc.normalize({"chart": {"type": "bar", "labels": ["a"], "values": [1]}})["layout"] == "chart"
    assert pc.normalize({"images": [{"url": "https://i/x.png"}]})["layout"] == "figure"
    assert pc.normalize({"summary": "一段话"})["layout"] == "text"
    assert pc.normalize({"items": [{"title": "A"}]})["layout"] == "list"


def test_dirty_data_is_rejected_before_reaching_renderer():
    """脏数据不能进渲染层。"""
    out = pc.normalize({
        "images": [{"url": "javascript:alert(1)"}, {"url": "https://ok/x.png"}],
        "chart": {"type": "bogus", "labels": ["a"], "values": [1]},
        "items": [{"title": ""}, "纯字符串条目"],
    })
    assert [i["url"] for i in out["images"]] == ["https://ok/x.png"]
    assert out["chart"] is None
    assert [i["title"] for i in out["items"]] == ["纯字符串条目"]


def test_nothing_renderable_returns_none():
    assert pc.normalize({"ok": True}) is None
    assert pc.normalize("not a dict") is None


def test_multiple_modalities_default_to_mixed_canvas():
    out = pc.normalize({
        "summary": "结论",
        "items": [{"title": "来源", "url": "https://example.com"}],
        "images": [{"url": "https://example.com/a.jpg"}],
    })
    assert out["layout"] == "mixed"


def test_table_and_ai_blocks_are_safely_normalized():
    out = pc.normalize({
        "title": "对比",
        "blocks": [
            {"type": "text", "text": "先看结论"},
            {"type": "table", "columns": ["方案", "价格"],
             "rows": [{"方案": "A", "价格": 10}]},
            {"type": "model", "url": "https://example.com/product.glb",
             "description": "可旋转模型"},
            {"type": "model", "url": "javascript:bad()"},
        ],
    })
    assert out["layout"] == "mixed"
    assert [block["type"] for block in out["blocks"]] == ["text", "table", "model"]
    assert out["blocks"][1]["rows"] == [["A", "10"]]
    assert out["blocks"][2]["model"]["url"].endswith("product.glb")


def test_glb_result_is_detected_as_interactive_model():
    out = pc.normalize({"items": [{
        "title": "3D 零件",
        "url": "https://example.com/part.glb?download=1",
        "snippet": "可交互预览",
    }]})
    assert out["model"]["url"].endswith("part.glb?download=1")
    assert out["layout"] == "mixed"


def test_bundled_static_glb_is_allowed_but_other_relative_urls_are_not():
    local = pc.normalize({"model": {"url": "/static/models/ev-demo-robot.glb"}})
    unsafe = pc.normalize({"model": {"url": "/private/arbitrary.glb"}})
    assert local["model"]["url"] == "/static/models/ev-demo-robot.glb"
    assert unsafe is None


def test_model_requires_a_real_asset_path_not_a_viewer_hostname():
    assert pc.normalize({"model": {"url": "https://www.gltf"}}) is None
    direct = pc.normalize({"model": {"url": "https://assets.example/robot.glb?download=1"}})
    assert direct["model"]["url"].endswith("robot.glb?download=1")


def test_current_canvas_can_focus_a_stable_image_node_without_researching():
    info_panel.clear()
    try:
        pushed = info_panel.push({
            "title": "图片",
            "images": [
                {"url": "https://example.com/one.jpg"},
                {"url": "https://example.com/two.jpg"},
            ],
        })
        assert set(pushed["document"]["nodes"]) == {"image-1", "image-2"}
        result = info_panel.apply(
            tab_id=pushed["active_tab_id"],
            base_rev=pushed["rev"],
            patches=[
                {"op": "replace", "path": "/view/focus_id", "value": "image-2"},
                {"op": "replace", "path": "/view/zoom", "value": 1.75},
            ],
        )
        assert result["ok"] is True
        assert result["document"]["view"]["focus_id"] == "image-2"
        assert result["document"]["view"]["zoom"] == 1.75
        assert result["affected_paths"] == ["/view/focus_id", "/view/zoom"]

        stale = info_panel.apply(
            tab_id=pushed["active_tab_id"],
            base_rev=pushed["rev"],
            patches=[{"op": "replace", "path": "/view/zoom", "value": 2}],
        )
        assert stale["ok"] is False
        assert stale["error"] == "revision_conflict"
    finally:
        info_panel.clear()


def test_invalid_patch_is_rejected_atomically_instead_of_sanitizing_away_node():
    info_panel.clear()
    try:
        pushed = info_panel.push({
            "title": "图片",
            "images": [{"url": "https://example.com/one.jpg"}],
        })
        result = info_panel.apply(
            tab_id=pushed["active_tab_id"],
            base_rev=pushed["rev"],
            patches=[{
                "op": "replace", "path": "/nodes/image-1",
                "value": {"scale": 2},
            }],
        )
        assert result["ok"] is False
        assert result["error"] == "patch_failed"
        snapshot = info_panel.snapshot()
        assert "image-1" in snapshot["document"]["nodes"]
        assert snapshot["rev"] == pushed["rev"]
    finally:
        info_panel.clear()


def test_canvas_document_stays_simple_but_keeps_real_model_preview():
    price = pc.to_canvas_document(pc.normalize({
        "query": "售价是多少",
        "items": [{"title": "产品 A 299 元", "url": "https://shop.test/a", "snippet": "售价 299 元"}],
    }, kind="search"))
    assert "table-1" not in price["nodes"]
    assert "summary" not in price["nodes"]
    assert price["nodes"]["source-1"]["snippet"] == "售价 299 元"
    assert price["layout"]["type"] == "container"
    assert "mode" not in price["layout"]

    model = pc.to_canvas_document(pc.normalize({
        "query": "显示一个 3D 模型",
        "model": {"url": "/static/models/ev-demo-robot.glb"},
        "items": [{"title": "模型来源", "url": "https://example.com"}],
    }, kind="search"))
    assert model["nodes"]["model-1"]["type"] == "model"
    assert model["layout"]["children"][0]["axis"] == "row"


def test_search_canvas_limits_visible_media_and_sources():
    document = pc.to_canvas_document(pc.normalize({
        "query": "故宫",
        "summary": "原始抓取摘要",
        "images": [
            {"url": "https://example.com/%d.jpg" % index}
            for index in range(5)
        ],
        "items": [
            {"title": "来源%d" % index, "url": "https://example.com/%d" % index}
            for index in range(7)
        ],
    }, kind="search"))
    assert [key for key in document["nodes"] if key.startswith("image-")] == ["image-1", "image-2"]
    assert [key for key in document["nodes"] if key.startswith("source-")] == [
        "source-1", "source-2",
    ]


def _flatten(spec, out=None):
    """把 layout 树摊平成有序 id 列表，用来断言呈现顺序。"""
    out = [] if out is None else out
    if isinstance(spec, str):
        out.append(spec)
    elif isinstance(spec, dict):
        for child in spec.get("children") or []:
            _flatten(child, out)
    return out


_ITEMS = [
    {"title": "U-Arm", "url": "https://a.com/x", "snippet": "400 元遥操"},
    {"title": "六轴 DIY", "url": "https://b.com/y", "snippet": "CSDN 教程"},
    {"title": "lerobot", "url": "https://c.com/z", "snippet": "1350 元"},
]
_IMAGES = [{"url": "https://i.example/%d.jpg" % n, "description": "图 %d" % n} for n in range(6)]


def _document(want, **payload):
    from control_plane import panel_contract

    clean = panel_contract.normalize(
        {"kind": "search", "query": "机械臂", "want": want, **payload}
    )
    return panel_contract.to_canvas_document(clean)


def test_declared_intent_drives_composition_not_a_fixed_template():
    """四类意图各有自己的呈现规格——同一份数据不该摆成同一个样子。

    真实问题：不管用户要图、要清单还是要一句答案，面板永远是
    「摘要 + 两条来源」。那是把请求当模板套，不是按需求组装。
    """
    listing = _document("list", items=_ITEMS, summary="三个能上手的项目")
    order = _flatten(listing["layout"])
    assert order[0] == "summary"
    # 清单的主体是编号条目本身，必须全部列出、且紧跟引子
    assert [i for i in order if i.startswith("source-")] == ["source-1", "source-2", "source-3"]
    assert order[1] == "source-1"

    answer = _document("answer", items=_ITEMS, summary="约 400 元。")
    answer_order = _flatten(answer["layout"])
    assert answer_order[0] == "summary"
    # 一句事实答案只需要一条出处，且压在结论之后
    assert [i for i in answer_order if i.startswith("source-")] == ["source-1"]

    compare = _document("compare", items=_ITEMS, summary="三者取舍")
    compare_order = _flatten(compare["layout"])
    # 要对比就先给并排的表，而不是散文加链接
    assert compare_order[0] == "table-1"
    assert compare["nodes"]["table-1"]["type"] == "table"
    assert len(compare["nodes"]["table-1"]["rows"]) == 3

    images = _document("images", images=_IMAGES, summary="故宫太和殿")
    image_order = _flatten(images["layout"])
    # 要看图：大图打头、其余成阵，不拿链接充数
    assert image_order[0] == "image-1"
    assert len([i for i in image_order if i.startswith("image-")]) == 6
    assert not [i for i in image_order if i.startswith("source-")]
    assert "summary" in image_order
