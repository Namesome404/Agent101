# -*- coding: utf-8 -*-
"""窗口不遮挡自动定位算法单测。

find_free_position 必须在纯本地几何上微秒级给出不遮挡位置；
auto_place_window 只在模型没给坐标时自动填 x/y，给了就不改。
"""
from contextlib import ExitStack
import unittest
from unittest import mock

from devices.coding import surfaces
from devices.coding.scene_store import SceneStore


def _patch_surface_store(store):
    """三个 surface 子模块共享同一 scene_store，需全部替换为测试实例。"""
    from devices.coding import surface_hints, surface_layout, surface_tools
    stack = ExitStack()
    for target in (surface_layout, surface_tools, surface_hints):
        stack.enter_context(mock.patch.object(target, "scene_store", store))
    return stack


def _overlap(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return not (ax + aw <= bx or bx + bw <= ax or ay + ah <= by or by + bh <= ay)


class FindFreePositionTest(unittest.TestCase):
    def test_no_existing_window_places_top_left(self):
        x, y = surfaces.find_free_position([], 520, 380)
        self.assertEqual((x, y), (0, 0))

    def test_avoids_single_window(self):
        # 一个窗口占左上角，新窗口应放到它的右边（不重叠）
        existing = [(0, 0, 520, 380)]
        x, y = surfaces.find_free_position(existing, 520, 380)
        self.assertFalse(_overlap((x, y, 520, 380), existing[0]))
        self.assertGreaterEqual(x, 520)

    def test_avoids_stacked_windows(self):
        # 多个窗口并排占第一行时，新窗口应落在一个不与任何窗口重叠的位置
        # （可能在同行右侧空隙，也可能换行——正确性只要求不重叠）
        existing = [(0, 0, 400, 300), (424, 0, 400, 300), (848, 0, 400, 300)]
        x, y = surfaces.find_free_position(existing, 400, 300)
        for rect in existing:
            self.assertFalse(_overlap((x, y, 400, 300), rect),
                             "new window overlaps %r" % (rect,))
        self.assertGreaterEqual(y, 0)

    def test_falls_back_when_full(self):
        # 整个 viewport 被占满时回退右上角，不允许越界
        existing = [(0, 0, 1900, 1040)]
        x, y = surfaces.find_free_position(existing, 500, 400, viewport=(1920, 1040))
        self.assertTrue(0 <= x and x + 500 <= 1920)
        self.assertTrue(0 <= y and y + 400 <= 1040)

    def test_respects_explicit_geometry_viewport_clamp(self):
        # 尺寸超过 viewport 被 clamp，不产生越界窗口
        x, y = surfaces.find_free_position([], 4000, 3000, viewport=(1920, 1040))
        self.assertEqual((x, y), (0, 0))


class AutoPlaceWindowTest(unittest.TestCase):
    def test_fills_coordinates_when_missing(self):
        data = {"window": {"width": 520, "height": 380}, "title": "t"}
        out = surfaces.auto_place_window(data)
        self.assertIn("x", out["window"])
        self.assertIn("y", out["window"])
        self.assertEqual(out["window"]["width"], 520)
        self.assertEqual(out["window"]["height"], 380)

    def test_keeps_explicit_coordinates(self):
        data = {"window": {"width": 520, "height": 380, "x": 100, "y": 200}}
        out = surfaces.auto_place_window(data)
        self.assertEqual(out["window"]["x"], 100)
        self.assertEqual(out["window"]["y"], 200)

    def test_does_not_mutate_input(self):
        data = {"window": {"width": 520, "height": 380}}
        surfaces.auto_place_window(data)
        self.assertNotIn("x", data["window"])

    def test_open_without_coordinates_gets_placed(self):
        # 模拟真实 open：无 surface_id → blank-board 自动创建，坐标被自动填上
        scene_store = SceneStore()
        with _patch_surface_store(scene_store):
            text, meta = surfaces.surface_manage_execute(
                {"action": "open", "content": {"html": "<p>hi</p>"}}
            )
            stored = scene_store.get("blank-board")
            self.assertIsNotNone(stored)
            win = (stored.get("data") or {}).get("window") or {}
            self.assertIn("x", win)
            self.assertIn("y", win)

    def test_open_existing_window_with_html_replaces_url(self):
        # 回归：已有 youtube(url) 窗口，模型 open 时给新 html 内容，
        # 必须把 content 换成 html 渲染，而不是保留旧 url（曾显示「标题番茄钟、内容仍是 youtube」）。
        scene_store = SceneStore()
        with _patch_surface_store(scene_store):
            surfaces.surface_manage_execute(
                {"action": "open", "surface_id": "youtube",
                 "content": {"url": "https://www.youtube.com"}}
            )
            surfaces.surface_manage_execute(
                {"action": "open", "surface_id": "youtube",
                 "definition": {"title": "番茄钟", "content": {"html": "<div id='app'>45:00</div>"}}}
            )
            stored = scene_store.get("youtube")
            data = (stored or {}).get("data") or {}
            content = data.get("content") or {}
            self.assertEqual(content.get("type"), "html")
            self.assertIn("<div id='app'>", content.get("html", ""))
            self.assertNotIn("url", content)
            self.assertEqual((data.get("title") or "")[:3], "番茄钟")

    def test_open_without_id_does_not_use_focused_window(self):
        # 回归：当前聚焦的是已有 youtube 窗口，用户 open 新内容（不带 surface_id）
        # 必须新建 blank-board，绝不能改掉 youtube。
        scene_store = SceneStore()
        scene_store.upsert("youtube", kind="web-surface", data={
            "title": "youtube", "content": {"url": "https://www.youtube.com", "type": "url"},
            "window": {"width": 520, "height": 380},
        }, visible=True, focus=True)
        with _patch_surface_store(scene_store):
            surfaces.surface_manage_execute(
                {"action": "open", "content": {"html": "<p>番茄钟</p>"}}
            )
            youtube = scene_store.get("youtube")
            self.assertEqual((youtube.get("data") or {}).get("content", {}).get("url"),
                             "https://www.youtube.com")
            blank = scene_store.get("blank-board")
            self.assertIsNotNone(blank)
            self.assertIn("<p>番茄钟</p>", (blank.get("data") or {}).get("content", {}).get("html", ""))


class SurfaceAppendTest(unittest.TestCase):
    def test_append_with_text(self):
        # 基本 append：text 进 /content/items
        scene_store = SceneStore()
        scene_store.upsert("notes", kind="web-surface", data={
            "title": "记录", "content": {"type": "html", "html": "<h2>记录</h2>"},
        }, visible=True)
        with _patch_surface_store(scene_store):
            text, meta = surfaces.surface_manage_execute(
                {"action": "append", "surface_id": "notes", "text": "明天下午三点开会"})
            stored = scene_store.get("notes")
            self.assertTrue(meta.get("changed"), text)
            self.assertEqual((stored.get("data") or {}).get("content", {}).get("items"),
                             ["明天下午三点开会"])

    def test_append_with_patches(self):
        # 回归：模型常把追加写成 RFC6902 patches（append 曾无视 patches，
        # 判 no_content_change 导致"记下了"但没写入）。
        scene_store = SceneStore()
        scene_store.upsert("notes", kind="web-surface", data={
            "title": "记录", "content": {"type": "html", "html": "<h2>记录</h2>"},
        }, visible=True)
        with _patch_surface_store(scene_store):
            text, meta = surfaces.surface_manage_execute(
                {"action": "append", "surface_id": "notes",
                 "patches": [{"op": "add", "path": "/content/items/-", "value": "明天下午三点开会"}]})
            stored = scene_store.get("notes")
            self.assertTrue(meta.get("changed"), text)
            self.assertEqual((stored.get("data") or {}).get("content", {}).get("items"),
                             ["明天下午三点开会"])

    def test_append_patches_then_text(self):
        # patches 与 text 都提供时先应用 patches，再追加 text
        scene_store = SceneStore()
        scene_store.upsert("notes", kind="web-surface", data={
            "title": "记录", "content": {"type": "html", "html": "<h2>记录</h2>"},
        }, visible=True)
        with _patch_surface_store(scene_store):
            text, meta = surfaces.surface_manage_execute(
                {"action": "append", "surface_id": "notes",
                 "patches": [{"op": "add", "path": "/content/items/-", "value": "a"}],
                 "text": "b"})
            stored = scene_store.get("notes")
            self.assertEqual((stored.get("data") or {}).get("content", {}).get("items"), ["a", "b"])

    def test_append_patch_without_dash_appends(self):
        # 回归：模型用 add /content/items（不带 /-）追加时也应追加，而非替换数组
        scene_store = SceneStore()
        scene_store.upsert("notes", kind="web-surface", data={
            "title": "记录", "content": {"type": "html", "html": "<h2>记录</h2>"},
        }, visible=True)
        with _patch_surface_store(scene_store):
            text, meta = surfaces.surface_manage_execute(
                {"action": "append", "surface_id": "notes",
                 "patches": [{"op": "add", "path": "/content/items", "value": "明天下午三点开会"}]})
            stored = scene_store.get("notes")
            self.assertTrue(meta.get("changed"), text)
            self.assertEqual((stored.get("data") or {}).get("content", {}).get("items"),
                             ["明天下午三点开会"])
            # 再追加一条，保证不是替换
            surfaces.surface_manage_execute(
                {"action": "append", "surface_id": "notes",
                 "patches": [{"op": "add", "path": "/content/items", "value": "后天去取快递"}]})
            stored = scene_store.get("notes")
            self.assertEqual((stored.get("data") or {}).get("content", {}).get("items"),
                             ["明天下午三点开会", "后天去取快递"])

    def test_append_without_content_errors_clearly(self):
        # 回归：append 只带 path 没带 text/items/patches 时，必须明确报错
        # （曾静默判 no_content_change，模型以为自己记录了实际没写入）
        scene_store = SceneStore()
        scene_store.upsert("notes", kind="web-surface", data={
            "title": "记录", "content": {"type": "html", "html": "<h2>记录</h2>"},
        }, visible=True)
        with _patch_surface_store(scene_store):
            text, meta = surfaces.surface_manage_execute(
                {"action": "append", "surface_id": "notes", "path": "/content/items"})
            self.assertFalse(meta.get("ok"))
            self.assertEqual(meta.get("reason"), "append_missing_content")


if __name__ == "__main__":
    unittest.main()
