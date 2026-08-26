import unittest

from speech.tts.voice_catalog import catalog_for_tts


class TTSVoiceCatalogTest(unittest.TestCase):
    def test_huoshan_v1_uses_speaker_choices(self):
        result = catalog_for_tts(
            "huoshan_double_stream",
            {
                "resource_id": "volc.service_type.10029",
                "speaker": "zh_female_wanwanxiaohe_moon_bigtts",
            },
        )

        self.assertEqual(result["voiceKey"], "speaker")
        self.assertIn(
            "zh_female_wanwanxiaohe_moon_bigtts",
            [value for value, _label in result["voices"]],
        )

    def test_huoshan_v1_includes_gujie_voice(self):
        result = catalog_for_tts(
            "huoshan_double_stream",
            {"resource_id": "volc.service_type.10029"},
        )

        self.assertIn(
            ("zh_female_gujie_mars_bigtts", "顾姐 · 女 · 御姐烟嗓"),
            result["voices"],
        )

    def test_huoshan_v2_gets_separate_compatible_choices(self):
        result = catalog_for_tts(
            "huoshan_double_stream",
            {
                "resource_id": "seed-tts-2.0",
                "speaker": "zh_female_xiaohe_uranus_bigtts",
            },
        )
        values = [value for value, _label in result["voices"]]

        self.assertEqual(result["voiceKey"], "speaker")
        self.assertIn("zh_female_xiaohe_uranus_bigtts", values)
        self.assertNotIn("zh_female_wanwanxiaohe_moon_bigtts", values)

    def test_classic_doubao_uses_voice_field(self):
        result = catalog_for_tts(
            "doubao",
            {"voice": "BV001_streaming"},
        )

        self.assertEqual(result["voiceKey"], "voice")
        self.assertEqual(result["current"], "BV001_streaming")
        self.assertGreaterEqual(len(result["voices"]), 2)

    def test_unrelated_provider_keeps_existing_freeform_behavior(self):
        self.assertIsNone(catalog_for_tts("custom", {"voice": "mine"}))


if __name__ == "__main__":
    unittest.main()
