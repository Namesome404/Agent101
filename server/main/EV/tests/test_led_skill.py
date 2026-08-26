import unittest
from unittest import mock

from devices.coding import led


class LedSkillDispatchTest(unittest.TestCase):
    """led skill 动作分发与参数校验。mock 掉设备 HTTP，验证回执不谎报。"""

    def setUp(self):
        self.patcher = mock.patch.object(led, "_request", autospec=True)
        self.mock_request = self.patcher.start()
        self.addCleanup(self.patcher.stop)
        # 默认返回一个完整 state，模拟设备在线
        self.state = {
            "power": True,
            "red": 255,
            "green": 0,
            "blue": 0,
            "brightness": 100,
            "effect": "solid",
            "speed": 50,
            "count": 60,
        }
        def request(method, path, payload=None):
            if method == "POST" and payload:
                self.state.update(payload)
            return dict(self.state)
        self.mock_request.side_effect = request

    def test_power_on_ok(self):
        text, meta = led.execute(
            "led_control", {"action": "power", "on": True}
        )
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["action"], "power")
        self.assertTrue(meta["on"])
        self.assertEqual(
            self.mock_request.call_args_list,
            [
                mock.call("POST", "/api/led/state", {"power": True}),
                mock.call("GET", "/api/led/status"),
            ],
        )
        self.assertIn("已打开", text)

    def test_power_off_ok(self):
        text, meta = led.execute(
            "led_control", {"action": "power", "on": False}
        )
        self.assertTrue(meta["ok"])
        self.assertFalse(meta["on"])
        self.assertIn("已关闭", text)

    def test_brightness_out_of_range_fails_truthfully(self):
        text, meta = led.execute(
            "led_control", {"action": "brightness", "brightness": 150}
        )
        self.assertFalse(meta["ok"])
        self.assertIn("必须在 0 到 100 之间", text)
        self.mock_request.assert_not_called()

    def test_color_payload(self):
        text, meta = led.execute(
            "led_control",
            {"action": "color", "red": 0, "green": 128, "blue": 255},
        )
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["action"], "color")
        payload = self.mock_request.call_args_list[0][0][2]
        self.assertEqual(
            payload,
            {"power": True, "red": 0, "green": 128, "blue": 255, "effect": "solid"},
        )

    def test_named_color_uses_deterministic_rgb(self):
        text, meta = led.execute(
            "led_control",
            {"action": "color", "color_name": "green"},
        )
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["color_name"], "green")
        self.assertIn("绿色", text)
        payload = self.mock_request.call_args_list[0][0][2]
        self.assertEqual(
            payload,
            {"power": True, "red": 0, "green": 255, "blue": 0, "effect": "solid"},
        )

    def test_effect_validation(self):
        text, meta = led.execute(
            "led_control", {"action": "effect", "effect": "strobe"}
        )
        self.assertFalse(meta["ok"])
        self.assertIn("不支持的 effect", text)
        self.mock_request.assert_not_called()

    def test_effect_ok(self):
        text, meta = led.execute(
            "led_control", {"action": "effect", "effect": "rainbow", "speed": 70}
        )
        self.assertTrue(meta["ok"])
        payload = self.mock_request.call_args_list[0][0][2]
        self.assertEqual(
            payload,
            {"power": True, "effect": "rainbow", "speed": 70},
        )
        self.assertIn("rainbow", text)

    def test_set_requires_at_least_one_field(self):
        text, meta = led.execute("led_control", {"action": "set"})
        self.assertFalse(meta["ok"])
        self.assertIn("至少提供一个", text)
        self.mock_request.assert_not_called()

    def test_set_multiple_fields(self):
        text, meta = led.execute(
            "led_control",
            {"action": "set", "brightness": 40, "effect": "breathing"},
        )
        self.assertTrue(meta["ok"])
        payload = self.mock_request.call_args_list[0][0][2]
        self.assertEqual(
            payload,
            {"brightness": 40, "effect": "breathing"},
        )

    def test_device_unreachable_returns_ok_false(self):
        self.mock_request.side_effect = RuntimeError("无法连接灯光设备 http://ws2812.local")
        text, meta = led.execute(
            "led_control", {"action": "power", "on": True}
        )
        self.assertFalse(meta["ok"])
        self.assertIn("无法连接", text)
        self.assertIn("error", meta)

    def test_write_response_is_not_enough_without_matching_readback(self):
        stale = dict(self.state)
        stale.update({"red": 255, "green": 0, "blue": 0})
        self.mock_request.side_effect = [dict(stale), dict(stale), dict(stale)]
        text, meta = led.execute(
            "led_control",
            {"action": "color", "red": 0, "green": 255, "blue": 0},
        )
        self.assertFalse(meta["ok"])
        self.assertFalse(meta["verified"])
        self.assertIn("实际状态与请求不一致", text)

    def test_write_timeout_but_readback_confirms_is_success(self):
        """POST 响应超时/丢包不代表设备没执行：独立读回符合期望即算成功。"""
        def flaky_post(method, path, payload=None):
            if method == "POST":
                self.state.update(payload)
                raise RuntimeError("无法连接灯光设备 http://ws2812.local：timed out")
            return dict(self.state)
        self.mock_request.side_effect = flaky_post
        text, meta = led.execute(
            "led_control", {"action": "color", "color_name": "green"}
        )
        self.assertTrue(meta["ok"])
        self.assertTrue(meta["verified"])
        self.assertTrue(meta["write_response_lost"])
        self.assertIn("绿色", text)
        methods = [call.args[0] for call in self.mock_request.call_args_list]
        self.assertEqual(methods, ["POST", "GET"])

    def test_write_timeout_and_readback_stale_still_fails(self):
        """POST 超时且读回仍是旧状态：确认真失败，不谎报成功。"""
        stale = dict(self.state)
        stale.update({"red": 255, "green": 0, "blue": 0})
        def flaky_post(method, path, payload=None):
            if method == "POST":
                raise RuntimeError("无法连接灯光设备 http://ws2812.local：timed out")
            return dict(stale)
        self.mock_request.side_effect = flaky_post
        text, meta = led.execute(
            "led_control", {"action": "color", "color_name": "green"}
        )
        self.assertFalse(meta["ok"])
        self.assertFalse(meta["verified"])
        self.assertIn("无法连接", text)
        self.assertIn("读回未确认变更", text)

    def test_status_returns_state(self):
        text, meta = led.execute("led_control", {"action": "status"})
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["action"], "status")
        self.assertEqual(meta["state"]["effect"], "solid")
        self.mock_request.assert_called_once_with("GET", "/api/led/status")

    def test_unknown_action_fails(self):
        text, meta = led.execute("led_control", {"action": "frobnicate"})
        self.assertFalse(meta["ok"])
        self.assertIn("未知的 led_control action", text)

    def test_tool_definition_schema(self):
        definitions = led.tool_definitions()
        self.assertEqual(len(definitions), 1)
        fn = definitions[0]["function"]
        self.assertEqual(fn["name"], "led_control")
        required = fn["parameters"]["required"]
        self.assertIn("action", required)
        enum = fn["parameters"]["properties"]["action"]["enum"]
        self.assertEqual(
            enum, ["power", "color", "brightness", "effect", "set", "status"]
        )
        self.assertIn(
            "green",
            fn["parameters"]["properties"]["color_name"]["enum"],
        )


class LedSkillRegisterTest(unittest.TestCase):
    """led skill 注册进 action_registry。"""

    def test_register_into_registry(self):
        from devices.coding.action_registry import ActionRegistry

        registry = ActionRegistry()
        led.register(registry)
        self.assertTrue(registry.has("led_control"))

        with mock.patch.object(
            led,
            "_request",
            side_effect=[{"power": True}, {"power": True}],
        ):
            result = registry.exec_action(
                "led_control", {"action": "power", "on": True}, {}
            )
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
