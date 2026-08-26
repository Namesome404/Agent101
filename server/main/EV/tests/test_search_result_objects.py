# -*- coding: utf-8 -*-
"""搜索结果作为可打开对象：模型引用 result.N，永远不自己写 URL。

事故复盘：搜索判定弱证据时，系统会（有意且有测试锁定地）把 URL 从模型上下文
里扣掉。用户接着说「把那个链接打开」，模型手里没有真 URL，于是凭记忆编了一个
B 站 BV 号——放出来是 rickroll。这里锁住修复后的性质。
"""
from unittest import mock

from control_plane import search_results
from control_plane.object_registry import object_registry
from tools import search_objects


def _seed():
    search_results.remember("4舵机机械臂", [
        {"title": "4个MG90S小机械臂模型", "url": "https://www.bilibili.com/video/BVreal",
         "snippet": "4舵机方案"},
        {"title": "CSDN 四轴机械臂", "url": "https://blog.csdn.net/x"},
        {"title": "没有链接的条目", "url": ""},
    ])
    search_objects.ensure_provider()


def test_only_openable_results_are_remembered():
    _seed()
    assert search_results.count() == 2, "没有 http(s) 链接的条目不该进引用表"


def test_results_are_exposed_as_objects_without_leaking_urls():
    """模型能看到标题与来源，但看不到完整 URL——它因此无法凭记忆改写链接。"""
    _seed()
    objects = search_objects._discover()
    assert [o["target_id"] for o in objects] == ["result.1", "result.2"]
    assert objects[0]["commands"] == ["open"]
    blob = str(objects)
    assert "bilibili.com/video/BVreal" not in blob
    assert "4个MG90S小机械臂模型" in blob


def test_open_uses_the_real_url_from_server_side():
    """invoke result.1 open → 用服务端保存的真实 URL 开窗。"""
    _seed()
    with mock.patch("tools.surface_control.execute",
                    return_value=("ok", {"ok": True, "surface_id": "web-bilibili-com"})) as opener:
        out = search_objects._execute("invoke", "result.1", {"command": "open"}, {})
    assert out["ok"] is True
    assert out["opened_url"] == "https://www.bilibili.com/video/BVreal"
    assert opener.call_args.args[0]["url"] == "https://www.bilibili.com/video/BVreal"


def test_out_of_range_reference_fails_loudly():
    """越界引用必须失败，不能悄悄开别的东西。"""
    _seed()
    out = search_objects._execute("invoke", "result.9", {"command": "open"}, {})
    assert out["ok"] is False
    assert out["reason"] == "result_not_found"


def test_provider_is_additive_and_routes_only_result_prefix():
    """纯新增 provider：不接管别的 target。"""
    _seed()
    out = search_objects._execute("invoke", "surface.new", {"command": "create"}, {})
    assert out["ok"] is False
    assert out["reason"] == "unsupported_target"


def test_results_visible_through_the_registry():
    _seed()
    found = object_registry.inspect(target="result.1")
    assert found.get("ok") is True
