# -*- coding: utf-8 -*-
"""稳定窗口 id 复用 + 关闭前 inspect 定位目标的行为测试。

覆盖两处根因修复：
- 打开网站不给 surface_id 时用稳定 id（web-<host>）复用同一窗口，不再堆重复窗口；
- 关闭时按真实场景（url 主机 / 稳定 id）定位并关掉对应窗口，绝不照搬模型猜的 id；
  关一个已经关掉/不存在的窗口算成功，不再谎报「没有收到成功回执」。
"""
import json
from unittest import mock

from tools import surface_control


def test_url_open_uses_stable_id_for_reuse():
    """create 打开网站不给 surface_id → 落到 web-<host> 稳定 id（可复用）。"""
    with mock.patch.object(surface_control.scene_store, "get", return_value=None), \
        mock.patch.object(
            surface_control.surface_tools,
            "surface_manage_execute",
            return_value=("ok", {"ok": True, "action": "open"}),
        ) as manage:
        surface_control.execute({
            "action": "create",
            "url": "https://www.bilibili.com",
            "continue_after": False,
        })
    payload = manage.call_args.args[0]
    assert payload["surface_id"] == "web-bilibili-com"


def test_reopen_existing_url_window_reuses_not_errors():
    """同一网站再次 create → 复用已有窗口（不报 surface_id_exists）。"""
    existing = {"data": {"content": {"type": "url", "url": "https://www.bilibili.com"}}}
    with mock.patch.object(surface_control.scene_store, "get", return_value=existing), \
        mock.patch.object(
            surface_control.surface_tools,
            "surface_manage_execute",
            return_value=("ok", {"ok": True, "action": "open", "title": "哔哩哔哩"}),
        ) as manage:
        text, meta = surface_control.execute({
            "action": "create",
            "url": "https://www.bilibili.com",
            "continue_after": False,
        })
    assert meta["ok"] is True
    assert meta.get("reason") != "surface_id_exists"
    assert manage.call_args.args[0]["surface_id"] == "web-bilibili-com"


def test_close_resolves_targets_from_scene_not_guessed_id():
    """关闭按真实场景定位可见目标并逐个关闭，忽略模型传的无关 id。"""
    with mock.patch.object(
        surface_control, "_close_targets", return_value=(["surface-a", "surface-b"], True)
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "close"}),
    ) as manage:
        text, meta = surface_control.execute({
            "action": "close",
            "surface_id": "surface-garbage",
            "url": "https://www.bilibili.com",
            "continue_after": False,
        })
    closed_ids = [c.args[0]["surface_id"] for c in manage.call_args_list]
    assert closed_ids == ["surface-a", "surface-b"]
    assert meta["ok"] is True
    assert meta["count"] == 2
    assert meta["speech"] == "关闭了2个窗口"


def test_close_already_hidden_is_success_not_failure():
    """目标本来就没开着（或没有这个窗口）→ 算成功，不报没收到回执。"""
    with mock.patch.object(
        surface_control, "_close_targets", return_value=([], True)
    ), mock.patch.object(
        surface_control.surface_tools, "surface_manage_execute"
    ) as manage:
        text, meta = surface_control.execute({
            "action": "close",
            "surface_id": "surface-f8f95d3144",
            "continue_after": False,
        })
    manage.assert_not_called()
    assert meta["ok"] is True
    assert meta["already_closed"] is True
    assert meta["speech"] == "已经关了"


def test_close_single_window_reports_terminal_speech():
    with mock.patch.object(
        surface_control, "_close_targets", return_value=(["web-bilibili-com"], True)
    ), mock.patch.object(
        surface_control.surface_tools,
        "surface_manage_execute",
        return_value=("ok", {"ok": True, "action": "close"}),
    ):
        text, meta = surface_control.execute({
            "action": "close",
            "url": "https://www.bilibili.com",
            "continue_after": False,
        })
    assert meta["ok"] is True
    assert meta["count"] == 1
    assert meta["speech"] == "窗口已关闭"


def test_url_surface_id_normalizes_host():
    assert surface_control._url_surface_id("https://www.bilibili.com/video/x") == "web-bilibili-com"
    assert surface_control._url_surface_id("https://YouTube.com") == "web-youtube-com"
    assert surface_control._url_surface_id("not a url") == ""


def test_pinned_status_bar_can_be_closed_when_named():
    """明确点名常驻窗（状态栏）时必须真的关闭，不能被过滤成「没有可关的窗口」。

    回归：曾把常驻窗从候选里过滤掉 → 匹配为空 → 回「没有开着的页面可关」且
    判成功，窗口却还开着，用户听到「关了」但面板纹丝不动。
    """
    scene = {"surfaces": [
        {"id": "status-timeline", "visible": True, "focused": False,
         "data": {"content": {}}},
        {"id": "web-bilibili-com", "visible": True, "focused": True,
         "data": {"content": {"url": "https://www.bilibili.com"}}},
    ]}
    with mock.patch.object(surface_control.scene_store, "inspect", return_value=scene):
        visible_ids, matched = surface_control._close_targets({}, "status-timeline")
    assert visible_ids == ["status-timeline"]
    assert matched is True


