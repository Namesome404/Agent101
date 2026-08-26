# -*- coding: utf-8 -*-
"""语音搜索：单次稳定快照、答案就绪后一次提交。"""
import unittest
from unittest import mock

import app as app_module
from control_plane import info_panel


class WebSearchBackgroundTest(unittest.TestCase):
    """兼容函数名保留，但 voice 不再后台二次覆盖画布。"""

    def setUp(self):
        self._saved_cache = app_module._REALTIME_CACHE
        app_module._REALTIME_CACHE = {}

    def tearDown(self):
        app_module._REALTIME_CACHE = self._saved_cache

    def _fake_result(self, profile):
        return {
            "ok": True,
            "profile": profile,
            "items": [{"url": "http://x/%s" % profile}],
            "answer_context": "【联网搜索结果】查询：测试\n来源：1. 测试标题",
        }

    def test_voice_mode_uses_fast_profile(self):
        with mock.patch.object(
            app_module, "_run_web_search_tool",
            side_effect=lambda q, profile="full", **kwargs: self._fake_result(profile),
        ):
            result = app_module._web_search_with_background(
                "今天北京天气", voice_mode=True
            )
        self.assertEqual(result["profile"], "voice")

    def test_non_voice_mode_uses_full_profile(self):
        with mock.patch.object(
            app_module, "_run_web_search_tool",
            side_effect=lambda q, profile="full", **kwargs: self._fake_result(profile),
        ):
            result = app_module._web_search_with_background("今天北京天气")
        self.assertEqual(result["profile"], "full")

    def test_cache_hit_skips_search(self):
        key = "web_search:quick:" + app_module._web_search_cache_key("今天北京天气")
        cached_full = {"ok": True, "profile": "voice", "cached": True}
        app_module._realtime_cache_put(key, cached_full)
        calls = []
        with mock.patch.object(
            app_module, "_run_web_search_tool",
            side_effect=lambda q, profile="full", **kwargs: calls.append(profile) or self._fake_result(profile),
        ):
            result = app_module._web_search_with_background(
                "今天北京天气", voice_mode=True
            )
        self.assertEqual(result.get("cached"), True)
        self.assertEqual(calls, [])

    def test_cache_miss_stores_the_same_stable_snapshot(self):
        calls = []
        with mock.patch.object(
            app_module, "_run_web_search_tool",
            side_effect=lambda q, profile="full", **kwargs: calls.append(profile) or self._fake_result(profile),
        ):
            result = app_module._web_search_with_background(
                "今天北京天气", voice_mode=True
            )
        self.assertEqual(result["profile"], "voice")
        key = "web_search:quick:" + app_module._web_search_cache_key("今天北京天气")
        cached = app_module._realtime_cache_get(key, 300)
        self.assertIsNotNone(cached)
        self.assertEqual(cached["profile"], "voice")
        self.assertEqual(calls, ["voice"])

    def test_thorough_voice_search_is_one_full_transaction(self):
        calls = []
        with mock.patch.object(
            app_module, "_run_web_search_tool",
            side_effect=lambda q, profile="full", **kwargs: (
                calls.append((profile, kwargs)) or self._fake_result(profile)
            ),
        ):
            result = app_module._web_search_with_background(
                "找一块小众成品板",
                voice_mode=True,
                research_depth="thorough",
                search_queries=["型号", "购买链接"],
                include_visuals=True,
            )
        self.assertEqual(result["profile"], "full")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "full")
        self.assertEqual(calls[0][1]["search_queries"], ["型号", "购买链接"])
        self.assertTrue(calls[0][1]["include_visuals"])

    def test_cache_key_normalization(self):
        self.assertEqual(
            app_module._web_search_cache_key("请帮我搜一下 今天 北京的天气"),
            app_module._web_search_cache_key("今天北京的天气"),
        )


