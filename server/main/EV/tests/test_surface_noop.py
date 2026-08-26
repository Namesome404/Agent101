import unittest
from unittest import mock

from devices.coding import surfaces
from devices.coding.scene_store import SceneStore


def _patch_surface_store(store):
    """三个 surface 子模块共享同一 scene_store，需全部替换为测试实例。"""
    from devices.coding import surface_hints, surface_layout, surface_tools
    patchers = [
        mock.patch.object(surface_layout, "scene_store", store),
        mock.patch.object(surface_tools, "scene_store", store),
        mock.patch.object(surface_hints, "scene_store", store),
    ]
    for p in patchers:
        p.start()
    return patchers


def _stop_surface_store(patchers):
    for p in reversed(patchers):
        p.stop()


class SurfaceNoOpReceiptTest(unittest.TestCase):
    """surface_manage 空操作必须如实回执：内容与当前一致时 changed=false、ok=false，
    不能让模型误以为"重推了"。"""

    def setUp(self):
        self.scene_store = SceneStore()
        self.patch = _patch_surface_store(self.scene_store)
        # 先走一次 normalize，模拟真实 create 已规范化的存储
        initial = surfaces.normalize_web_surface_definition(
            {
                "title": "空窗口",
                "content": {
                    "html": "<canvas id='g'></canvas>",
                    "css": "body{}",
                    "js": "console.log(1)",
                },
            },
            current={},
        )
        self.scene_store.upsert(
            "blank-1",
            kind="web-surface",
            data=initial,
            visible=True,
            focus=True,
        )

    def tearDown(self):
        _stop_surface_store(self.patch)

    def test_repeat_set_same_content_returns_no_op(self):
        same = {
            "html": "<canvas id='g'></canvas>",
            "css": "body{}",
            "js": "console.log(1)",
        }
        text, meta = surfaces.surface_manage_execute(
            {"action": "set", "surface_id": "blank-1", "content": same}
        )
        self.assertFalse(meta["ok"])
        self.assertFalse(meta["changed"])
        self.assertEqual(meta["reason"], "no_content_change")

    def test_real_change_reports_changed_true(self):
        text, meta = surfaces.surface_manage_execute(
            {
                "action": "set",
                "surface_id": "blank-1",
                "content": {
                    "html": "<canvas id='g'></canvas><button>开始</button>",
                    "css": "body{}",
                    "js": "console.log(2)",
                },
            }
        )
        self.assertTrue(meta["changed"])
        self.assertIsInstance(meta["ok"], bool)

    def test_open_is_not_a_no_op_even_when_already_visible(self):
        # open 已有窗口是幂等操作，不应因为"没变"而判失败
        text, meta = surfaces.surface_manage_execute(
            {"action": "open", "surface_id": "blank-1"}
        )
        self.assertIn("changed", meta)
        self.assertTrue(meta["visible"])


