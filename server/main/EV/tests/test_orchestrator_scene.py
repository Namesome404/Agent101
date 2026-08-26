import unittest
from unittest import mock

from devices.coding import orchestrator
from devices.coding.scene_store import SceneStore


class WorkAgentSurfaceTest(unittest.TestCase):
    def setUp(self):
        self.scene_store = SceneStore()
        self.scene_patch = mock.patch.object(orchestrator, "scene_store", self.scene_store)
        self.scene_patch.start()

    def tearDown(self):
        self.scene_patch.stop()

    def test_agent_activity_uses_dedicated_compact_hud(self):
        self.scene_store.upsert(
            "custom-claude-view",
            kind="web-surface",
            data={
                "title": "我的运行窗",
                "window": {"width": 700, "height": 500},
                "content": {
                    "type": "stream",
                    "source": {"type": "claude-code", "channel": "active"},
                },
            },
            visible=True,
        )

        orchestrator.push_studio(
            987654,
            status="编写中",
            detail="正在修改文件",
            phase="writing",
            log=["Read app.py", "Edit app.py"],
        )

        custom = self.scene_store.get("custom-claude-view")
        self.assertEqual(custom["data"]["title"], "我的运行窗")
        surface = self.scene_store.get("work-hud")
        self.assertEqual(surface["data"]["window"]["width"], 152)
        self.assertEqual(surface["data"]["window"]["height"], 224)
        self.assertEqual(surface["data"]["app"]["id"], "agent-work")
        self.assertTrue(surface["visible"])
        self.assertIsNone(self.scene_store.get("coding-studio"))

    def test_awaiting_confirmation_does_not_open_a_second_page(self):
        orchestrator.push_studio(
            987655, status="待确认计划", phase="awaiting_confirm",
            plan_steps=["先检查", "再实现"],
        )
        surface = self.scene_store.get("work-hud")
        self.assertFalse(surface["visible"])
        self.assertIsNone(self.scene_store.get("coding-studio"))

    def test_preview_url_changes_revision_to_force_reload(self):
        url = "http://127.0.0.1:8002/api/skills/claude-code/preview/demo/index.html"
        orchestrator.ensure_preview_window(987656, url=url, path="index.html")
        first = self.scene_store.get("site-preview")["data"]["content"]["url"]
        orchestrator.ensure_preview_window(987656, url=url, path="index.html")
        second = self.scene_store.get("site-preview")["data"]["content"]["url"]
        self.assertNotEqual(first, second)
        self.assertIn("__ev_rev=", second)


if __name__ == "__main__":
    unittest.main()