def test_pinned_status_bar_not_picked_when_guessing():
    """没点名、需要猜目标时不得动常驻窗。"""
    scene = {"surfaces": [
        {"id": "status-timeline", "visible": True, "focused": True,
         "data": {"content": {}}},
    ]}
    with mock.patch.object(surface_control.scene_store, "inspect", return_value=scene):
        visible_ids, matched = surface_control._close_targets({}, "")
    assert visible_ids == []


def test_info_board_is_now_status_bar_panel_not_a_window():
    """信息推送已并入状态栏：close/open 走展开收起，不再操作窗口。"""
    from control_plane import info_panel
    info_panel.push({"query": "x", "items": [{"title": "T", "url": "https://a.com"}]})
    with mock.patch.object(surface_control.surface_tools, "set_status_timeline_expanded") as flip:
        _text, meta = surface_control.execute({
            "action": "close", "surface_id": "info-board",
            "continue_after": False, "reply": "收起了",
        })
    assert meta["ok"] is True
    assert meta["expanded"] is False
    flip.assert_called_once_with(False)
    assert info_panel.snapshot()["expanded"] is False


def test_background_refresh_keeps_user_collapse():
    """后台补充结果只换内容，不能把用户刚收起的面板重新顶开。"""
    from control_plane import info_panel
    info_panel.push({"query": "x", "items": [{"title": "A", "url": "https://a.com"}]})
    info_panel.set_expanded(False)          # 用户收起
    info_panel.push({"query": "x", "items": [{"title": "A", "url": "https://a.com"},
                                             {"title": "B", "url": "https://b.com"}]},
                    expand=False)            # 后台补图那一轮
    snap = info_panel.snapshot()
    assert snap["expanded"] is False, "后台刷新不该覆盖用户的收起选择"
    sources = [node for node in snap["document"]["nodes"].values() if node["type"] == "source"]
    assert len(sources) == 2, "内容仍应更新"


def test_status_bar_update_without_geometry_expands_panel():
    """模型常发不带任何参数的 update：也必须理解为展开，否则回「展开了」却没动。"""
    from control_plane import info_panel
    info_panel.push({"query": "x", "items": [{"title": "A", "url": "https://a.com"}]})
    info_panel.set_expanded(False)
    with mock.patch.object(surface_control.surface_tools, "set_status_timeline_expanded") as flip:
        _text, meta = surface_control.execute({
            "action": "update", "surface_id": "status-timeline",
            "continue_after": False, "reply": "展开了",
        })
    assert meta["expanded"] is True
    flip.assert_called_once_with(True)


def test_bare_close_collapses_panel_when_nothing_else_open():
    """没点名的 close：没有普通窗口可关而面板开着时，收面板而不是谎报成功。"""
    from control_plane import info_panel
    info_panel.push({"query": "x", "items": [{"title": "A", "url": "https://a.com"}]})
    with mock.patch.object(surface_control, "_close_targets", return_value=([], False)), \
        mock.patch.object(surface_control.surface_tools, "set_status_timeline_expanded") as flip:
        _text, meta = surface_control.execute({
            "action": "close", "continue_after": False, "reply": "关了",
        })
    assert meta["expanded"] is False
    flip.assert_called_once_with(False)


def test_noop_panel_action_rejects_model_claim():
    """空操作不得采用模型写的话——模型选错目标时也会走到这里。

    实测回归：用户要放大 GitHub 榜单窗口，模型把目标写成 status-timeline，
    面板本就展开着，却回了成功，于是「放大了」这句假回执被播出去。
    """
    from control_plane import info_panel
    info_panel.push({"query": "x", "items": [{"title": "A", "url": "https://a.com"}]})
    # 面板此时已展开，再要求展开 = 空操作
    _text, meta = surface_control.execute({
        "action": "update", "surface_id": "status-timeline",
        "continue_after": False, "reply": "放大了",
    })
    assert meta["changed"] is False
    assert meta.get("direct_reply") is None, "空操作不能沿用模型的话"
    assert meta["speech"] == "信息推送本来就是展开的"
    assert "surface_id" in meta.get("detail", ""), "应提示模型改用真实窗口 id"


def test_real_panel_change_keeps_model_reply():
    """状态真的改变时，模型自己的话照常采用。"""
    from control_plane import info_panel
    info_panel.push({"query": "x", "items": [{"title": "A", "url": "https://a.com"}]})
    with mock.patch.object(surface_control.surface_tools, "set_status_timeline_expanded"):
        _text, meta = surface_control.execute({
            "action": "close", "surface_id": "status-timeline",
            "continue_after": False, "reply": "收起来了",
        })
    assert meta["changed"] is True
    assert meta["direct_reply"] == "收起来了"
