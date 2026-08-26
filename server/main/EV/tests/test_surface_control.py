import json
from unittest import mock

from tools import surface_control


def test_remote_threejs_model_preview_is_rejected_in_favor_of_research_canvas():
    text, meta = surface_control.execute({
        "action": "create",
        "title": "3D 模型预览",
        "html": "<script type='module'>new GLTFLoader().load('https://x.test/model.glb')</script>",
        "continue_after": False,
        "reply": "好了",
    })
    assert meta["ok"] is False
    assert meta["reason"] == "use_research_canvas"
    assert "研究画布" in text


def test_delete_success_receipt_always_has_terminal_speech():
    with mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=(
            '{"ok": true, "action": "delete"}',
            {
                "ok": True,
                "action": "delete",
                "surface_id": "notes",
                "deleted": True,
            },
        ),
    ):
        text, meta = surface_control.execute({
            "action": "delete",
            "surface_id": "notes",
            "continue_after": False,
        })

    assert meta["speech"] == "窗口已删除"
    assert json.loads(text)["speech"] == "窗口已删除"


def test_structured_page_escapes_content_and_avoids_custom_script():
    with mock.patch.object(surface_control.scene_store, "get", return_value=None), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "open"}),
    ) as execute:
        text, meta = surface_control.execute({
            "action": "show",
            "title": "计划 <script>",
            "summary": "今天 & 明天",
            "sections": [{"heading": "重点", "items": ["A < B"]}],
        })
    payload = execute.call_args.args[0]
    assert payload["surface_id"] == "conversation-canvas"
    assert "&lt;script&gt;" in payload["content"]["html"]
    assert "A &lt; B" in payload["content"]["html"]
    assert payload["content"].get("js") is None
    assert meta["presentation"] == "structured"


def test_geometry_only_show_preserves_existing_content():
    with mock.patch.object(
        surface_control.scene_store,
        "get",
        return_value={"data": {"content": {"type": "html", "html": "old"}}},
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "open"}),
    ) as execute:
        surface_control.execute({
            "action": "show",
            "width": 900,
            "height": 620,
            "x": 140,
            "y": 80,
            "position": "top-left",
        })
    payload = execute.call_args.args[0]
    assert payload["window"] == {
        "width": 900,
        "height": 620,
        "x": 140,
        "y": 80,
        "position": "top-left",
    }
    assert "content" not in payload


def test_status_is_read_only_inspection():
    with mock.patch.object(
        surface_control.surface_tools,
        "surface_inspect_execute",
        return_value=("{}", {
            "ok": True,
            "action": "inspect",
            "surfaces": [{
                "id": "notes",
                "data": {
                    "title": "Notes",
                    "content": {"type": "html", "html": "very large"},
                },
                "visible": True,
                "focused": True,
                "bounds": {"x": 20, "y": 30, "width": 700, "height": 500},
                "renderer_ready": True,
                "content_status": "ready",
            }],
        }),
    ) as inspect:
        text, meta = surface_control.execute({"action": "status", "surface_id": "notes"})
    inspect.assert_called_once_with({"scope": "id", "surface_id": "notes"})
    assert meta["ok"] is True
    assert meta["bounds"] == {"x": 20, "y": 30, "width": 700, "height": 500}
    assert "very large" not in text


def test_status_without_id_resolves_only_current_surface():
    with mock.patch.object(
        surface_control.surface_tools,
        "surface_inspect_execute",
        return_value=("{}", {
            "ok": True,
            "surfaces": [{
                "id": "current-notes",
                "data": {"title": "Notes", "content": {"type": "html"}},
                "bounds": {"x": 10, "y": 12, "width": 600, "height": 420},
            }],
        }),
    ) as inspect:
        _text, meta = surface_control.execute({"action": "status"})
    inspect.assert_called_once_with({"scope": "id", "surface_id": "current"})
    assert meta["surface_id"] == "current-notes"


def test_css_only_update_preserves_existing_html_and_js():
    existing = {
        "data": {
            "content": {
                "type": "html",
                "html": "<main>old</main>",
                "css": "body{color:white}",
                "js": "window.ready=true",
            },
        },
    }
    with mock.patch.object(
        surface_control.scene_store,
        "get",
        return_value=existing,
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "open"}),
    ) as execute:
        surface_control.execute({"action": "show", "css": "body{color:blue}"})
    assert execute.call_args.args[0]["content"] == {
        "html": "<main>old</main>",
        "css": "body{color:blue}",
        "js": "window.ready=true",
    }


def test_css_only_update_rejects_external_url_instead_of_claiming_success():
    existing = {"data": {"content": {"type": "url", "url": "https://example.com"}}}
    with mock.patch.object(
        surface_control.scene_store,
        "get",
        return_value=existing,
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
    ) as execute:
        _text, meta = surface_control.execute({"action": "show", "css": "body{}"})
    assert meta["ok"] is False
    assert meta["reason"] == "url_page_requires_html"
    execute.assert_not_called()