class ExecuteChatToolVoiceModeTest(unittest.TestCase):
    """_execute_chat_tool 的 voice_mode 决定 web_search profile。"""

    def setUp(self):
        self._saved_cache = app_module._REALTIME_CACHE
        app_module._REALTIME_CACHE = {}

    def tearDown(self):
        app_module._REALTIME_CACHE = self._saved_cache

    def _search(self, q, profile="full", **kwargs):
        return {
            "ok": True,
            "profile": profile,
            "items": [{"url": "http://x/%s" % profile}],
            "answer_context": "【联网搜索结果】查询：%s" % q,
        }

    def test_web_search_voice_mode_fast(self):
        with mock.patch.object(
            app_module, "_run_web_search_tool", side_effect=self._search,
        ), mock.patch.object(app_module, "_begin_search_canvas", return_value={"tab_id": "research-test"}):
            out, meta = app_module._execute_chat_tool(
                "web_search", {"query": "最近新闻"}, 1, voice_mode=True
            )
        self.assertEqual(meta["profile"], "voice")
        self.assertTrue(out)

    def test_web_search_text_mode_full(self):
        with mock.patch.object(
            app_module, "_run_web_search_tool", side_effect=self._search,
        ):
            out, meta = app_module._execute_chat_tool(
                "web_search", {"query": "最近新闻"}, 1
            )
        self.assertEqual(meta["profile"], "full")
        self.assertTrue(out)

    def test_task_control_search_does_not_forward_a_presentation_enum(self):
        result = {
            "ok": True,
            "profile": "voice",
            "items": [{"url": "http://x/voice"}],
            "answer_context": "搜索结果",
        }
        with mock.patch.object(
            app_module, "_web_search_with_background", return_value=result,
        ) as search, mock.patch.object(
            app_module, "_begin_search_canvas", return_value={"tab_id": "research-test"},
        ):
            app_module._execute_chat_tool(
                "task_control",
                {
                    "kind": "web_search",
                    "request": "查接线图",
                    "present": "figure",
                    "continue_after": False,
                },
                1,
                voice_mode=True,
            )
        search.assert_called_once_with(
            "查接线图",
            voice_mode=True,
            research_depth="quick",
            search_queries=[],
            include_visuals=False,
            grounding="",
        )


class ModelSearchPresentationTest(unittest.TestCase):
    def test_model_query_is_constrained_to_previewable_assets(self):
        self.assertEqual(
            app_module._model_search_query("恐龙 3D模型"),
            "恐龙 3D模型 可直接下载 GLB glTF",
        )
        self.assertEqual(app_module._model_search_query("机器人.glb"), "机器人.glb")

    def test_direct_glb_wins_over_fallback(self):
        model = app_module._search_model_asset("任意 3D 模型", {
            "items": [{
                "title": "Duck",
                "url": "https://assets.example/Duck.glb?download=1",
            }],
        })
        self.assertEqual(model["title"], "Duck")
        self.assertIn("Duck.glb", model["url"])

    def test_generic_model_request_uses_offline_demo_when_search_has_only_pages(self):
        model = app_module._search_model_asset("给我搜索一个3D模型", {
            "items": [{"title": "模型网站", "url": "https://example.com/models"}],
        })
        self.assertEqual(model["url"], "/static/models/ev-demo-robot.glb")
        self.assertTrue(app_module._is_generic_model_search("随便来个3D模型看看"))
        self.assertTrue(app_module._is_generic_model_search("3D模型 免费下载 网站"))
        self.assertTrue(app_module._is_generic_model_search("可交互预览的免费3D模型 GLB glTF 在线展示"))
        self.assertFalse(app_module._is_generic_model_search("恐龙3D模型免费网站"))

    def test_truncated_viewer_hostname_is_not_treated_as_a_model_file(self):
        model = app_module._search_model_asset("任意3D模型 GLB 在线预览", {
            "summary": "可在 https://www.gltf-viewer.example/ 在线浏览",
        })
        self.assertEqual(model["url"], "/static/models/ev-demo-robot.glb")
        self.assertFalse(app_module._is_direct_model_url("https://www.gltf"))

    def test_search_push_contains_real_model_payload_without_layout_enum(self):
        result = {
            "query": "3D模型 下载 免费",
            "items": [{"title": "模型网站", "url": "https://example.com/models"}],
        }
        with mock.patch("control_plane.info_panel.push") as push, mock.patch(
            "devices.coding.surface_tools.set_status_timeline_expanded"
        ):
            app_module._push_search_to_info_board(result)
        payload = push.call_args.args[0]
        self.assertNotIn("layout", payload)
        self.assertEqual(payload["model"]["url"], "/static/models/ev-demo-robot.glb")


