import json
import os
import tempfile
import unittest

from settings import DEFAULTS, load_settings, save_settings


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False))

    def test_missing_file_gives_defaults_and_shows_guide(self):
        s = load_settings(self.path)
        self.assertEqual(s, DEFAULTS)
        self.assertFalse(s["guide_seen"])

    def test_corrupt_file_gives_defaults(self):
        self.write("{壞掉")
        self.assertEqual(load_settings(self.path), DEFAULTS)
        self.write("[1, 2]")
        self.assertEqual(load_settings(self.path), DEFAULTS)

    def test_v1_migration(self):
        self.write({"category": "本地 LLM", "preset": "Sakura / llama.cpp", "src_lang": "ja", "tgt_lang": "zh-TW",
                    "per_preset": {"OpenAI": {"api_key": "sk-1", "model": "m"}, "已刪除的服務": {"api_key": "x"}},
                    "context_lines": 3, "background": "bg", "input": "a.json", "output": "b.json"})
        s = load_settings(self.path)
        self.assertEqual(s["provider"], "sakura")
        self.assertEqual(s["per_provider"], {"openai": {"api_key": "sk-1", "model": "m"}})
        self.assertEqual((s["context_lines"], s["background"], s["input"]), (3, "bg", "a.json"))
        self.assertTrue(s["guide_seen"])

    def test_invalid_values_fall_back(self):
        self.write({"version": 2, "provider": "不存在", "src_lang": "xx", "context_lines": "很多",
                    "per_provider": {"openai": "壞掉", "claude": {"model": "m"}}})
        s = load_settings(self.path)
        self.assertEqual((s["provider"], s["src_lang"], s["context_lines"]), ("claude", "ja", 5))
        self.assertEqual(s["per_provider"], {"claude": {"model": "m"}})

    def test_roundtrip(self):
        s = dict(DEFAULTS, provider="ollama", guide_seen=True)
        save_settings(self.path, s)
        self.assertEqual(load_settings(self.path), s)


if __name__ == "__main__":
    unittest.main()
