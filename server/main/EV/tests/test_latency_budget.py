# -*- coding: utf-8 -*-
import time
import unittest
from unittest import mock

import app as app_module
from app import _llm_create_with_budget


class FakeCompletions:
    def __init__(self, delay=0.0):
        self.delay = delay

    def create(self, **kwargs):
        if self.delay:
            time.sleep(self.delay)
        return "stream-response"


class FakeClient:
    def __init__(self, delay=0.0):
        self.chat = mock.Mock()
        self.chat.completions = FakeCompletions(delay=delay)


class LlmCreateBudgetTest(unittest.TestCase):
    def test_fast_path_returns_immediately(self):
        client = FakeClient(delay=0.0)
        result = _llm_create_with_budget(client, 5.0, model="m")
        self.assertEqual(result, "stream-response")

    def test_slow_path_raises_timeout(self):
        client = FakeClient(delay=10.0)
        start = time.perf_counter()
        with self.assertRaises(TimeoutError) as ctx:
            _llm_create_with_budget(client, 0.5, model="m")
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertIn("llm_ttft_timeout", str(ctx.exception))

    def test_budget_zero_disables_guard(self):
        client = FakeClient(delay=0.0)
        result = _llm_create_with_budget(client, 0, model="m")
        self.assertEqual(result, "stream-response")

    def test_parallel_race_api_is_removed(self):
        self.assertFalse(hasattr(app_module, "_llm_stream_race"))
        self.assertFalse(hasattr(app_module, "_voice_llm_race_create"))


class SerialFailoverConfigTest(unittest.TestCase):
    def test_voice_llm_backups_skips_unconfigured_and_primary(self):
        app_module._clear_failover_blacklist()
        try:
            def fake_block(provider):
                if provider == "NoSuchProvider":
                    return {}
                return {
                    "api_key": "k",
                    "url": "u",
                    "model_name": "m",
                    "type": "openai",
                }
            with mock.patch.object(
                app_module,
                "_llm_block_for_provider",
                side_effect=fake_block,
            ):
                got = app_module._voice_llm_backups(
                    "DeepSeekLLM",
                    ["DeepSeekLLM", "QwenFlashLLM", "NoSuchProvider"],
                )
            self.assertEqual([name for name, _block in got], ["QwenFlashLLM"])
        finally:
            app_module._clear_failover_blacklist()

    def test_blacklisted_backup_is_skipped(self):
        app_module._clear_failover_blacklist()
        try:
            app_module._blacklist_failover("QwenFlashLLM", "403 test")
            with mock.patch.object(
                app_module,
                "_llm_block_for_provider",
                return_value={
                    "api_key": "k",
                    "url": "u",
                    "model_name": "m",
                    "type": "openai",
                },
            ):
                got = app_module._voice_llm_backups(
                    "DeepSeekLLM",
                    ["QwenFlashLLM"],
                )
            self.assertEqual(got, [])
        finally:
            app_module._clear_failover_blacklist()


if __name__ == "__main__":
    unittest.main()
