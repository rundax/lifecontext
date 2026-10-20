"""Shared readable filename contracts for journal and wiki projections."""

import unittest

from lifecontext.projection_names import readable_basename


class ProjectionNamesTests(unittest.TestCase):
    def test_unicode_unsafe_characters_and_whitespace(self):
        title = '  ../Cafe\u0301: 日記 [walk] #tag ^block /\\*?"<>|\x00\u202e.  '
        self.assertEqual(readable_basename(title, fallback="Wiki page"), "Café 日記 walk tag block")
        self.assertEqual(readable_basename("One\t two\nthree\u00a0four", fallback="Wiki page"), "One twothree four")

    def test_empty_and_reserved_names_use_caller_fallback(self):
        for title in ("", "   ", "...", "[]/#^", "\x00\u202e"):
            with self.subTest(title=title):
                self.assertEqual(readable_basename(title, fallback="Wiki page"), "Wiki page")
        for title in ("CON", "prn", "AUX", "nul.txt", "COM1", "COM9.txt", "LPT1", "lpt9"):
            with self.subTest(title=title):
                self.assertEqual(readable_basename(title, fallback="Wiki page"), f"Wiki page - {title}")
        for title in ("COM0", "COM10", "LPT0", "LPT10", "CONtext"):
            with self.subTest(title=title):
                self.assertEqual(readable_basename(title, fallback="Wiki page"), title)

    def test_byte_budget_includes_intact_suffix(self):
        for title in ("a" * 500, "日" * 500, "😀" * 500):
            for suffix in ("", " - v2", " - 日本語"):
                with self.subTest(title=title[:1], suffix=suffix):
                    budget = 180 - len(suffix.encode())
                    expected = title.encode()[:budget].decode(errors="ignore") + suffix
                    result = readable_basename(title, fallback="Wiki page", suffix=suffix)
                    self.assertEqual(result, expected)
                    self.assertLessEqual(len(result.encode()), 180)
        self.assertEqual(readable_basename("a" * 174 + " .tail", fallback="Wiki page", suffix=" - v2"), "a" * 174 + " - v2")

    def test_suffix_on_fallback_and_reserved_name(self):
        self.assertEqual(readable_basename("[]", fallback="Wiki page", suffix=" - v2"), "Wiki page - v2")
        self.assertEqual(readable_basename("CON", fallback="Wiki page", suffix=" - v2"), "Wiki page - CON - v2")
        with self.assertRaisesRegex(ValueError, "suffix"):
            readable_basename("Title", fallback="Wiki page", suffix="a" * 181)


if __name__ == "__main__":
    unittest.main()
