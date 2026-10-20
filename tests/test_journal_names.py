"""Standalone journal naming contracts, without filesystem or service fixtures."""

import time
import unittest

from lifecontext.custody import SourceManifest
from lifecontext.journal_names import (
    first_line_journal_title,
    journal_basename,
    journal_title,
)


def manifest(provenance=None, version=1):
    return SourceManifest("src_test", version, "0" * 64, 0, provenance or {})


class JournalNamesTests(unittest.TestCase):
    def test_title_priority(self):
        raw = b"First text\n## First heading ###\n# Second heading"
        self.assertEqual(journal_title(manifest({"title": "  My  title\n "}), raw), "My title")
        for title in (None, "  ", 42, []):
            with self.subTest(title=title):
                self.assertEqual(journal_title(manifest({"title": title}), raw), "First heading")
        self.assertEqual(journal_title(manifest(), b"\nFirst meaningful line\nNext"), "First meaningful line")
        self.assertEqual(journal_title(manifest(), b" \n---\n***\n"), "Journal entry")

    def test_markdown_metadata_and_code_are_not_titles(self):
        raw = b"---\ntitle: Metadata\n---\n```md\n# Fake\n```\nReal heading\n============\n"
        self.assertEqual(journal_title(manifest(), raw), "Real heading")
        self.assertEqual(journal_title(manifest(), b"~~~\n# Fake\n~~~\nText"), "Text")
        self.assertEqual(journal_title(manifest(), "\ufeff# Café".encode()), "Café")

    def test_frontmatter_and_fences_skip_to_meaningful_body(self):
        prefix = b"---\ntitle: Metadata\ndate: 2026-10-09\n...\n```md\n# Fake 2026-10-08\n```\n~~~\nFake text\n~~~\n"
        for body, expected in ((b"First meaningful line\nNext", "First meaningful line"), (b"Intro\n# Real heading", "Real heading"), (b"", "Journal entry")):
            with self.subTest(body=body):
                self.assertEqual(journal_title(manifest(), prefix + body), expected)
                self.assertEqual(journal_basename(manifest(), prefix + body), expected)

    def test_invalid_utf8_raises_even_with_provenance_title(self):
        for helper in (journal_title, journal_basename):
            with self.subTest(helper=helper.__name__):
                with self.assertRaisesRegex(ValueError, "UTF-8"):
                    helper(manifest({"title": "Known"}), b"\xff")

    def test_explicit_dates_and_timestamps(self):
        for field in ("date", "entry_date", "journal_date", "timestamp", "created_at", "captured_at", "occurred_at"):
            with self.subTest(field=field):
                self.assertEqual(journal_basename(manifest({field: "2026-10-09T23:30:00-07:00"}), b"# Walk"), "2026-10-09 - Walk")
        self.assertEqual(journal_basename(manifest({"date": "2024-02-29", "timestamp": "2026-10-09T12:00:00Z"}), b"# Walk"), "2024-02-29 - Walk")

    def test_heading_date_fallback_and_no_invented_dates(self):
        self.assertEqual(journal_basename(manifest({"date": "2026-02-30"}), b"# Walk on 2026-10-09"), "2026-10-09 - Walk on 2026-10-09")
        self.assertEqual(journal_basename(manifest(), b"# 2026-10-09 Walk"), "2026-10-09 - Walk")
        for provenance in ({}, {"date": 1791504000}, {"date": "yesterday"}, {"timestamp": "2026-10-09garbage"}, {"source_ref": "2026-10-09"}):
            with self.subTest(provenance=provenance):
                self.assertEqual(journal_basename(manifest(provenance), b"# Walk\nBody 2026-10-09"), "Walk")
        self.assertEqual(journal_basename(manifest(), b"# Invalid 2026-02-30"), "Invalid 2026-02-30")

    def test_legacy_metadata_title_and_actual_entry_date(self):
        metadata = {
            "title": "  Earlier  walk ",
            "occurred_at": "2024-02-29T23:30:00-07:00",
            "occurred_at_precision": "minute",
            "timestamp": "2026-10-07T12:00:00Z",
            "captured_at": "2026-10-08T12:00:00Z",
            "created_at": "2026-10-09T12:00:00Z",
            "timezone": "UTC",
        }
        provenance = {"journal_source_metadata": metadata}
        raw = b"# Content heading 2026-10-09"
        self.assertEqual(journal_title(manifest(provenance), raw), "Earlier walk")
        self.assertEqual(journal_basename(manifest(provenance), raw), "2024-02-29 - Earlier walk")
        self.assertEqual(journal_basename(manifest(metadata), raw), "2024-02-29 - Earlier walk")
        provenance.update({"title": "Root title", "captured_at": "2025-01-02"})
        self.assertEqual(journal_basename(manifest(provenance), raw), "2025-01-02 - Root title")

    def test_legacy_metadata_fallbacks(self):
        for metadata in (None, [], "invalid", {"title": " ", "occurred_at": "2026-02-30", "occurred_at_precision": "day", "timezone": "UTC"}):
            with self.subTest(metadata=metadata):
                provenance = {"journal_source_metadata": metadata}
                self.assertEqual(journal_basename(manifest(provenance), b"# Walk"), "Walk")
                self.assertEqual(journal_basename(manifest(provenance), b"# 2024-02-29 Walk"), "2024-02-29 - Walk")
        for field in ("timestamp", "captured_at", "created_at"):
            with self.subTest(field=field):
                provenance = {"title": " ", "date": "invalid", "journal_source_metadata": {"title": "Legacy", "occurred_at": "invalid", field: "2024-02-29"}}
                self.assertEqual(journal_basename(manifest(provenance), b"# Content"), "2024-02-29 - Legacy")

    def test_unicode_and_unsafe_characters(self):
        title = '  ../Café: 日記 [walk] #tag ^block /\\*?"<>|\x00\u202e.  '
        result = journal_basename(manifest({"title": title}), b"")
        self.assertEqual(result, "Café 日記 walk tag block")
        self.assertEqual(journal_basename(manifest({"title": "Cafe\u0301"}), b""), "Café")
        for title in ("...", "[]/#^", "\x00"):
            self.assertEqual(journal_basename(manifest({"title": title}), b""), "Journal entry")
        for title in ("CON", "nul.txt", "LPT1"):
            self.assertEqual(journal_basename(manifest({"title": title}), b""), f"Journal entry - {title}")

    def test_byte_budget_and_version_suffix(self):
        for title in ("日" * 500, "😀" * 500, "a" * 500):
            for version in (1, 2, 2147483647):
                with self.subTest(title=title[:1], version=version):
                    result = journal_basename(manifest({"title": title, "date": "2026-10-09"}, version), b"")
                    self.assertLessEqual(len(result.encode()), 180)
                    temporary = f".{result}.md.tmp.{'0' * 20}"
                    self.assertLessEqual(len(temporary.encode()), 255)
                    self.assertTrue(result.startswith("2026-10-09 - "))
                    if version > 1:
                        self.assertTrue(result.endswith(f" - v{version}"))
        self.assertEqual(journal_basename(manifest(version=2), b"# Walk"), "Walk - v2")

    def test_date_prefix_is_not_repeated_and_keeps_one_separator(self):
        for title in ("#2026-10-09 Walk", "[2026-10-09] Walk", "\u202e2026-10-09 Walk", "../2026-10-09 Walk", "2026-10-09 - Walk", "2026-10-09: Walk"):
            with self.subTest(title=title):
                self.assertEqual(journal_basename(manifest({"title": title, "date": "2026-10-09"}, 2), b""), "2026-10-09 - Walk - v2")
        self.assertEqual(journal_basename(manifest({"title": "2026-10-09", "date": "2026-10-09"}), b""), "2026-10-09 - Journal entry")
        for title, expected in (("CON", "CON"), ("nul.txt", "nul.txt"), ("[]/#^", "Journal entry")):
            with self.subTest(title=title):
                self.assertEqual(journal_basename(manifest({"title": title, "date": "2026-10-09"}), b""), f"2026-10-09 - {expected}")

    def test_exact_truncation_and_suffix(self):
        self.assertEqual(journal_basename(manifest({"title": "a/\u0301"}), b""), "a\u0301")
        self.assertEqual(journal_basename(manifest({"title": "日" * 100}, 2), b""), "日" * 58 + " - v2")
        self.assertEqual(journal_basename(manifest({"title": "a" * 174 + " .tail"}, 2), b""), "a" * 174 + " - v2")

    def test_opening_date_line_dates_the_entry_and_is_not_its_title(self):
        cases = (
            (("Entry date: 2026-10-12 (Europe/Vienna).\nProvenance: user-provided update; not verified.\n\n"
              "idleflow.io is now listed for sale on Afternic. It isn't sold yet."),
             "2026-10-12 - idleflow.io is now listed for sale on Afternic", "idleflow.io is now listed for sale on Afternic"),
            ("Дата запису: 2026-10-08 (Europe/Vienna).\n\nСьогодні насичений робочий день. Я вкомітився в роботу.",
             "2026-10-08 - Сьогодні насичений робочий день", "Сьогодні насичений робочий день"),
            ("Рома Співбесіда\nMon, Sep 14, 2026\n\nSummary:", "2026-09-14 - Рома Співбесіда", "Рома Співбесіда"),
            ("Date: 13.10.2026 21:30 UTC\nTimezone: Europe/Vienna\nSource: voice note\nEvening run", "2026-10-13 - Evening run", "Evening run"),
            ("# Evening run\nEntry date: 2026-10-13 (Europe/Vienna).\nBody", "2026-10-13 - Evening run", "Evening run"),
            ("2026-10-13\n", "2026-10-13 - Journal entry", "Journal entry"),
            ("Дата запису: 12.10.2026.\n\nСьогодні був у лікаря.", "2026-10-12 - Сьогодні був у лікаря", "Сьогодні був у лікаря"),
            ("# 2026-10-12\n\nСьогодні був у лікаря.", "2026-10-12 - Сьогодні був у лікаря", "Сьогодні був у лікаря"),
            ("Entry date: 2026-10-12 (Europe/Vienna)\n---\nA quiet day at home.", "2026-10-12 - A quiet day at home", "A quiet day at home"),
            ("# Entry date: 2026-10-12\n## Evening run\nBody", "2026-10-12 - Evening run", "Evening run"),
            ("# Notes on the 2019-05-05 trip\nEntry date: 2026-10-12", "2026-10-12 - Notes on the 2019-05-05 trip", "Notes on the 2019-05-05 trip"),
            ("Згадав, як 5 травня 2019 року їздили в гори.\nEntry date: 2026-10-12 (Europe/Vienna).",
             "2026-10-12 - Згадав, як 5 травня 2019 року їздили в гори", "Згадав, як 5 травня 2019 року їздили в гори"),
            ("Запис\n13-го жовтня 2026 р.", "2026-10-13 - Запис", "Запис"),
            ("Запис\nEntry date: 2026-10-12 21:30 Europe/Vienna+02:00", "2026-10-12 - Запис", "Запис"),
            ("Запис\n2026-10-12, Monday, America/Port-au-Prince", "2026-10-12 - Запис", "Запис"),
            ("Запис\n12 жовтня 2026 р., вівторок", "2026-10-12 - Запис", "Запис"),
            ("# 2026-10-12\n\nСьогодні був у лікаря. Все добре.\n\n## Плани на завтра", "2026-10-12 - Сьогодні був у лікаря", "Сьогодні був у лікаря"),
            ("Entry date: 2026-10-12 (Europe/Vienna).\nProvenance: user-provided update.\n---\nA quiet day at home.",
             "2026-10-12 - A quiet day at home", "A quiet day at home"),
            ("Запис\nПн, 12 жовт. 2026, 21:30 +02:00", "2026-10-12 - Запис", "Запис"),
            ("Запись\n12 окт. 2026 г.", "2026-10-12 - Запись", "Запись"),
        )
        for text, basename, title in cases:
            with self.subTest(basename=basename):
                self.assertEqual(journal_basename(manifest(), text.encode()), basename)
                self.assertEqual(journal_title(manifest(), text.encode()), title)

    def test_written_dates_in_the_first_line(self):
        cases = (
            ("Щоденник за 13 жовтня 2026 року. Часовий пояс: Europe/Vienna.\n\nОригінальний звіт:", "2026-10-13 - Щоденник за 13 жовтня 2026 року"),
            ("Дневник за 3 марта 2026 года", "2026-03-03 - Дневник за 3 марта 2026 года"),
            ("Notes from September 7th, 2026 standup", "2026-09-07 - Notes from September 7th, 2026 standup"),
            ("Walk on 2026-10-09", "2026-10-09 - Walk on 2026-10-09"),
            ("2026-10-09 Walk", "2026-10-09 - Walk"),
        )
        for text, basename in cases:
            with self.subTest(basename=basename):
                self.assertEqual(journal_basename(manifest(), text.encode()), basename)

    def test_dates_that_are_not_the_entry_date_are_ignored(self):
        for text in (
            "Product idea\nDeadline is 2026-12-01 for the first version",
            "Product idea\nDeadline: 2026-12-01",
            "Візит до лікаря\nНаступний прийом: 03.11.2026",
            "Про тата\nНародився: 12 травня 1957 р.",
            "Product idea\nSecond line\nThird line\nEntry date: 2026-10-12",
            "Version 13.14.2026 notes",
            "Met 5 по 2026 рік",
            "Reading list for February 30, 2026",
            "A long opening sentence that goes on without a date for quite a while. Then 2026-10-09 appears.",
        ):
            with self.subTest(text=text):
                self.assertNotRegex(journal_basename(manifest(), text.encode()), r"^\d{4}-\d{2}-\d{2}")
        self.assertEqual(journal_basename(manifest({"date": "2026-10-01"}), b"Entry date: 2026-10-12\nWalk"), "2026-10-01 - Walk")

    def test_a_mentioned_date_counts_only_near_the_retained_day(self):
        def name(text, retained="2026-10-13"):
            return journal_basename(manifest(), text.encode(), fallback_date=retained)

        diary = "Щоденник за 13 жовтня 2026 року. Часовий пояс: Europe/Vienna."
        self.assertEqual(name(diary), "2026-10-13 - Щоденник за 13 жовтня 2026 року")
        self.assertEqual(name(diary, "2026-10-14"), "2026-10-13 - Щоденник за 13 жовтня 2026 року")
        self.assertEqual(name(diary, "2026-11-13"), "2026-10-13 - Щоденник за 13 жовтня 2026 року")
        self.assertEqual(name(diary, "2026-11-14"), "2026-11-14 - Щоденник за 13 жовтня 2026 року")
        self.assertEqual(name(diary, "2026-10-12"), "2026-10-13 - Щоденник за 13 жовтня 2026 року")
        self.assertEqual(name(diary, "2026-10-11"), "2026-10-11 - Щоденник за 13 жовтня 2026 року")
        self.assertEqual(name("Згадав, як 5 травня 2019 року їздили в гори."), "2026-10-13 - Згадав, як 5 травня 2019 року їздили в гори")
        self.assertEqual(name("Plans for 2026-12-01 and after"), "2026-10-13 - Plans for 2026-12-01 and after")
        self.assertEqual(name("Запис\nMon, Sep 14, 2020"), "2020-09-14 - Запис")
        late = "Щоденник за 14 жовтня 2026 року. Часовий пояс: Europe/Vienna."
        self.assertEqual(name(late), "2026-10-14 - Щоденник за 14 жовтня 2026 року")
        self.assertEqual(name("Щоденник за 13 жовт. 2026 року. Часовий пояс: Europe/Vienna.", "2026-10-14"), "2026-10-13 - Щоденник за 13 жовт. 2026 року")

    def test_long_and_ambiguous_lines_are_handled_quickly(self):
        lines = (
            "2026-10-12 src/lifecontext/api/routes/projections/reconcile/handler/tests/unit/more/and/more broke",
            "2026-10-12 " + "a/b" * 2000 + "!",
            "2026-10-12" + " 09:00-10:00" * 40 + " meetings",
            "Entry date: 2026-10-12 " + "(" * 5000,
            "2026-10-12 " + "thurs" * 37 + "d",
            "x" * 200_000,
            "word. " * 50_000,
            "2026-10-12 " * 20_000,
        )
        started = time.perf_counter()
        for line in lines:
            self.assertRegex(journal_basename(manifest(), line.encode(), fallback_date="2026-10-13"), r"^2026-10-1[23] - ")
        self.assertLess(time.perf_counter() - started, 5)

    def test_fallback_date_applies_only_without_a_stated_date(self):
        self.assertEqual(journal_basename(manifest(), b"# Walk", fallback_date="2026-10-13"), "2026-10-13 - Walk")
        self.assertEqual(journal_basename(manifest(version=3), b"Walk", fallback_date="2026-10-13"), "2026-10-13 - Walk - v3")
        self.assertEqual(journal_basename(manifest(), b"# Walk on 2026-10-09", fallback_date="2026-10-13"), "2026-10-09 - Walk on 2026-10-09")
        self.assertEqual(journal_basename(manifest({"date": "2026-10-01"}), b"# Walk", fallback_date="2026-10-13"), "2026-10-01 - Walk")
        with self.assertRaises(ValueError):
            journal_basename(manifest(), b"# Walk", fallback_date="yesterday")

    def test_opening_sentence_titles_are_short_and_headings_are_kept_whole(self):
        self.assertEqual(journal_title(manifest(), "Пробіг зранку гарне тренування темпове. 5 по 7 хв в середньому.".encode()), "Пробіг зранку гарне тренування темпове")
        self.assertEqual(journal_title(manifest(), b"Ran approx. five km before work. Felt good."), "Ran approx. five km before work")
        self.assertEqual(journal_title(manifest(), b"Hm. Today went well. Tomorrow too."), "Hm. Today went well")
        for text, title in (
            ("Сьогодні їздили в м. Львів на вихідні. Було класно.", "Сьогодні їздили в м. Львів на вихідні"),
            ("Зустрівся з проф. Іваненком щодо дисертації. Далі буде.", "Зустрівся з проф. Іваненком щодо дисертації"),
            ("Talked to Mr. Jones about the roof. He agreed.", "Talked to Mr. Jones about the roof"),
            ("12 жовтня 2026 р. Прогулянка в парку", "12 жовтня 2026 р. Прогулянка в парку"),
            ("Сьогодні насичений робочий день. Я вкомітився в роботу.", "Сьогодні насичений робочий день"),
            ("It simply isn't done. Next time.", "It simply isn't done"),
            ("Зустріч 12 жовт. 2026 пройшла добре. Далі буде.", "Зустріч 12 жовт. 2026 пройшла добре"),
            ("Notes from Sept. 7th, 2026 standup. More later.", "Notes from Sept. 7th, 2026 standup"),
            ("Прочитав стор. 12 нової книги. Сподобалось.", "Прочитав стор. 12 нової книги"),
            ("Meeting with Capt. Smith went fine. More later.", "Meeting with Capt. Smith went fine"),
            ("Today was a good day. Tomorrow I will rest and read.", "Today was a good day"),
            ("Сьогодні 13 жовтня. Прокинувся рано і пішов бігати.", "Сьогодні 13 жовтня"),
            ("We met in March. It was a good trip.", "We met in March"),
            ("Зранку пішов гуляти в ліс. Потім працював до вечора.", "Зранку пішов гуляти в ліс"),
        ):
            with self.subTest(text=text):
                self.assertEqual(journal_title(manifest(), text.encode()), title)
        cyrillic = ("слово " * 40).encode()
        self.assertEqual(journal_title(manifest(), cyrillic), ("слово " * 13).strip())
        self.assertEqual(
            journal_basename(manifest(version=2147483647), cyrillic, fallback_date="2026-10-13"),
            "2026-10-13 - " + ("слово " * 13).strip() + " - v2147483647",
        )
        self.assertEqual(journal_title(manifest(), b"Synthetic milestone acceptance; no personal data.\n"), "Synthetic milestone acceptance; no personal data")
        long_line = ("word " * 60).encode()
        self.assertEqual(journal_title(manifest(), long_line), ("word " * 20).strip())
        self.assertEqual(journal_title(manifest(), b"x" * 300), "x" * 100)
        heading = "Heading. " * 20
        self.assertEqual(journal_title(manifest(), ("# " + heading).encode()), heading.strip())
        self.assertEqual(journal_title(manifest({"title": "Stated. Title."}), b"Body"), "Stated. Title.")

    def test_headings_stay_titles_unless_they_only_state_the_date(self):
        for text, title in (
            ("# Source: The Lean Startup\n\nNotes on chapter one. More.", "Source: The Lean Startup"),
            ("# Дата: побачення з Анною\n\nБуло добре.", "Дата: побачення з Анною"),
            ("# Timezone: notes on DST handling\nBody", "Timezone: notes on DST handling"),
            ("Intro text. More.\n# Real heading", "Real heading"),
            ("Source: voice note\n---\nEvening run in the park. More.", "Evening run in the park"),
            ("# Entry date: 2026-10-12\n\n## Evening run\nBody", "Evening run"),
            ("# Mon, Sep 14, 2026\n", "Journal entry"),
        ):
            with self.subTest(text=text):
                self.assertEqual(journal_title(manifest(), text.encode()), title)

    def test_first_line_title_reproduces_the_superseded_rule(self):
        raw = b"Entry date: 2026-10-12 (Europe/Vienna).\nFirst sentence. Second."
        self.assertEqual(first_line_journal_title(manifest(), raw), "Entry date: 2026-10-12 (Europe/Vienna).")
        self.assertEqual(first_line_journal_title(manifest(), b"First sentence. Second."), "First sentence. Second.")
        self.assertEqual(first_line_journal_title(manifest(), b" \n---\n"), "Journal entry")
        for provenance, text in (({"title": "Stated"}, b"Body"), ({"journal_source_metadata": {"title": "Legacy"}}, b"Body"), ({}, b"Intro\n# Heading")):
            with self.subTest(provenance=provenance):
                self.assertEqual(first_line_journal_title(manifest(provenance), text), journal_title(manifest(provenance), text))

    def test_names_do_not_depend_on_source_identity_or_hash(self):
        first = manifest()
        other = SourceManifest("src_other", 1, "f" * 64, 100, {})
        self.assertEqual(journal_basename(first, b"# Walk"), "Walk")
        self.assertEqual(journal_basename(first, b"# Walk"), journal_basename(other, b"# Walk"))


if __name__ == "__main__":
    unittest.main()