def test_status_timeline_show_reuses_system_surface_without_stealing_focus():
    existing = {"data": {"content": {"type": "url", "url": "http://status"}}}
    with mock.patch.object(
        surface_control.scene_store,
        "get",
        return_value=existing,
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "open"}),
    ) as execute:
        surface_control.execute({
            "action": "show",
            "surface_id": "status-timeline",
            "continue_after": False,
            "reply": "我把状态栏叫回来了。",
        })
    payload = execute.call_args.args[0]
    assert payload == {
        "action": "open",
        "surface_id": "status-timeline",
        "focus": False,
    }


def test_surface_success_prefers_first_round_natural_reply_over_fixed_receipt():
    with mock.patch.object(
        surface_control.scene_store,
        "get",
        return_value={"data": {"content": {"type": "html"}}},
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "open", "speech": "页面已打开"}),
    ):
        text, meta = surface_control.execute({
            "action": "show",
            "summary": "新的页面内容",
            "reply": "我把页面重新收得更简洁了，你看看这一版。",
            "continue_after": False,
        })
    assert meta["direct_reply"] == "我把页面重新收得更简洁了，你看看这一版。"
    assert meta["speech"] == "页面已打开"


def test_voice_tool_schema_separates_create_update_and_hide_target():
    definition = surface_control.tool_definition()
    description = definition["function"]["description"]
    surface_description = definition["function"]["parameters"]["properties"]["surface_id"]["description"]
    actions = definition["function"]["parameters"]["properties"]["action"]["enum"]
    assert "status-timeline" in description
    assert "create 新建独立页" in description
    assert "update 修改现有页" in description
    assert "close 隐藏" in description
    assert "status-timeline" in surface_description
    assert "create" in actions
    assert "update" in actions
    assert "show" not in actions
    assert "theme" in definition["function"]["parameters"]["properties"]


def test_status_timeline_theme_update_changes_palette_without_opening_panel():
    with mock.patch.object(
        surface_control.surface_tools,
        "set_status_timeline_theme",
        return_value={"ok": True, "changed": True, "theme": {"accent": "#77aaff"}},
    ) as update_theme:
        _text, meta = surface_control.execute({
            "action": "update",
            "surface_id": "status-timeline",
            "theme": {"accent": "#77aaff"},
            "continue_after": False,
            "reply": "换成蓝色了",
        })

    update_theme.assert_called_once_with({"accent": "#77aaff"})
    assert meta["ok"] is True
    assert meta["changed"] is True
    assert meta["direct_reply"] == "换成蓝色了"


def test_create_without_id_allocates_new_surface_and_never_reads_focused_page():
    with mock.patch.object(
        surface_control.uuid,
        "uuid4",
        return_value=mock.Mock(hex="1234567890abcdef"),
    ), mock.patch.object(
        surface_control.scene_store,
        "get",
        return_value=None,
    ), mock.patch.object(
        surface_control.scene_store,
        "inspect",
    ) as inspect, mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {
            "ok": True,
            "action": "open",
            "surface_id": "surface-1234567890",
            "title": "记事本",
        }),
    ) as execute:
        _text, meta = surface_control.execute({
            "action": "create",
            "title": "记事本",
            "continue_after": False,
        })
    payload = execute.call_args.args[0]
    assert payload["surface_id"] == "surface-1234567890"
    assert payload["title"] == "记事本"
    assert payload["content"].get("url") is None
    assert "记事本" in payload["content"]["html"]
    assert meta["action"] == "create"
    inspect.assert_not_called()


def test_update_without_id_uses_unique_focused_surface_and_preserves_content():
    existing = {
        "id": "notes",
        "data": {"title": "旧标题", "content": {"type": "html", "html": "old"}},
    }
    with mock.patch.object(
        surface_control.scene_store,
        "inspect",
        return_value={"surfaces": [existing]},
    ), mock.patch.object(
        surface_control.scene_store,
        "get",
        return_value=existing,
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "open", "title": "新标题"}),
    ) as execute:
        _text, meta = surface_control.execute({
            "action": "update",
            "title": "新标题",
            "continue_after": False,
        })
    payload = execute.call_args.args[0]
    assert payload["surface_id"] == "notes"
    assert payload["title"] == "新标题"
    assert "content" not in payload
    assert meta["action"] == "update"


def test_update_without_unique_focus_refuses_to_guess_target():
    with mock.patch.object(
        surface_control.scene_store,
        "inspect",
        return_value={"surfaces": []},
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
    ) as execute:
        _text, meta = surface_control.execute({
            "action": "update",
            "title": "新标题",
            "continue_after": False,
        })
    assert meta["ok"] is False
    assert meta["reason"] == "surface_target_required"
    execute.assert_not_called()


def test_normalize_keeps_script_but_strips_external_loads_and_inline_events():
    """窗口即代码：script 是交互核心必须保留；外部加载与内联事件仍拦截。"""
    from devices.coding.surface_layout import normalize_web_surface_definition

    html = (
        "<!doctype html><html><body>"
        '<canvas id="game"></canvas>'
        '<iframe src="https://evil.example"></iframe>'
        '<button onclick="steal()">x</button>'
        "<script>const c=document.getElementById('game');c.width=400;</script>"
        "</body></html>"
    )
    out = normalize_web_surface_definition(
        {"title": "贪吃蛇", "content": {"html": html}}, current={}
    )
    normalized = out["content"]["html"]
    assert "<script>const c" in normalized
    assert "iframe" not in normalized
    assert "onclick" not in normalized