class SearchPanelStreamTest(unittest.TestCase):
    def test_canvas_search_does_not_duplicate_raw_legacy_panel_to_client(self):
        meta = {
            "task_kind": "web_search",
            "panel": {"kind": "search", "data": {"items": ["raw"]}},
            "canvas": {"visible": True, "tab_id": "research-1"},
        }
        self.assertFalse(app_module._should_emit_legacy_panel("task_control", meta))
        self.assertTrue(app_module._should_emit_legacy_panel(
            "surface_control", {"panel": {"kind": "web"}},
        ))

    def test_voice_canvas_reveals_evidence_only_with_the_final_answer(self):
        info_panel.clear()
        try:
            with mock.patch(
                "devices.coding.surface_tools.sync_status_timeline_to_canvas"
            ) as sync:
                pending = app_module._begin_search_canvas("ESP32 红外成品板")
                staged = info_panel.snapshot()["document"]
                self.assertTrue(staged["pending"])
                self.assertEqual(set(staged["nodes"]), {"summary"})
                committed = app_module._commit_search_answer({
                    "query": "ESP32 红外成品板",
                    "summary": "抓取器原始标题串",
                    "items": [
                        {"title": "精确来源", "url": "https://example.com/1"},
                        {"title": "辅助来源", "url": "https://example.com/2"},
                        {"title": "多余来源", "url": "https://example.com/3"},
                    ],
                    "images": [
                        {"url": "https://example.com/1.jpg"},
                        {"url": "https://example.com/2.jpg"},
                    ],
                }, pending["tab_id"], "找到一个明确匹配的成品板，板载红外收发。")
            document = committed["document"]
            self.assertFalse(document["pending"])
            self.assertTrue(document["answer_locked"])
            self.assertNotIn("抓取器原始标题串", document["nodes"]["summary"]["text"])
            self.assertEqual(
                [key for key in document["nodes"] if key.startswith("source-")],
                ["source-1", "source-2"],
            )
            self.assertEqual(
                [key for key in document["nodes"] if key.startswith("image-")],
                ["image-1", "image-2"],
            )
            self.assertEqual(sync.call_count, 2)
        finally:
            info_panel.clear()

    def test_weak_search_does_not_make_generic_images_look_verified(self):
        info_panel.clear()
        try:
            with mock.patch(
                "devices.coding.surface_tools.sync_status_timeline_to_canvas"
            ):
                receipt = app_module._push_search_to_info_board({
                    "query": "ESP32 红外收发成品板",
                    "summary": "本轮结果与问题匹配度不足。",
                    "evidence_quality": "weak",
                    "answerable": False,
                    "items": [{
                        "title": "泛用 ESP32 教程",
                        "url": "https://example.com/tutorial",
                    }],
                    "images": [{
                        "url": "https://example.com/generic-board.jpg",
                    }],
                })
            document = info_panel.inspect(receipt["tab_id"])["document"]
            self.assertFalse(any(
                node.get("type") == "image"
                for node in document["nodes"].values()
            ))
            self.assertFalse(any(
                node.get("type") == "source"
                for node in document["nodes"].values()
            ))
        finally:
            info_panel.clear()


if __name__ == "__main__":
    unittest.main()
