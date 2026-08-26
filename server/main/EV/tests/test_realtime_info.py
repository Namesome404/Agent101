# -*- coding: utf-8 -*-
"""realtime_info 工具单测：定义完整、执行正确、注册表可达。"""
import unittest
from unittest import mock

import app


class RealtimeInfoToolTest(unittest.TestCase):
    def test_tool_definition_complete(self):
        td = app._realtime_info_tool_definition()
        fn = td["function"]
        self.assertEqual(fn["name"], "realtime_info")
        kinds = fn["parameters"]["properties"]["kind"]["enum"]
        self.assertEqual(kinds, ["time", "date", "weather"])
        self.assertIn("location", fn["parameters"]["properties"])

    def test_time_execute(self):
        out, meta = app._realtime_info_execute({"kind": "time"})
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["kind"], "time")
        self.assertIn("现在", out)
        self.assertIn("点", out)

    def test_date_execute(self):
        out, meta = app._realtime_info_execute({"kind": "date"})
        self.assertTrue(meta["ok"])
        self.assertIn("星期", out)
        self.assertIn("年", out)

    def test_weather_execute_delegates_to_voice_realtime_tool(self):
        with mock.patch.object(
            app, "_voice_realtime_tool",
            return_value={"name": "weather", "context": "测试天气：晴 25°C", "elapsed_ms": 5},
        ) as fake:
            out, meta = app._realtime_info_execute({"kind": "weather", "location": "杭州"})
        fake.assert_called_once_with("杭州天气", forced_kind="weather")
        self.assertTrue(meta["ok"])
        self.assertEqual(out, "测试天气：晴 25°C")

    def test_weather_without_location_uses_default_text(self):
        with mock.patch.object(
            app, "_voice_realtime_tool",
            return_value={"name": "weather", "context": "默认天气", "elapsed_ms": 3},
        ) as fake:
            app._realtime_info_execute({"kind": "weather"})
        fake.assert_called_once_with("", forced_kind="weather")

    def test_unknown_kind_returns_failure(self):
        out, meta = app._realtime_info_execute({"kind": "unknown"})
        self.assertFalse(meta["ok"])
        self.assertIn("unknown", out)

    def test_registered_in_action_registry(self):
        self.assertEqual(app._action_registry.resolve("realtime_info"), "realtime_info")

    def test_in_readonly_action_names(self):
        self.assertIn("realtime_info", app._READONLY_ACTION_NAMES)


if __name__ == "__main__":
    unittest.main()
