from __future__ import annotations

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier

from riddle_bot.database import (
    ActiveRiddleLimitError,
    DiscordBindingError,
    RiddleDatabase,
    SubmissionStatus,
)


class RiddleDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.database_path = Path(self.temporary_directory.name) / "riddles.db"
        self.database = RiddleDatabase(self.database_path)
        self.now = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)

    def create_riddle(
        self,
        *,
        mode: str = "riddle",
        creator_id: int = 100,
        guild_id: int = 1,
        channel_id: int = 10,
        thread_id: int | None = None,
        message_id: int | None = None,
        answer_display: str = "Blue Whale | シロナガスクジラ",
        deadline_at: datetime | None = None,
        public_answers: bool = False,
        answer_limit: int | None = None,
    ):
        return self.database.create_riddle(
            guild_id=guild_id,
            channel_id=channel_id,
            creator_id=creator_id,
            mode=mode,
            question="世界で一番大きな動物は？",
            answer_display=answer_display,
            deadline_at=deadline_at or self.now + timedelta(hours=1),
            thread_id=thread_id,
            message_id=message_id,
            created_at=self.now,
            public_answers=public_answers,
            answer_limit=answer_limit,
        )

    def test_uses_wal_and_persists_settings_between_instances(self) -> None:
        defaults = self.database.get_guild_settings(1)
        self.assertIsNone(defaults.allowed_channel_id)
        self.assertEqual(defaults.max_active, 10)
        self.assertTrue(defaults.mention_winners)

        personal_defaults = self.database.get_user_preferences(1, 100)
        self.assertFalse(personal_defaults.public_answers)
        self.assertIsNone(personal_defaults.answer_limit)

        updated = self.database.update_guild_settings(
            1,
            allowed_channel_id=99,
            max_active=3,
            mention_winners=False,
        )
        self.assertEqual(updated.allowed_channel_id, 99)
        self.assertEqual(updated.max_active, 3)
        self.assertFalse(updated.mention_winners)

        reopened = RiddleDatabase(self.database_path)
        self.assertEqual(reopened.get_guild_settings(1), updated)
        self.assertEqual(reopened.list_guild_ids(), [1])

        with closing(sqlite3.connect(self.database_path)) as connection:
            journal_mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        self.assertEqual(journal_mode.lower(), "wal")

    def test_user_preferences_are_per_guild_and_persist(self) -> None:
        updated = self.database.update_user_preferences(
            1,
            100,
            public_answers=True,
            answer_limit=3,
        )
        self.assertTrue(updated.public_answers)
        self.assertEqual(updated.answer_limit, 3)
        self.assertEqual(self.database.list_guild_ids(), [1])

        unlimited = self.database.update_user_preferences(
            1,
            100,
            answer_limit=None,
        )
        self.assertTrue(unlimited.public_answers)
        self.assertIsNone(unlimited.answer_limit)
        self.assertEqual(
            self.database.get_user_preferences(1, 101).answer_limit,
            None,
        )
        self.assertFalse(
            self.database.get_user_preferences(2, 100).public_answers,
        )

        reopened = RiddleDatabase(self.database_path)
        self.assertEqual(reopened.get_user_preferences(1, 100), unlimited)

        for invalid_limit in (0, -1, 101, True):
            with (
                self.subTest(invalid_limit=invalid_limit),
                self.assertRaises(ValueError),
            ):
                self.database.update_user_preferences(
                    1,
                    100,
                    answer_limit=invalid_limit,
                )
        with self.assertRaises(TypeError):
            self.database.update_user_preferences(
                1,
                100,
                public_answers=1,  # type: ignore[arg-type]
            )

    def test_existing_v1_database_is_migrated_without_losing_riddles(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                CREATE TABLE riddles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    message_id INTEGER,
                    creator_id INTEGER NOT NULL,
                    mode TEXT NOT NULL,
                    question TEXT NOT NULL,
                    answer_display TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    winner_id INTEGER,
                    created_at TEXT NOT NULL
                );
                INSERT INTO riddles (
                    guild_id, channel_id, creator_id, mode, question,
                    answer_display, answers_json, deadline_at, status, created_at
                )
                VALUES (
                    1, 10, 100, 'riddle', '旧問題', '答え', '["答え"]',
                    '2026-07-25T13:00:00.000000Z', 'active',
                    '2026-07-25T12:00:00.000000Z'
                );
                """
            )

        migrated = RiddleDatabase(legacy_path)
        riddle = migrated.get_riddle(1)
        self.assertIsNotNone(riddle)
        self.assertEqual(riddle.question, "旧問題")
        self.assertFalse(riddle.public_answers)
        self.assertIsNone(riddle.answer_limit)

        with closing(sqlite3.connect(legacy_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(riddles)")
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue({"public_answers", "answer_limit"} <= columns)
        self.assertEqual(version, 2)

    def test_create_bind_lookup_list_and_limit(self) -> None:
        self.database.update_guild_settings(1, max_active=1)
        first = self.create_riddle()
        self.assertEqual(first.normalized_answers, ("bluewhale", "シロナガスクジラ"))
        self.assertEqual(self.database.count_active_riddles(1, creator_id=100), 1)

        with self.assertRaises(ActiveRiddleLimitError):
            self.create_riddle()

        other_creator = self.create_riddle(creator_id=101)
        self.assertEqual(len(self.database.list_active_riddles(1)), 2)
        self.assertEqual(
            self.database.list_active_riddles(1, creator_id=101),
            [other_creator],
        )

        bound = self.database.bind_discord_ids(
            first.id,
            thread_id=200,
            message_id=300,
        )
        self.assertEqual(bound.thread_id, 200)
        self.assertEqual(bound.message_id, 300)
        self.assertEqual(self.database.get_riddle_by_thread(200), bound)
        self.assertEqual(self.database.get_riddle_by_message(300), bound)
        self.assertEqual(
            self.database.bind_discord_ids(first.id, thread_id=200),
            bound,
        )
        with self.assertRaises(DiscordBindingError):
            self.database.bind_discord_ids(first.id, thread_id=201)

    def test_riddle_stores_resolved_public_and_answer_limit_settings(self) -> None:
        riddle = self.create_riddle(public_answers=True, answer_limit=4)
        self.assertTrue(riddle.public_answers)
        self.assertEqual(riddle.answer_limit, 4)
        self.assertEqual(self.database.get_riddle(riddle.id), riddle)

        for invalid_limit in (0, -1, 101, True):
            with (
                self.subTest(invalid_limit=invalid_limit),
                self.assertRaises(ValueError),
            ):
                self.create_riddle(answer_limit=invalid_limit)
        with self.assertRaises(TypeError):
            self.create_riddle(public_answers=1)  # type: ignore[arg-type]

    def test_briddle_accepts_exactly_one_concurrent_winner(self) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            thread_id=200,
            message_id=300,
        )
        barrier = Barrier(2)

        def submit(user_id: int):
            barrier.wait()
            return self.database.submit_answer_by_message(
                300,
                user_id,
                "ＢＬＵＥ　ＷＨＡＬＥ",
                now=self.now + timedelta(minutes=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, (501, 502)))

        statuses = sorted(result.status.value for result in results)
        self.assertEqual(statuses, ["closed", "correct"])
        winners = self.database.list_winner_ids(riddle.id)
        self.assertEqual(len(winners), 1)

        stored = self.database.get_riddle(riddle.id)
        self.assertEqual(stored.status, "solved")
        self.assertEqual(stored.winner_id, winners[0])
        self.assertEqual(self.database.count_active_riddles(1), 0)
        self.assertEqual(self.database.list_pending_finalizations(), [stored])

        self.assertEqual(self.database.delete_finished_riddle(riddle.id), stored)
        self.assertIsNone(self.database.get_riddle(riddle.id))

    def test_active_delete_racing_correct_answer_never_deletes_solved_row(self) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            thread_id=200,
            message_id=300,
        )
        barrier = Barrier(2)

        def delete():
            barrier.wait()
            return self.database.delete_active_riddle(riddle.id)

        def submit():
            barrier.wait()
            return self.database.submit_answer(
                riddle.id,
                501,
                "blue whale",
                now=self.now + timedelta(minutes=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            delete_future = executor.submit(delete)
            submit_future = executor.submit(submit)
            deleted = delete_future.result()
            submitted = submit_future.result()

        if deleted is not None:
            self.assertEqual(deleted.status, "active")
            self.assertEqual(submitted.status, SubmissionStatus.NOT_FOUND)
            self.assertIsNone(self.database.get_riddle(riddle.id))
        else:
            self.assertEqual(submitted.status, SubmissionStatus.CORRECT)
            stored = self.database.get_riddle(riddle.id)
            self.assertIsNotNone(stored)
            self.assertEqual(stored.status, "solved")
            self.assertEqual(self.database.list_pending_finalizations(), [stored])

    def test_active_delete_refuses_a_problem_that_is_already_solved(self) -> None:
        riddle = self.create_riddle(mode="briddle")
        result = self.database.submit_answer(
            riddle.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(result.status, SubmissionStatus.CORRECT)
        self.assertIsNone(self.database.delete_active_riddle(riddle.id))
        self.assertEqual(self.database.get_riddle(riddle.id).status, "solved")

    def test_riddle_records_unique_correct_users_without_saving_wrong_text(
        self,
    ) -> None:
        riddle = self.create_riddle(message_id=300)
        secret_wrong_answer = "definitely-wrong-secret"

        wrong = self.database.submit_answer_by_message(
            300,
            501,
            secret_wrong_answer,
            now=self.now + timedelta(minutes=1),
        )
        first = self.database.submit_answer(
            riddle.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=2),
        )
        duplicate = self.database.submit_answer_by_message(
            300,
            501,
            "BLUEWHALE",
            now=self.now + timedelta(minutes=3),
        )
        second = self.database.submit_answer(
            riddle.id,
            502,
            "シロナガスクジラ",
            now=self.now + timedelta(minutes=4),
        )

        self.assertEqual(wrong.status, SubmissionStatus.WRONG)
        self.assertEqual(first.status, SubmissionStatus.CORRECT)
        self.assertEqual(duplicate.status, SubmissionStatus.ALREADY_CORRECT)
        self.assertEqual(second.status, SubmissionStatus.CORRECT)
        self.assertEqual(second.winner_ids, (501, 502))
        self.assertEqual(self.database.get_riddle(riddle.id).status, "active")
        self.assertEqual(self.database.get_answer_attempt_count(riddle.id, 501), 3)
        self.assertEqual(self.database.get_answer_attempt_count(riddle.id, 502), 1)

        with closing(sqlite3.connect(self.database_path)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(secret_wrong_answer, dump)

    def test_answer_limit_counts_each_accepted_submission_per_user(self) -> None:
        riddle = self.create_riddle(
            message_id=300,
            public_answers=True,
            answer_limit=2,
        )
        secret_wrong_answer = "first-private-guess"

        wrong = self.database.submit_answer(
            riddle.id,
            501,
            secret_wrong_answer,
            now=self.now + timedelta(minutes=1),
        )
        correct = self.database.submit_answer(
            riddle.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=2),
        )
        rejected = self.database.submit_answer(
            riddle.id,
            501,
            "シロナガスクジラ",
            now=self.now + timedelta(minutes=3),
        )
        other_user = self.database.submit_answer(
            riddle.id,
            502,
            "wrong",
            now=self.now + timedelta(minutes=4),
        )

        self.assertEqual(wrong.status, SubmissionStatus.WRONG)
        self.assertTrue(wrong.attempt_recorded)
        self.assertEqual(wrong.attempt_count, 1)
        self.assertEqual(wrong.remaining_attempts, 1)
        self.assertEqual(correct.status, SubmissionStatus.CORRECT)
        self.assertEqual(correct.attempt_count, 2)
        self.assertEqual(correct.remaining_attempts, 0)
        self.assertEqual(rejected.status, SubmissionStatus.LIMIT_REACHED)
        self.assertFalse(rejected.attempt_recorded)
        self.assertEqual(rejected.attempt_count, 2)
        self.assertEqual(rejected.remaining_attempts, 0)
        self.assertEqual(other_user.status, SubmissionStatus.WRONG)
        self.assertEqual(other_user.attempt_count, 1)
        self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))

        with closing(sqlite3.connect(self.database_path)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(secret_wrong_answer, dump)

    def test_answer_limit_last_slot_is_atomic_for_same_user(self) -> None:
        riddle = self.create_riddle(answer_limit=1)
        barrier = Barrier(2)

        def submit(answer: str):
            barrier.wait()
            return self.database.submit_answer(
                riddle.id,
                501,
                answer,
                now=self.now + timedelta(minutes=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, ("wrong-one", "wrong-two")))

        self.assertEqual(
            sorted(result.status.value for result in results),
            ["limit_reached", "wrong"],
        )
        self.assertEqual(self.database.get_answer_attempt_count(riddle.id, 501), 1)
        with closing(sqlite3.connect(self.database_path)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn("wrong-one", dump)
        self.assertNotIn("wrong-two", dump)

    def test_due_riddle_is_expired_and_survives_restart_until_finalized(self) -> None:
        riddle = self.create_riddle(deadline_at=self.now + timedelta(minutes=1))
        before = self.database.list_due_riddles(self.now)
        self.assertEqual(before, [])
        due = self.database.list_due_riddles(self.now + timedelta(minutes=1))
        self.assertEqual(due, [riddle])

        expired = self.database.mark_riddle_expired(
            riddle.id,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(expired.status, "expired")

        reopened = RiddleDatabase(self.database_path)
        self.assertEqual(reopened.list_pending_finalizations(), [expired])
        self.assertEqual(reopened.delete_finished_riddle(riddle.id), expired)

    def test_late_submission_atomically_marks_problem_expired(self) -> None:
        riddle = self.create_riddle(deadline_at=self.now + timedelta(minutes=1))
        result = self.database.submit_answer(
            riddle.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(result.status, SubmissionStatus.EXPIRED)
        self.assertEqual(result.riddle.status, "expired")
        self.assertEqual(result.winner_ids, ())

    def test_user_summary_and_deletion_remove_all_user_references(self) -> None:
        owned = self.create_riddle(creator_id=501)
        answered = self.create_riddle(creator_id=502, message_id=300)
        self.database.update_user_preferences(
            1,
            501,
            public_answers=True,
            answer_limit=2,
        )
        self.database.submit_answer(
            answered.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )

        summary = self.database.get_user_data_summary(1, 501)
        self.assertEqual(summary.created_riddles, 1)
        self.assertEqual(summary.active_created_riddles, 1)
        self.assertEqual(summary.correct_riddles, 1)
        self.assertEqual(summary.first_wins, 1)
        self.assertEqual(summary.answer_attempts, 1)
        self.assertTrue(summary.has_preferences)

        deletion = self.database.delete_user_data(1, 501)
        self.assertEqual(deletion.deleted_riddles, 1)
        self.assertEqual(deletion.deleted_winner_entries, 1)
        self.assertEqual(deletion.deleted_answer_attempts, 1)
        self.assertTrue(deletion.deleted_preferences)
        self.assertIsNone(self.database.get_riddle(owned.id))
        remaining = self.database.get_riddle(answered.id)
        self.assertIsNotNone(remaining)
        self.assertIsNone(remaining.winner_id)
        self.assertEqual(self.database.list_winner_ids(answered.id), ())
        self.assertEqual(self.database.get_answer_attempt_count(answered.id, 501), 0)
        self.assertFalse(
            self.database.get_user_preferences(1, 501).public_answers,
        )
        self.assertEqual(
            self.database.get_user_data_summary(1, 501).created_riddles,
            0,
        )

    def test_lists_every_status_created_by_user_for_discord_cleanup(self) -> None:
        active = self.create_riddle(creator_id=501)
        solved = self.create_riddle(
            mode="briddle",
            creator_id=501,
            message_id=301,
        )
        self.database.submit_answer(
            solved.id,
            601,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )
        expired = self.create_riddle(
            creator_id=501,
            deadline_at=self.now + timedelta(minutes=1),
        )
        self.database.mark_riddle_expired(
            expired.id,
            now=self.now + timedelta(minutes=1),
        )
        self.create_riddle(creator_id=502)
        self.create_riddle(creator_id=501, guild_id=2)

        self.assertEqual(
            self.database.list_riddles_by_creator(1, 501),
            [
                active,
                self.database.get_riddle(solved.id),
                self.database.get_riddle(expired.id),
            ],
        )

    def test_delete_guild_data_removes_settings_riddles_and_winners(self) -> None:
        self.database.update_guild_settings(1, allowed_channel_id=10)
        self.database.update_user_preferences(
            1,
            501,
            public_answers=True,
            answer_limit=3,
        )
        riddle = self.create_riddle()
        self.database.submit_answer(
            riddle.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )

        deletion = self.database.delete_guild_data(1)
        self.assertEqual(deletion.deleted_riddles, 1)
        self.assertEqual(deletion.deleted_winner_entries, 1)
        self.assertEqual(deletion.deleted_answer_attempts, 1)
        self.assertEqual(deletion.deleted_user_preferences, 1)
        self.assertTrue(deletion.deleted_settings)
        self.assertEqual(self.database.list_guild_ids(), [])
        self.assertIsNone(self.database.get_riddle(riddle.id))

    def test_manual_delete_can_remove_an_active_problem(self) -> None:
        riddle = self.create_riddle()
        self.assertIsNone(self.database.delete_finished_riddle(riddle.id))
        self.assertEqual(self.database.delete_riddle(riddle.id), riddle)
        self.assertIsNone(self.database.get_riddle(riddle.id))


if __name__ == "__main__":
    unittest.main()
