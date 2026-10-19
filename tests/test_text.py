import unittest

from translator.text import (mask, needs_translation, normalize_glossary, parse_json_reply, placeholder_error,
                             relevant_terms, strip_placeholder_spaces, term_error, unmask)


class MaskTest(unittest.TestCase):
    def roundtrip(self, s):
        m, codes = mask(s)
        self.assertEqual(unmask(m, codes), s)
        return m

    def test_rpgmaker_codes(self):
        m = self.roundtrip("\\C[2]アリス\\C[0]は\\V[1]Gを手に入れた！\\.\\|\\{大声\\}\\n<ボブ>")
        self.assertNotIn("\\", m)
        self.assertIn("アリス", m)
        self.assertIn("大声", m)

    def test_tags_and_format_strings(self):
        for s, hidden in [
            ("<color=red>注意</color>", "color"),
            ("{name}は{0:N0}Gを払った{w=0.5}{/b}", "name"),
            ("[name]：[r]こんにちは[l]", "name"),
            ("残り%02d個、%s、%1、%.1f秒", "%"),
        ]:
            m = self.roundtrip(s)
            self.assertNotIn(hidden, m, s)

    def test_japanese_in_angle_brackets_is_text(self):
        m, _ = mask("<勇者の剣>を手に入れた")
        self.assertIn("勇者の剣", m)
        self.assertTrue(needs_translation("<勇者の剣>", "ja"))

    def test_ruby_keeps_base_text(self):
        m, codes = mask("\\r[漢字,かんじ]を読む")
        self.assertEqual(unmask(m, codes), "漢字を読む")

    def test_percent_sign_in_prose_untouched(self):
        m, _ = mask("50% discount")
        self.assertEqual(m, "50% discount")

    def test_existing_placeholder_lookalike_protected(self):
        self.roundtrip("[[0]]と[[1]]")

    def test_terms_replaced_longest_first(self):
        m, codes = mask("アリスとアリスの剣", {"アリス": "愛麗絲", "アリスの剣": "愛麗絲之劍"})
        self.assertNotIn("アリス", m)
        self.assertEqual(unmask(m.replace("と", "和"), codes), "愛麗絲和愛麗絲之劍")

    def test_unmask_tolerates_spaces_and_order(self):
        self.assertEqual(unmask("[[ 1 ]][[0]]", ["A", "B"]), "BA")

    def test_strip_placeholder_spaces_keeps_fullwidth_indent(self):
        self.assertEqual(strip_placeholder_spaces("[[0]] 愛麗絲 [[1]]"), "[[0]]愛麗絲[[1]]")
        self.assertEqual(strip_placeholder_spaces("　[[0]]"), "　[[0]]")


class ValidateTest(unittest.TestCase):
    def test_placeholder_error(self):
        self.assertEqual(placeholder_error("[[0]]a[[1]]", "b[[1]][[0]]"), "")
        self.assertIn("遺漏", placeholder_error("[[0]]a[[1]]", "b[[0]]"))
        self.assertIn("多出", placeholder_error("[[0]]", "[[0]][[0]]"))

    def test_term_error(self):
        self.assertEqual(term_error("アリスが来た", "愛麗絲來了", {"アリス": "愛麗絲"}), "")
        self.assertIn("アリス→愛麗絲", term_error("アリスが来た", "艾莉絲來了", {"アリス": "愛麗絲"}))

    def test_needs_translation(self):
        self.assertTrue(needs_translation("はい", "ja"))
        self.assertTrue(needs_translation("攻撃力", "ja"))
        self.assertFalse(needs_translation("123 !?", "ja"))
        self.assertFalse(needs_translation("\\C[2]", "ja"))
        self.assertFalse(needs_translation("OK", "ja"))
        self.assertTrue(needs_translation("Hello", "en"))
        self.assertTrue(needs_translation("안녕", "ko"))


class ParseTest(unittest.TestCase):
    def test_variants(self):
        cases = [
            '{"1": "是"}',
            '```json\n{"1": "是"}\n```',
            '<think>想一下 {x}</think>好的：{"1": "是"}',
            '想一下 {不是 JSON} 還有 {"a": 1 中間</think>\n{"1": "是"}',
            '{"1": "是",}',
            '{"translations": {"1": "是"}}',
            '範例 {"1": "例"} 最後答案 {"1": "是"}',
        ]
        for c in cases:
            self.assertEqual(parse_json_reply(c), {"1": "是"}, c)

    def test_no_json(self):
        with self.assertRaises(ValueError):
            parse_json_reply("抱歉，我無法翻譯")


class GlossaryTest(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_glossary({" アリス ": " 愛麗絲 ", "": "x", "a": "", "b": 1}), {"アリス": "愛麗絲"})
        with self.assertRaises(ValueError):
            normalize_glossary(["アリス"])

    def test_relevant(self):
        self.assertEqual(relevant_terms({"アリス": "A", "ボブ": "B"}, ["アリスが来た"]), {"アリス": "A"})


if __name__ == "__main__":
    unittest.main()