class OpenRequiresDefinitionTest(unittest.TestCase):
    """open 一个不存在的窗口必须带内容（url/html），否则拒绝而不是静默建空窗。

    曾出现幻觉：模型只传 {"action":"open","surface_id":"deepseek"}，系统返回 ok:true
    但窗口是空的，模型据此谎报「开了」。"""

    def setUp(self):
        self.scene_store = SceneStore()
        self.patch = _patch_surface_store(self.scene_store)

    def tearDown(self):
        _stop_surface_store(self.patch)

    def test_open_missing_id_without_definition_rejected(self):
        text, meta = surfaces.surface_manage_execute(
            {"action": "open", "surface_id": "deepseek"}
        )
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["reason"], "open_requires_definition")
        self.assertIn("content.url", str(meta.get("detail") or ""))
        # 不应创建任何窗口
        surfaces_list = self.scene_store.inspect(scope="all").get("surfaces") or []
        self.assertEqual(len(surfaces_list), 0)

    def test_open_missing_id_with_url_creates_url_surface(self):
        text, meta = surfaces.surface_manage_execute(
            {
                "action": "open",
                "surface_id": "deepseek",
                "definition": {
                    "content": {"type": "url", "url": "https://www.deepseek.com/"},
                },
            }
        )
        entry = self.scene_store.get("deepseek")
        self.assertIsNotNone(entry)
        self.assertEqual((entry.get("data") or {}).get("content", {}).get("url"),
                         "https://www.deepseek.com/")
        self.assertTrue(entry.get("visible"))

        # 测试环境无真实桌面壳，content_status 可能非 ready，但窗口必须已创建显示
        self.assertIn("deepseek", str(meta.get("surface_id") or ""))

    def test_open_existing_id_with_definition_updates_content(self):
        initial = surfaces.normalize_web_surface_definition(
            {"title": "旧", "content": {"html": "<p>old</p>"}}, current={}
        )
        self.scene_store.upsert("note", kind="web-surface", data=initial, visible=False)
        text, meta = surfaces.surface_manage_execute(
            {
                "action": "open",
                "surface_id": "note",
                "definition": {
                    "content": {"type": "url", "url": "https://example.com/"},
                },
            }
        )
        entry = self.scene_store.get("note")
        self.assertEqual((entry.get("data") or {}).get("content", {}).get("url"),
                         "https://example.com/")
        self.assertTrue(entry.get("visible"))


class DeleteRequiresExistingSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.scene_store = SceneStore()
        self.patch = _patch_surface_store(self.scene_store)

    def tearDown(self):
        _stop_surface_store(self.patch)

    def test_delete_missing_surface_is_failed_receipt(self):
        _text, meta = surfaces.surface_manage_execute({
            "action": "delete",
            "surface_id": "missing-surface",
        })
        self.assertFalse(meta["ok"])
        self.assertFalse(meta["deleted"])
        self.assertEqual(meta["reason"], "surface_not_found")

    def test_delete_existing_surface_reports_real_change(self):
        self.scene_store.upsert(
            "temporary",
            kind="web-surface",
            data={"content": {"html": "<p>test</p>"}},
            visible=True,
        )
        _text, meta = surfaces.surface_manage_execute({
            "action": "delete",
            "surface_id": "temporary",
        })
        self.assertTrue(meta["ok"])
        self.assertTrue(meta["deleted"])
        self.assertIsNone(self.scene_store.get("temporary"))


class StatusTimelineVisibilityTest(unittest.TestCase):
    """状态栏可语音隐藏和叫回，但仍是不可删除的系统 surface。"""

    def setUp(self):
        self.scene_store = SceneStore()
        self.patch = _patch_surface_store(self.scene_store)
        initial = surfaces.normalize_web_surface_definition(
            {"title": "EV", "window": {"compact": True, "height": 184}},
            current={},
        )
        self.scene_store.upsert(
            "status-timeline",
            kind="web-surface",
            data=initial,
            visible=True,
            focus=False,
        )

    def tearDown(self):
        _stop_surface_store(self.patch)

    def test_close_hides_status_timeline_instead_of_rejecting_it_as_pinned(self):
        with mock.patch.object(
            self.scene_store,
            "wait_surface_ready",
            return_value=True,
        ):
            text, meta = surfaces.surface_manage_execute({
                "action": "close",
                "surface_id": "status-timeline",
            })
        self.assertTrue(meta["ok"])
        self.assertFalse(self.scene_store.get("status-timeline")["visible"])
        self.assertEqual(meta["speech"], "状态栏已隐藏")

    def test_delete_still_rejects_status_timeline(self):
        text, meta = surfaces.surface_manage_execute({
            "action": "delete",
            "surface_id": "status-timeline",
        })
        self.assertFalse(meta["ok"])
        self.assertEqual(meta["reason"], "pinned_surface")
        self.assertIsNotNone(self.scene_store.get("status-timeline"))


if __name__ == "__main__":
    unittest.main()
