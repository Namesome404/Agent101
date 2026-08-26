import unittest
import tempfile
from pathlib import Path
from unittest import mock

from devices.coding.scene_store import SceneStore


class SceneStoreTest(unittest.TestCase):
    def test_upsert_is_idempotent_and_reuses_id(self):
        store = SceneStore()
        first = store.upsert("coding-studio", kind="coding-studio", data={"phase": "planning"})
        second = store.upsert("coding-studio", kind="coding-studio", data={"phase": "planning"})
        changed = store.upsert("coding-studio", kind="coding-studio", data={"phase": "writing"})
        self.assertTrue(first["changed"])
        self.assertFalse(second["changed"])
        self.assertEqual(changed["rev"], 2)
        self.assertEqual(len(store.snapshot()["surfaces"]), 1)

    def test_patch_and_remove_have_contiguous_revisions(self):
        store = SceneStore()
        events = []
        store.subscribe(events.append)
        store.upsert("blank-board", kind="blank-board", data={})
        store.remove("blank-board")
        self.assertEqual([(event["base"], event["rev"]) for event in events], [(0, 1), (1, 2)])
        self.assertEqual(events[1]["ops"][0], {"op": "remove", "id": "blank-board"})

    def test_runtime_ready_is_a_real_shell_receipt(self):
        store = SceneStore()
        result = store.upsert("site-preview", kind="site-preview", data={"url": "http://127.0.0.1"})
        self.assertFalse(store.wait_surface_ready("site-preview", min_rev=result["rev"], timeout=0))
        store.shell_connected("test-shell")
        store.mark_surface_ready("site-preview", shell_id="test-shell", rev=result["rev"])
        self.assertTrue(store.wait_surface_ready("site-preview", min_rev=result["rev"], timeout=0))

    def test_focused_returns_the_current_surface(self):
        store = SceneStore()
        store.upsert("coding-studio", kind="coding-studio", data={}, focus=True)
        store.upsert("blank-board", kind="blank-board", data={"title": "自定义"}, focus=True)
        self.assertEqual(store.focused()["id"], "blank-board")
        self.assertIsNot(store.get("coding-studio").get("focus"), True)

    def test_created_closed_opened_and_deleted_are_distinct(self):
        store = SceneStore()
        created = store.upsert("custom", kind="web-surface", data={}, visible=False)
        self.assertFalse(created["surface"]["visible"])
        self.assertEqual(len(store.inspect(scope="all")["surfaces"]), 1)
        self.assertEqual(store.inspect(scope="visible")["surfaces"], [])

        opened = store.set_visible("custom", True, focus=True)
        self.assertTrue(opened["surface"]["visible"])
        self.assertEqual(store.focused()["id"], "custom")

        closed = store.set_visible("custom", False)
        self.assertFalse(closed["surface"]["visible"])
        self.assertIsNone(store.focused())
        self.assertIsNotNone(store.get("custom"))

        store.remove("custom")
        self.assertIsNone(store.get("custom"))

    def test_inspect_uses_desktop_shell_runtime_receipt(self):
        store = SceneStore()
        result = store.upsert("custom", kind="web-surface", data={}, visible=True, focus=True)
        store.mark_surface_ready(
            "custom",
            shell_id="shell-1",
            rev=result["rev"],
            visible=True,
            focused=True,
            bounds={"x": 10, "y": 20, "width": 700, "height": 500},
        )
        inspected = store.inspect(scope="visible")["surfaces"][0]
        self.assertTrue(inspected["renderer_ready"])
        self.assertEqual(inspected["bounds"]["width"], 700)

    def test_request_show_reasserts_visible_state_without_stealing_focus(self):
        store = SceneStore()
        first = store.upsert("status", kind="web-surface", data={}, visible=True)
        shown = store.request_show("status", focus=False)
        self.assertTrue(shown["changed"])
        self.assertGreater(shown["rev"], first["rev"])
        self.assertTrue(shown["surface"]["visible"])
        self.assertIsNot(shown["surface"].get("focus"), True)

    def test_request_show_preserves_unchanged_content_receipt(self):
        store = SceneStore()
        first = store.upsert(
            "status", kind="web-surface",
            data={"content": {"type": "url", "url": "http://127.0.0.1/status"}},
            visible=True,
        )
        store.mark_surface_ready(
            "status", shell_id="shell-1", rev=first["rev"], visible=True,
        )
        store.mark_content_status("status", shell_id="shell-1", status="ready")
        shown = store.request_show("status", focus=False)
        self.assertEqual(store.wait_content_result("status", timeout=0), "ready")
        store.mark_surface_ready(
            "status", shell_id="shell-1", rev=shown["rev"], visible=True,
        )
        inspected = store.inspect(scope="id", surface_id="status")["surfaces"][0]
        self.assertEqual(inspected["content_status"], "ready")

    def test_inspect_exposes_inner_content_receipt(self):
        store = SceneStore()
        store.upsert("web", kind="web-surface", data={"content": {"type": "url"}}, visible=True)
        store.mark_content_status(
            "web", shell_id="shell-1", status="error",
            url="https://example.invalid", error="ERR_BLOCKED_BY_RESPONSE",
        )
        inspected = store.inspect(scope="id", surface_id="web")["surfaces"][0]
        self.assertEqual(inspected["content_status"], "error")
        self.assertIn("BLOCKED", inspected["content_error"])
        self.assertEqual(store.wait_content_result("web", timeout=0), "error")

    def test_inspect_exposes_measured_content_size(self):
        store = SceneStore()
        store.upsert("doc", kind="web-surface", data={"content": {"type": "text"}}, visible=True)
        store.mark_content_size("doc", shell_id="shell-1", width=640, height=980)
        inspected = store.inspect(scope="id", surface_id="doc")["surfaces"][0]
        self.assertEqual(inspected["content_size"], {"width": 640, "height": 980})

    def test_content_size_ignored_for_unknown_surface(self):
        store = SceneStore()
        store.mark_content_size("ghost", shell_id="shell-1", width=100, height=100)
        self.assertEqual(store.inspect(scope="all")["surfaces"], [])

    def test_surface_definitions_survive_core_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "scene.json"
            first = SceneStore(state_path=state_path)
            first.upsert(
                "timer", kind="web-surface",
                data={"content": {"type": "app", "html": "<button>开始</button>"}},
                visible=True, focus=True,
            )
            restored = SceneStore(state_path=state_path)
            self.assertEqual(restored.focused()["id"], "timer")
            self.assertIn("开始", restored.get("timer")["data"]["content"]["html"])

    def test_show_sequence_survives_restart_and_still_reasserts(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "scene.json"
            first = SceneStore(state_path=state_path)
            first.upsert("status", kind="web-surface", data={}, visible=True)
            shown = first.request_show("status", focus=False)
            restored = SceneStore(state_path=state_path)
            repeated = restored.request_show("status", focus=False)
            self.assertTrue(repeated["changed"])
            self.assertGreater(repeated["rev"], shown["rev"])

    def test_eviction_keeps_visible_focused_and_pinned(self):
        with mock.patch(
            "devices.coding.scene_store.SURFACE_LIMIT", 3
        ):
            store = SceneStore()
            # 常驻窗（pinned）与可见/聚焦窗不被淘汰
            store.upsert("status-timeline", kind="web-surface", data={"title": "EV"}, visible=True)
            store.upsert("web-youtube", kind="web-surface", data={"title": "Youtube"}, visible=True, focus=True)
            # 历史残留：不可见、非常驻，会被优先淘汰到只剩上限内
            for i in range(5):
                store.upsert("old-%d" % i, kind="web-surface", data={"title": "旧%d" % i}, visible=False)
            ids = [s["id"] for s in store.inspect(scope="all")["surfaces"]]
            self.assertIn("status-timeline", ids)
            self.assertIn("web-youtube", ids)
            self.assertLessEqual(len(ids), 3)
            # 淘汰的一定是最早插入的不可见残留
            self.assertNotIn("old-0", ids)
            self.assertNotIn("old-1", ids)

    def test_user_drag_writeback_updates_declared_geometry(self):
        store = SceneStore()
        store.upsert(
            "web-doc", kind="web-surface",
            data={"window": {"width": 620, "height": 480, "x": 40, "y": 60}},
            visible=True,
        )
        store.mark_surface_ready(
            "web-doc",
            shell_id="shell-1",
            visible=True,
            bounds={"x": 320, "y": 280, "width": 620, "height": 480},
        )
        surface = store.get("web-doc")
        window = surface["data"]["window"]
        self.assertEqual(window["x"], 320)
        self.assertEqual(window["y"], 280)
        # 尺寸不是用户拖动的范畴，不被回写覆盖
        self.assertEqual(window["width"], 620)
        self.assertEqual(window["height"], 480)

    def test_dragged_geometry_survives_content_update(self):
        """用户拖到新位置后，模型再 set 内容不应把窗口弹回旧坐标。

        模型更新内容时 surface_manage 会把当前声明几何合并进新定义；
        回写后的 x/y 已成为声明几何，因此内容更新后窗口仍在拖动后的位置。
        """
        store = SceneStore()
        store.upsert(
            "web-doc", kind="web-surface",
            data={"window": {"width": 620, "height": 480, "x": 40, "y": 60}},
            visible=True,
        )
        store.mark_surface_ready(
            "web-doc", shell_id="shell-1", visible=True,
            bounds={"x": 400, "y": 300, "width": 620, "height": 480},
        )
        # 模拟 surface_manage set 的合并语义：新定义携带当前 window（含回写后的 x/y）
        current = store.get("web-doc")["data"]
        merged = {
            **current,
            "content": {"type": "text", "text": "新版内容"},
        }
        store.upsert("web-doc", kind="web-surface", data=merged, visible=True)
        window = store.get("web-doc")["data"]["window"]
        self.assertEqual(window["x"], 400)
        self.assertEqual(window["y"], 300)


if __name__ == "__main__":
    unittest.main()
