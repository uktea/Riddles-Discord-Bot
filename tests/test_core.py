from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from riddle_bot.core import (
    MAX_ANSWER_LIMIT,
    AnswerLimitValidationError,
    AnswerValidationError,
    ModeValidationError,
    TermValidationError,
    answer_matches,
    deadline_from_term,
    normalize_answer,
    parse_term,
    prepare_answers,
    validate_answer_limit,
    validate_mode,
)


class TermTests(unittest.TestCase):
    def test_missing_term_defaults_to_24_hours(self) -> None:
        self.assertEqual(parse_term(None), timedelta(hours=24))
        self.assertEqual(parse_term(""), timedelta(hours=24))
        self.assertEqual(parse_term(" \t "), timedelta(hours=24))

    def test_accepts_supported_units_case_insensitively(self) -> None:
        self.assertEqual(parse_term("30m"), timedelta(minutes=30))
        self.assertEqual(parse_term(" 2H "), timedelta(hours=2))
        self.assertEqual(parse_term("1d"), timedelta(days=1))

    def test_accepts_range_boundaries(self) -> None:
        self.assertEqual(parse_term("1m"), timedelta(minutes=1))
        self.assertEqual(parse_term("30d"), timedelta(days=30))
        self.assertEqual(parse_term("720h"), timedelta(days=30))

    def test_rejects_invalid_format_or_range(self) -> None:
        for value in ("0m", "31d", "721h", "1.5h", "h1", "1hour", "-1h", "1 h"):
            with self.subTest(value=value), self.assertRaises(TermValidationError):
                parse_term(value)

    def test_builds_an_aware_utc_deadline(self) -> None:
        japan_time = datetime(
            2026,
            7,
            25,
            12,
            0,
            tzinfo=timezone(timedelta(hours=9)),
        )
        deadline = deadline_from_term("2h", now=japan_time)
        self.assertEqual(
            deadline,
            datetime(2026, 7, 25, 5, 0, tzinfo=timezone.utc),
        )


class AnswerTests(unittest.TestCase):
    def test_normalizes_nfkc_all_whitespace_and_case(self) -> None:
        self.assertEqual(normalize_answer(" Ａ\u3000b\tC\n"), "abc")
        self.assertEqual(normalize_answer("Straße"), normalize_answer("STRASSE"))

    def test_prepares_pipe_separated_candidates_and_deduplicates(self) -> None:
        self.assertEqual(
            prepare_answers(" 東京 | とうきょう|ＴＯＫＹＯ | tokyo "),
            ("東京", "とうきょう", "tokyo"),
        )

    def test_rejects_empty_answer_candidates(self) -> None:
        for value in ("", "  ", "foo|", "|foo", "foo||bar"):
            with self.subTest(value=value), self.assertRaises(AnswerValidationError):
                prepare_answers(value)

    def test_matching_is_normalized_complete_match(self) -> None:
        candidates = prepare_answers("Blue Whale | シロナガスクジラ")
        self.assertTrue(answer_matches("ｂｌｕｅ　ｗｈａｌｅ", candidates))
        self.assertTrue(
            answer_matches(" シロナガスジラ ".replace("スジラ", "スクジラ"), candidates)
        )
        self.assertFalse(answer_matches("blue", candidates))

    def test_validates_mode(self) -> None:
        self.assertEqual(validate_mode("BRIDDLE"), "briddle")
        self.assertEqual(validate_mode("riddle"), "riddle")
        with self.assertRaises(ModeValidationError):
            validate_mode("quiz")

    def test_validates_answer_limit(self) -> None:
        self.assertIsNone(validate_answer_limit(None))
        self.assertEqual(validate_answer_limit(1), 1)
        self.assertEqual(validate_answer_limit(MAX_ANSWER_LIMIT), MAX_ANSWER_LIMIT)

        for value in (0, -1, MAX_ANSWER_LIMIT + 1, True, 1.5, "3"):
            with (
                self.subTest(value=value),
                self.assertRaises(AnswerLimitValidationError),
            ):
                validate_answer_limit(value)  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
