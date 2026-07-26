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
    DeferredAnswer,
    DiscordBindingError,
    ManualAcceptanceStatus,
    ManualCloseStatus,
    RiddleDatabase,
    SubmissionStatus,
    UnsupportedSchemaVersionError,
    emoji_identity_key,
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
        manual_judging: bool = False,
        winner_limit: int = 1,
        wrong_answer_visibility: str = "private",
        answer_accept_emoji: str = "✅",
        ogiri_mvp_emoji: str = "👑",
        good_question_emoji: str = "⭐",
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
            manual_judging=manual_judging,
            winner_limit=winner_limit,
            wrong_answer_visibility=wrong_answer_visibility,
            answer_accept_emoji=answer_accept_emoji,
            ogiri_mvp_emoji=ogiri_mvp_emoji,
            good_question_emoji=good_question_emoji,
        )

    def test_uses_wal_and_persists_settings_between_instances(self) -> None:
        defaults = self.database.get_guild_settings(1)
        self.assertIsNone(defaults.allowed_channel_id)
        self.assertEqual(defaults.max_active, 10)
        self.assertTrue(defaults.mention_winners)
        self.assertEqual(defaults.answer_accept_emoji, "✅")
        self.assertEqual(defaults.ogiri_mvp_emoji, "👑")
        self.assertEqual(defaults.good_question_emoji, "⭐")

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

    def test_guild_evaluation_emojis_are_distinct_and_persistent(self) -> None:
        updated = self.database.update_guild_settings(
            1,
            answer_accept_emoji="☑️",
            ogiri_mvp_emoji="🏅",
            good_question_emoji="💯",
        )

        self.assertEqual(updated.answer_accept_emoji, "☑️")
        self.assertEqual(updated.ogiri_mvp_emoji, "🏅")
        self.assertEqual(updated.good_question_emoji, "💯")
        self.assertEqual(
            RiddleDatabase(self.database_path).get_guild_settings(1),
            updated,
        )
        with self.assertRaisesRegex(ValueError, "それぞれ別"):
            self.database.update_guild_settings(
                1,
                ogiri_mvp_emoji="☑️",
            )
        with self.assertRaisesRegex(ValueError, "それぞれ別"):
            self.database.update_guild_settings(
                1,
                ogiri_mvp_emoji="☑",
            )

        custom_emoji_id = "123456789012345"
        self.database.update_guild_settings(
            1,
            answer_accept_emoji=f"<:old_name:{custom_emoji_id}>",
        )
        with self.assertRaisesRegex(ValueError, "それぞれ別"):
            self.database.update_guild_settings(
                1,
                ogiri_mvp_emoji=f"<a:new_name:{custom_emoji_id}>",
            )
        with self.assertRaisesRegex(ValueError, "公式記録マーク"):
            self.database.update_guild_settings(
                1,
                good_question_emoji="🏆",
            )
        with self.assertRaisesRegex(ValueError, "公式記録マーク"):
            self.database.update_guild_settings(
                1,
                good_question_emoji="🏆️",
            )

    def test_emoji_identity_key_matches_discord_reaction_identity(self) -> None:
        custom_emoji_id = "123456789012345"
        self.assertEqual(
            emoji_identity_key(f"<:old_name:{custom_emoji_id}>"),
            emoji_identity_key(f"<a:new_name:{custom_emoji_id}>"),
        )
        self.assertEqual(
            emoji_identity_key("☑"),
            emoji_identity_key("☑️"),
        )
        self.assertNotEqual(
            emoji_identity_key("✅"),
            emoji_identity_key("⭐"),
        )

    def test_riddle_snapshot_rejects_semantically_duplicate_emojis(self) -> None:
        custom_emoji_id = "123456789012345"
        with self.assertRaisesRegex(ValueError, "それぞれ別"):
            self.create_riddle(
                answer_accept_emoji=f"<:old_name:{custom_emoji_id}>",
                ogiri_mvp_emoji=f"<a:new_name:{custom_emoji_id}>",
            )

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
        reopened = RiddleDatabase(legacy_path)
        riddle = reopened.get_riddle(1)
        self.assertIsNotNone(riddle)
        self.assertEqual(riddle.question, "旧問題")
        self.assertFalse(riddle.public_answers)
        self.assertIsNone(riddle.answer_limit)
        self.assertFalse(riddle.manual_judging)
        self.assertEqual(riddle.winner_limit, 1)
        self.assertEqual(riddle.wrong_answer_visibility, "private")
        self.assertIsNone(riddle.closed_early_at)
        self.assertIsNone(riddle.finalized_at)
        self.assertEqual(migrated.get_riddle(1), riddle)

        with closing(sqlite3.connect(legacy_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(riddles)")
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue(
            {
                "public_answers",
                "answer_limit",
                "manual_judging",
                "answer_accept_emoji",
                "ogiri_mvp_emoji",
                "good_question_emoji",
                "winner_limit",
                "wrong_answer_visibility",
                "closed_early_at",
                "finalized_at",
            }
            <= columns
        )
        self.assertEqual(version, 5)

    def test_existing_v2_database_is_migrated_idempotently(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy-v2.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                PRAGMA user_version = 2;
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
                    created_at TEXT NOT NULL,
                    public_answers INTEGER NOT NULL DEFAULT 0,
                    answer_limit INTEGER
                );
                CREATE TABLE riddle_winners (
                    riddle_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    answered_at TEXT NOT NULL,
                    PRIMARY KEY (riddle_id, user_id)
                );
                CREATE TABLE riddle_attempts (
                    riddle_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    last_answered_at TEXT NOT NULL,
                    PRIMARY KEY (riddle_id, user_id)
                );
                CREATE TABLE user_preferences (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    public_answers INTEGER NOT NULL DEFAULT 0,
                    answer_limit INTEGER,
                    PRIMARY KEY (guild_id, user_id)
                );
                INSERT INTO riddles (
                    guild_id, channel_id, thread_id, message_id, creator_id,
                    mode, question, answer_display, answers_json, deadline_at,
                    status, winner_id, created_at, public_answers, answer_limit
                )
                VALUES (
                    1, 10, 20, 30, 100, 'riddle', 'V2問題', '答え', '["答え"]',
                    '2026-07-25T13:00:00.000000Z', 'active', 501,
                    '2026-07-25T12:00:00.000000Z', 1, 3
                );
                INSERT INTO riddle_winners
                VALUES (1, 501, '2026-07-25T12:10:00.000000Z');
                INSERT INTO riddle_attempts
                VALUES (1, 501, 2, '2026-07-25T12:11:00.000000Z');
                INSERT INTO user_preferences VALUES (1, 100, 1, 3);
                """
            )

        migrated = RiddleDatabase(legacy_path)
        reopened = RiddleDatabase(legacy_path)
        riddle = reopened.get_riddle(1)

        self.assertEqual(riddle.question, "V2問題")
        self.assertTrue(riddle.public_answers)
        self.assertEqual(riddle.answer_limit, 3)
        self.assertFalse(riddle.manual_judging)
        self.assertEqual(riddle.answer_accept_emoji, "✅")
        self.assertEqual(riddle.ogiri_mvp_emoji, "👑")
        self.assertEqual(riddle.good_question_emoji, "⭐")
        self.assertEqual(riddle.winner_limit, 1)
        self.assertEqual(riddle.wrong_answer_visibility, "private")
        self.assertIsNone(riddle.closed_early_at)
        self.assertIsNone(riddle.finalized_at)
        self.assertEqual(reopened.list_winner_ids(1), (501,))
        self.assertEqual(reopened.get_answer_attempt_count(1, 501), 2)
        self.assertTrue(reopened.get_user_preferences(1, 100).public_answers)
        self.assertEqual(migrated.get_riddle(1), riddle)
        with closing(sqlite3.connect(legacy_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            mapping_table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'riddle_answer_messages'
                """
            ).fetchone()
        self.assertEqual(version, 5)
        self.assertIsNotNone(mapping_table)

    def test_existing_v3_database_is_migrated_to_v5_idempotently(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy-v3.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                PRAGMA user_version = 3;
                CREATE TABLE guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    allowed_channel_id INTEGER,
                    max_active INTEGER NOT NULL DEFAULT 10,
                    mention_winners INTEGER NOT NULL DEFAULT 1,
                    answer_accept_emoji TEXT NOT NULL DEFAULT '✅',
                    ogiri_mvp_emoji TEXT NOT NULL DEFAULT '👑',
                    good_question_emoji TEXT NOT NULL DEFAULT '⭐'
                );
                INSERT INTO guild_settings
                VALUES (1, 10, 7, 0, '✅', '👑', '⭐');
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
                    created_at TEXT NOT NULL,
                    public_answers INTEGER NOT NULL DEFAULT 0,
                    answer_limit INTEGER,
                    manual_judging INTEGER NOT NULL DEFAULT 0,
                    answer_accept_emoji TEXT NOT NULL DEFAULT '✅',
                    ogiri_mvp_emoji TEXT NOT NULL DEFAULT '👑',
                    good_question_emoji TEXT NOT NULL DEFAULT '⭐'
                );
                INSERT INTO riddles (
                    guild_id, channel_id, creator_id, mode, question,
                    answer_display, answers_json, deadline_at, status,
                    created_at
                )
                VALUES (
                    1, 10, 100, 'briddle', 'V3問題', '答え', '["答え"]',
                    '2026-07-25T13:00:00.000000Z', 'active',
                    '2026-07-25T12:00:00.000000Z'
                );
                """
            )

        migrated = RiddleDatabase(legacy_path)
        reopened = RiddleDatabase(legacy_path)
        riddle = reopened.get_riddle(1)

        self.assertEqual(riddle.question, "V3問題")
        self.assertEqual(riddle.winner_limit, 1)
        self.assertEqual(riddle.wrong_answer_visibility, "private")
        self.assertIsNone(riddle.closed_early_at)
        self.assertIsNone(riddle.finalized_at)
        self.assertEqual(reopened.list_pending_deferred_answers(1), [])
        settings = reopened.get_guild_settings(1)
        self.assertEqual(settings.allowed_channel_id, 10)
        self.assertEqual(settings.max_active, 7)
        self.assertFalse(settings.mention_winners)
        self.assertEqual(migrated.get_riddle(1), riddle)
        with closing(sqlite3.connect(legacy_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            deferred_table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'riddle_deferred_answers'
                """
            ).fetchone()
        self.assertEqual(version, 5)
        self.assertIsNotNone(deferred_table)

    def test_existing_v4_database_is_migrated_to_v5_idempotently(self) -> None:
        legacy_path = Path(self.temporary_directory.name) / "legacy-v4.db"
        with closing(sqlite3.connect(legacy_path)) as connection:
            connection.executescript(
                """
                PRAGMA user_version = 4;
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
                    created_at TEXT NOT NULL,
                    public_answers INTEGER NOT NULL DEFAULT 0,
                    answer_limit INTEGER,
                    manual_judging INTEGER NOT NULL DEFAULT 0,
                    answer_accept_emoji TEXT NOT NULL DEFAULT '✅',
                    ogiri_mvp_emoji TEXT NOT NULL DEFAULT '👑',
                    good_question_emoji TEXT NOT NULL DEFAULT '⭐',
                    winner_limit INTEGER NOT NULL DEFAULT 1,
                    wrong_answer_visibility TEXT NOT NULL DEFAULT 'private'
                );
                INSERT INTO riddles (
                    guild_id, channel_id, thread_id, message_id, creator_id,
                    mode, question, answer_display, answers_json, deadline_at,
                    status, winner_id, created_at, public_answers, answer_limit,
                    manual_judging, answer_accept_emoji, ogiri_mvp_emoji,
                    good_question_emoji, winner_limit, wrong_answer_visibility
                )
                VALUES (
                    9, 90, 900, 901, 902, 'briddle', 'V4問題', 'V4答え',
                    '["v4答え"]', '2026-07-25T13:00:00.000000Z', 'active',
                    NULL, '2026-07-25T12:00:00.000000Z', 0, 7, 0,
                    '✅', '👑', '⭐', 4, 'after_close'
                );
                """
            )

        migrated = RiddleDatabase(legacy_path)
        reopened = RiddleDatabase(legacy_path)
        riddle = reopened.get_riddle(1)

        self.assertEqual(riddle.question, "V4問題")
        self.assertEqual(riddle.guild_id, 9)
        self.assertEqual(riddle.thread_id, 900)
        self.assertEqual(riddle.message_id, 901)
        self.assertEqual(riddle.answer_limit, 7)
        self.assertEqual(riddle.winner_limit, 4)
        self.assertEqual(riddle.wrong_answer_visibility, "after_close")
        self.assertIsNone(riddle.closed_early_at)
        self.assertIsNone(riddle.finalized_at)
        self.assertEqual(migrated.get_riddle(1), riddle)
        with closing(sqlite3.connect(legacy_path)) as connection:
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(riddles)")
            }
            version = connection.execute("PRAGMA user_version").fetchone()[0]
        self.assertTrue({"closed_early_at", "finalized_at"} <= columns)
        self.assertEqual(version, 5)

    def test_future_schema_version_is_rejected_without_downgrade(self) -> None:
        future_path = Path(self.temporary_directory.name) / "future.db"
        with closing(sqlite3.connect(future_path)) as connection:
            connection.executescript(
                """
                PRAGMA user_version = 6;
                CREATE TABLE future_only (
                    id INTEGER PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT INTO future_only VALUES (1, 'preserve-me');
                """
            )

        with self.assertRaisesRegex(
            UnsupportedSchemaVersionError,
            r"DB: v6 / 対応: v5",
        ):
            RiddleDatabase(future_path)

        with closing(sqlite3.connect(future_path)) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            preserved = connection.execute(
                "SELECT value FROM future_only WHERE id = 1"
            ).fetchone()[0]
            riddles_table = connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'table' AND name = 'riddles'
                """
            ).fetchone()
        self.assertEqual(version, 6)
        self.assertEqual(preserved, "preserve-me")
        self.assertIsNone(riddles_table)

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

    def test_briddle_stores_and_validates_winner_and_wrong_answer_settings(
        self,
    ) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            winner_limit=100,
            wrong_answer_visibility=" AFTER_CLOSE ",
        )

        self.assertEqual(riddle.winner_limit, 100)
        self.assertEqual(riddle.wrong_answer_visibility, "after_close")
        self.assertEqual(self.database.get_riddle(riddle.id), riddle)

        for invalid_limit in (0, -1, 101, True, "2"):
            with (
                self.subTest(invalid_limit=invalid_limit),
                self.assertRaises(ValueError),
            ):
                self.create_riddle(
                    mode="briddle",
                    winner_limit=invalid_limit,  # type: ignore[arg-type]
                )
        with self.assertRaises(ValueError):
            self.create_riddle(
                mode="briddle",
                wrong_answer_visibility="later",
            )
        with self.assertRaises(TypeError):
            self.create_riddle(
                mode="briddle",
                wrong_answer_visibility=None,  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "briddle"):
            self.create_riddle(mode="riddle", winner_limit=2)
        with self.assertRaisesRegex(ValueError, "briddle"):
            self.create_riddle(
                mode="riddle",
                wrong_answer_visibility="immediate",
            )

    def test_manual_judging_requires_public_riddle_and_has_no_fixed_answer(
        self,
    ) -> None:
        riddle = self.database.create_riddle(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            mode="riddle",
            question="写真で一言",
            answer_display="",
            deadline_at=self.now + timedelta(hours=1),
            created_at=self.now,
            public_answers=True,
            answer_limit=2,
            manual_judging=True,
        )

        self.assertTrue(riddle.manual_judging)
        self.assertEqual(riddle.normalized_answers, ())
        result = self.database.submit_answer(
            riddle.id,
            501,
            "これは面白い回答",
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(result.status, SubmissionStatus.WRONG)
        self.assertEqual(self.database.list_winner_ids(riddle.id), ())

        with self.assertRaisesRegex(ValueError, "回答公開"):
            self.database.create_riddle(
                guild_id=1,
                channel_id=10,
                creator_id=101,
                mode="riddle",
                question="非公開にはできない",
                answer_display="",
                deadline_at=self.now + timedelta(hours=1),
                public_answers=False,
                manual_judging=True,
            )

    def test_public_answer_mapping_and_creator_acceptance_are_persistent(
        self,
    ) -> None:
        riddle = self.create_riddle(public_answers=True)
        secret_submission = "DBには残してはいけない珍回答"
        result = self.database.submit_answer(
            riddle.id,
            501,
            secret_submission,
            now=self.now + timedelta(minutes=1),
        )
        self.assertEqual(result.status, SubmissionStatus.WRONG)
        mapping = self.database.register_public_answer_message(
            message_id=900,
            riddle_id=riddle.id,
            user_id=501,
            attempt_count=1,
            posted_at=self.now + timedelta(minutes=1),
        )
        self.assertEqual(mapping.message_id, 900)
        self.assertIsNone(mapping.accepted_at)

        forbidden = self.database.accept_public_answer_message(
            900,
            999,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(forbidden.status, ManualAcceptanceStatus.FORBIDDEN)
        accepted = self.database.accept_public_answer_message(
            900,
            riddle.creator_id,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(accepted.status, ManualAcceptanceStatus.ACCEPTED)
        self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))
        self.assertEqual(
            self.database.get_first_winner_at(riddle.id),
            self.now + timedelta(minutes=1),
        )
        first_accepted_at = accepted.answer_message.accepted_at

        repeated = self.database.accept_public_answer_message(
            900,
            riddle.creator_id,
            now=self.now + timedelta(minutes=3),
        )
        self.assertEqual(
            repeated.status,
            ManualAcceptanceStatus.ALREADY_ACCEPTED,
        )
        self.assertEqual(repeated.answer_message.accepted_at, first_accepted_at)
        self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))

        reopened = RiddleDatabase(self.database_path)
        self.assertEqual(
            reopened.get_public_answer_message(900), repeated.answer_message
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(secret_submission, dump)

    def test_ogiri_mvp_is_creator_only_and_idempotent(self) -> None:
        riddle = self.database.create_riddle(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            mode="riddle",
            question="写真で一言",
            answer_display="",
            deadline_at=self.now + timedelta(hours=1),
            created_at=self.now,
            public_answers=True,
            manual_judging=True,
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "MVP候補",
            now=self.now + timedelta(minutes=1),
        )
        self.database.register_public_answer_message(
            message_id=910,
            riddle_id=riddle.id,
            user_id=501,
            attempt_count=1,
        )

        forbidden = self.database.mark_public_answer_mvp(910, 999, now=self.now)
        self.assertEqual(forbidden.status, ManualAcceptanceStatus.FORBIDDEN)
        selected = self.database.mark_public_answer_mvp(
            910,
            riddle.creator_id,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(selected.status, ManualAcceptanceStatus.MVP_MARKED)
        self.assertIsNotNone(selected.answer_message.mvp_at)
        self.assertEqual(self.database.list_mvp_user_ids(riddle.id), (501,))
        self.assertEqual(self.database.list_winner_ids(riddle.id), ())

        repeated = self.database.mark_public_answer_mvp(
            910,
            riddle.creator_id,
            now=self.now + timedelta(minutes=3),
        )
        self.assertEqual(repeated.status, ManualAcceptanceStatus.ALREADY_MVP)
        self.assertEqual(self.database.list_mvp_user_ids(riddle.id), (501,))
        self.assertEqual(self.database.list_winner_ids(riddle.id), ())

        accepted = self.database.accept_public_answer_message(
            910,
            riddle.creator_id,
            now=self.now + timedelta(minutes=4),
        )
        self.assertEqual(accepted.status, ManualAcceptanceStatus.ACCEPTED)
        self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))

        normal_riddle = self.create_riddle(
            creator_id=101,
            public_answers=True,
        )
        self.database.submit_answer(
            normal_riddle.id,
            502,
            "不正解",
            now=self.now + timedelta(minutes=1),
        )
        self.database.register_public_answer_message(
            message_id=911,
            riddle_id=normal_riddle.id,
            user_id=502,
            attempt_count=1,
        )
        not_ogiri = self.database.mark_public_answer_mvp(
            911,
            normal_riddle.creator_id,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(not_ogiri.status, ManualAcceptanceStatus.NOT_OGIRI)

    def test_public_answer_mapping_rejects_invalid_or_duplicate_bindings(self) -> None:
        private_riddle = self.create_riddle()
        self.database.submit_answer(
            private_riddle.id,
            501,
            "不正解",
            now=self.now + timedelta(minutes=1),
        )
        with self.assertRaisesRegex(Exception, "公開回答"):
            self.database.register_public_answer_message(
                message_id=900,
                riddle_id=private_riddle.id,
                user_id=501,
                attempt_count=1,
            )

        public_riddle = self.create_riddle(
            creator_id=101,
            public_answers=True,
        )
        self.database.submit_answer(
            public_riddle.id,
            501,
            "不正解",
            now=self.now + timedelta(minutes=1),
        )
        self.database.register_public_answer_message(
            message_id=901,
            riddle_id=public_riddle.id,
            user_id=501,
            attempt_count=1,
        )
        with self.assertRaises(DiscordBindingError):
            self.database.register_public_answer_message(
                message_id=902,
                riddle_id=public_riddle.id,
                user_id=501,
                attempt_count=1,
            )
        with self.assertRaisesRegex(ValueError, "未記録"):
            self.database.register_public_answer_message(
                message_id=903,
                riddle_id=public_riddle.id,
                user_id=501,
                attempt_count=2,
            )
        unknown = self.database.accept_public_answer_message(999, 101)
        self.assertEqual(unknown.status, ManualAcceptanceStatus.NOT_FOUND)

    def test_public_answer_cannot_be_accepted_after_deadline(self) -> None:
        riddle = self.create_riddle(public_answers=True)
        self.database.submit_answer(
            riddle.id,
            501,
            "不正解",
            now=self.now + timedelta(minutes=1),
        )
        self.database.register_public_answer_message(
            message_id=900,
            riddle_id=riddle.id,
            user_id=501,
            attempt_count=1,
        )

        result = self.database.accept_public_answer_message(
            900,
            riddle.creator_id,
            now=riddle.deadline_at,
        )

        self.assertEqual(result.status, ManualAcceptanceStatus.EXPIRED)
        self.assertEqual(result.riddle.status, "expired")
        self.assertEqual(self.database.list_winner_ids(riddle.id), ())

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
        self.assertEqual(
            self.database.get_first_winner_at(riddle.id),
            self.now + timedelta(minutes=1),
        )

        stored = self.database.get_riddle(riddle.id)
        self.assertEqual(stored.status, "solved")
        self.assertEqual(stored.winner_id, winners[0])
        self.assertEqual(self.database.count_active_riddles(1), 0)
        self.assertEqual(self.database.list_pending_finalizations(), [stored])

        self.assertEqual(self.database.delete_finished_riddle(riddle.id), stored)
        self.assertIsNone(self.database.get_riddle(riddle.id))

    def test_briddle_stays_active_until_distinct_winner_limit_atomically(
        self,
    ) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            message_id=300,
            winner_limit=2,
        )
        first = self.database.submit_answer(
            riddle.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )
        duplicate = self.database.submit_answer(
            riddle.id,
            501,
            "シロナガスクジラ",
            now=self.now + timedelta(minutes=2),
        )

        self.assertEqual(first.status, SubmissionStatus.CORRECT)
        self.assertEqual(first.riddle.status, "active")
        self.assertEqual(first.winner_ids, (501,))
        self.assertEqual(duplicate.status, SubmissionStatus.ALREADY_CORRECT)
        self.assertEqual(duplicate.riddle.status, "active")
        self.assertEqual(duplicate.winner_ids, (501,))
        self.assertEqual(duplicate.attempt_count, 2)

        barrier = Barrier(2)

        def submit(user_id: int):
            barrier.wait()
            return self.database.submit_answer_by_message(
                300,
                user_id,
                "BLUE WHALE",
                now=self.now + timedelta(minutes=3),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(submit, (502, 503)))

        self.assertEqual(
            sorted(result.status.value for result in results),
            ["closed", "correct"],
        )
        stored = self.database.get_riddle(riddle.id)
        self.assertEqual(stored.status, "solved")
        self.assertEqual(stored.winner_id, 501)
        self.assertEqual(len(self.database.list_winner_ids(riddle.id)), 2)
        self.assertIn(501, self.database.list_winner_ids(riddle.id))

    def test_active_delete_racing_correct_answer_never_deletes_solved_row(self) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            thread_id=200,
            message_id=300,
        )
        barrier = Barrier(2)

        def delete():
            barrier.wait()
            return self.database.delete_active_riddle(
                riddle.id,
                now=self.now + timedelta(minutes=1),
            )

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
        self.assertIsNone(self.database.delete_active_riddle(riddle.id, now=self.now))
        self.assertEqual(self.database.get_riddle(riddle.id).status, "solved")

    def test_active_delete_refuses_a_problem_at_or_after_deadline(self) -> None:
        deadline = self.now + timedelta(minutes=1)
        riddle = self.create_riddle(deadline_at=deadline)

        self.assertIsNone(self.database.delete_active_riddle(riddle.id, now=deadline))
        stored = self.database.get_riddle(riddle.id)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.status, "active")

        expired = self.database.mark_riddle_expired(riddle.id, now=deadline)
        self.assertIsNotNone(expired)
        self.assertEqual(expired.status, "expired")

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

    def test_lists_unique_riddle_participants_in_user_id_order_and_cascades(
        self,
    ) -> None:
        riddle = self.create_riddle()
        self.assertEqual(
            self.database.list_riddle_participant_user_ids(riddle.id),
            (),
        )

        for user_id, answer, minutes in (
            (503, "wrong-one", 1),
            (501, "wrong-two", 2),
            (503, "wrong-three", 3),
            (502, "blue whale", 4),
        ):
            result = self.database.submit_answer(
                riddle.id,
                user_id,
                answer,
                now=self.now + timedelta(minutes=minutes),
            )
            self.assertTrue(result.attempt_recorded)

        self.assertEqual(
            self.database.list_riddle_participant_user_ids(riddle.id),
            (501, 502, 503),
        )
        self.assertEqual(
            self.database.list_riddle_participant_user_ids(riddle.id + 999),
            (),
        )

        for invalid_riddle_id in (0, -1, True, "1"):
            with (
                self.subTest(invalid_riddle_id=invalid_riddle_id),
                self.assertRaises(ValueError),
            ):
                self.database.list_riddle_participant_user_ids(
                    invalid_riddle_id,
                )

        stored = self.database.get_riddle(riddle.id)
        self.assertEqual(self.database.delete_riddle(riddle.id), stored)
        self.assertEqual(
            self.database.list_riddle_participant_user_ids(riddle.id),
            (),
        )

    def test_only_after_close_persists_wrong_body_until_disclosed_and_deleted(
        self,
    ) -> None:
        private_riddle = self.create_riddle(
            mode="briddle",
            creator_id=101,
            wrong_answer_visibility="private",
        )
        immediate_riddle = self.create_riddle(
            mode="briddle",
            creator_id=102,
            wrong_answer_visibility="immediate",
        )
        deferred_riddle = self.create_riddle(
            mode="briddle",
            creator_id=103,
            wrong_answer_visibility="after_close",
        )
        private_body = "private-secret-wrong-answer"
        immediate_body = "immediate-secret-wrong-answer"
        deferred_body = "after-close-secret-wrong-answer"
        submitted_at = self.now + timedelta(minutes=1)

        for riddle, body in (
            (private_riddle, private_body),
            (immediate_riddle, immediate_body),
            (deferred_riddle, deferred_body),
        ):
            result = self.database.submit_answer(
                riddle.id,
                501,
                body,
                now=submitted_at,
            )
            self.assertEqual(result.status, SubmissionStatus.WRONG)

        self.assertEqual(
            self.database.list_pending_deferred_answers(private_riddle.id),
            [],
        )
        self.assertEqual(
            self.database.list_pending_deferred_answers(immediate_riddle.id),
            [],
        )
        pending = self.database.list_pending_deferred_answers(deferred_riddle.id)
        self.assertEqual(len(pending), 1)
        self.assertIsInstance(pending[0], DeferredAnswer)
        self.assertEqual(pending[0].riddle_id, deferred_riddle.id)
        self.assertEqual(pending[0].user_id, 501)
        self.assertEqual(pending[0].attempt_count, 1)
        self.assertEqual(pending[0].answer_body, deferred_body)
        self.assertEqual(pending[0].submitted_at, submitted_at)
        self.assertIsNone(pending[0].disclosed_at)

        correct = self.database.submit_answer(
            deferred_riddle.id,
            502,
            "blue whale",
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(correct.status, SubmissionStatus.CORRECT)
        self.assertEqual(
            self.database.list_pending_deferred_answers(deferred_riddle.id),
            pending,
        )

        with closing(sqlite3.connect(self.database_path)) as connection:
            dump = "\n".join(connection.iterdump())
        self.assertNotIn(private_body, dump)
        self.assertNotIn(immediate_body, dump)
        self.assertIn(deferred_body, dump)

        disclosed_at = self.now + timedelta(minutes=3)
        self.assertEqual(
            self.database.mark_deferred_answers_disclosed(
                [pending[0].id, pending[0].id],
                now=disclosed_at,
            ),
            1,
        )
        self.assertEqual(
            self.database.mark_deferred_answers_disclosed(
                [pending[0].id],
                now=disclosed_at,
            ),
            0,
        )
        self.assertEqual(self.database.mark_deferred_answers_disclosed([]), 0)
        self.assertEqual(
            self.database.list_pending_deferred_answers(deferred_riddle.id),
            [],
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            stored_disclosed_at = connection.execute(
                """
                SELECT disclosed_at FROM riddle_deferred_answers
                WHERE id = ?
                """,
                (pending[0].id,),
            ).fetchone()[0]
        self.assertEqual(stored_disclosed_at, "2026-07-25T12:03:00.000000Z")

        self.assertIsNotNone(
            self.database.delete_finished_riddle(deferred_riddle.id),
        )
        with closing(sqlite3.connect(self.database_path)) as connection:
            remaining = connection.execute(
                """
                SELECT COUNT(*) FROM riddle_deferred_answers
                WHERE riddle_id = ?
                """,
                (deferred_riddle.id,),
            ).fetchone()[0]
        self.assertEqual(remaining, 0)

    def test_pending_deferred_answer_limit_preserves_submission_order(
        self,
    ) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            wrong_answer_visibility="after_close",
        )
        for minutes, body in (
            (3, "third"),
            (1, "first-a"),
            (1, "first-b"),
            (2, "second"),
        ):
            result = self.database.submit_answer(
                riddle.id,
                501,
                body,
                now=self.now + timedelta(minutes=minutes),
            )
            self.assertEqual(result.status, SubmissionStatus.WRONG)

        all_pending = self.database.list_pending_deferred_answers(riddle.id)
        self.assertEqual(
            [answer.answer_body for answer in all_pending],
            ["first-a", "first-b", "second", "third"],
        )
        first_page = self.database.list_pending_deferred_answers(
            riddle.id,
            limit=2,
        )
        self.assertEqual(first_page, all_pending[:2])

        self.database.mark_deferred_answers_disclosed(
            [answer.id for answer in first_page],
            now=self.now + timedelta(minutes=4),
        )
        self.assertEqual(
            self.database.list_pending_deferred_answers(riddle.id, limit=2),
            all_pending[2:],
        )

        for invalid_limit in (0, -1, True, "2"):
            with (
                self.subTest(invalid_limit=invalid_limit),
                self.assertRaises(ValueError),
            ):
                self.database.list_pending_deferred_answers(
                    riddle.id,
                    limit=invalid_limit,
                )
        with self.assertRaises(TypeError):
            self.database.list_pending_deferred_answers(riddle.id, 2)

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

    def test_finalize_retains_riddle_bindings_prunes_children_and_is_one_shot(
        self,
    ) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            thread_id=200,
            message_id=300,
            public_answers=True,
            wrong_answer_visibility="after_close",
        )
        wrong = self.database.submit_answer(
            riddle.id,
            501,
            "公開待ちの誤答",
            now=self.now + timedelta(minutes=1),
        )
        self.database.register_public_answer_message(
            message_id=400,
            riddle_id=riddle.id,
            user_id=501,
            attempt_count=wrong.attempt_count,
            posted_at=self.now + timedelta(minutes=1),
        )
        solved = self.database.submit_answer(
            riddle.id,
            502,
            "blue whale",
            now=self.now + timedelta(minutes=2),
        ).riddle
        self.assertEqual(self.database.list_pending_finalizations(), [solved])
        self.assertEqual(len(self.database.list_winner_ids(riddle.id)), 1)
        self.assertEqual(
            self.database.get_answer_attempt_count(riddle.id, 501),
            1,
        )
        self.assertIsNotNone(self.database.get_public_answer_message(400))
        self.assertEqual(
            len(self.database.list_pending_deferred_answers(riddle.id)),
            1,
        )

        finalized_at = self.now + timedelta(minutes=3)
        finalized = self.database.finalize_riddle(
            riddle.id,
            now=finalized_at,
        )

        self.assertIsNotNone(finalized)
        self.assertEqual(finalized.id, riddle.id)
        self.assertEqual(finalized.thread_id, 200)
        self.assertEqual(finalized.message_id, 300)
        self.assertEqual(finalized.status, "solved")
        self.assertEqual(finalized.finalized_at, finalized_at)
        self.assertEqual(self.database.get_riddle(riddle.id), finalized)
        self.assertEqual(self.database.list_pending_finalizations(), [])
        self.assertEqual(self.database.list_winner_ids(riddle.id), ())
        self.assertEqual(
            self.database.get_answer_attempt_count(riddle.id, 501),
            0,
        )
        self.assertIsNone(self.database.get_public_answer_message(400))
        self.assertEqual(
            self.database.list_pending_deferred_answers(riddle.id),
            [],
        )

        reopened = RiddleDatabase(self.database_path)
        self.assertEqual(reopened.get_riddle(riddle.id), finalized)
        self.assertEqual(reopened.list_pending_finalizations(), [])
        self.assertIsNone(
            reopened.finalize_riddle(
                riddle.id,
                now=self.now + timedelta(minutes=4),
            )
        )
        self.assertEqual(reopened.get_riddle(riddle.id).finalized_at, finalized_at)

        self.assertEqual(reopened.delete_riddle(riddle.id), finalized)
        self.assertIsNone(reopened.get_riddle(riddle.id))

    def test_finalize_refuses_active_missing_and_concurrent_duplicate(self) -> None:
        active = self.create_riddle()
        self.assertIsNone(self.database.finalize_riddle(active.id, now=self.now))
        self.assertIsNone(
            self.database.finalize_riddle(active.id + 999, now=self.now),
        )
        self.assertIsNone(self.database.get_riddle(active.id).finalized_at)

        finished = self.create_riddle(
            mode="briddle",
            creator_id=101,
        )
        self.database.submit_answer(
            finished.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )
        barrier = Barrier(2)

        def finalize(minutes: int):
            barrier.wait()
            return self.database.finalize_riddle(
                finished.id,
                now=self.now + timedelta(minutes=minutes),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(finalize, (2, 3)))

        self.assertEqual(sum(result is not None for result in results), 1)
        finalized = next(result for result in results if result is not None)
        self.assertEqual(self.database.get_riddle(finished.id), finalized)
        self.assertEqual(self.database.list_pending_finalizations(), [])

        with self.assertRaises(ValueError):
            self.database.finalize_riddle(0)

    def test_creator_or_admin_can_close_every_active_problem_mode_early(
        self,
    ) -> None:
        standard = self.create_riddle(creator_id=101)
        manual = self.create_riddle(
            creator_id=102,
            answer_display="",
            public_answers=True,
            manual_judging=True,
        )
        briddle = self.create_riddle(mode="briddle", creator_id=103)
        closed_at = self.now + timedelta(minutes=5)

        cases = (
            (standard, 101, False),
            (manual, 102, False),
            (briddle, 999, True),
        )
        for riddle, actor_id, is_admin in cases:
            with self.subTest(riddle_id=riddle.id):
                result = self.database.close_riddle_early(
                    riddle.id,
                    guild_id=1,
                    actor_id=actor_id,
                    is_admin=is_admin,
                    now=closed_at,
                )
                self.assertEqual(result.status, ManualCloseStatus.SUCCESS)
                self.assertTrue(result.closed)
                self.assertEqual(result.riddle.status, "expired")
                self.assertEqual(result.riddle.closed_early_at, closed_at)
                self.assertEqual(self.database.get_riddle(riddle.id), result.riddle)

        reopened = RiddleDatabase(self.database_path)
        for riddle, _actor_id, _is_admin in cases:
            self.assertEqual(
                reopened.get_riddle(riddle.id).closed_early_at,
                closed_at,
            )

    def test_close_riddle_early_is_guild_scoped_and_authorized(self) -> None:
        riddle = self.create_riddle(creator_id=101)

        not_found = self.database.close_riddle_early(
            riddle.id + 999,
            guild_id=1,
            actor_id=101,
            now=self.now,
        )
        self.assertEqual(not_found.status, ManualCloseStatus.NOT_FOUND)
        self.assertIsNone(not_found.riddle)

        wrong_guild = self.database.close_riddle_early(
            riddle.id,
            guild_id=2,
            actor_id=101,
            now=self.now,
        )
        self.assertEqual(wrong_guild.status, ManualCloseStatus.WRONG_GUILD)
        self.assertIsNone(wrong_guild.riddle)

        forbidden = self.database.close_riddle_early(
            riddle.id,
            guild_id=1,
            actor_id=501,
            now=self.now,
        )
        self.assertEqual(forbidden.status, ManualCloseStatus.FORBIDDEN)
        self.assertIsNone(forbidden.riddle)
        self.assertEqual(self.database.get_riddle(riddle.id).status, "active")

        for field_name, arguments in (
            ("riddle_id", {"riddle_id": 0, "guild_id": 1, "actor_id": 101}),
            ("guild_id", {"riddle_id": riddle.id, "guild_id": 0, "actor_id": 101}),
            ("actor_id", {"riddle_id": riddle.id, "guild_id": 1, "actor_id": 0}),
        ):
            with (
                self.subTest(field_name=field_name),
                self.assertRaises(ValueError),
            ):
                self.database.close_riddle_early(**arguments)
        with self.assertRaises(TypeError):
            self.database.close_riddle_early(
                riddle.id,
                guild_id=1,
                actor_id=101,
                is_admin=1,  # type: ignore[arg-type]
            )

    def test_close_riddle_early_is_idempotent_and_preserves_first_timestamp(
        self,
    ) -> None:
        riddle = self.create_riddle()
        first_time = self.now + timedelta(minutes=1)
        first = self.database.close_riddle_early(
            riddle.id,
            guild_id=1,
            actor_id=100,
            now=first_time,
        )
        repeated = self.database.close_riddle_early(
            riddle.id,
            guild_id=1,
            actor_id=100,
            now=self.now + timedelta(minutes=2),
        )

        self.assertEqual(first.status, ManualCloseStatus.SUCCESS)
        self.assertEqual(repeated.status, ManualCloseStatus.ALREADY_CLOSED)
        self.assertFalse(repeated.closed)
        self.assertEqual(repeated.riddle.closed_early_at, first_time)
        self.assertEqual(self.database.get_riddle(riddle.id), repeated.riddle)

        due = self.create_riddle(
            creator_id=101,
            deadline_at=self.now + timedelta(minutes=1),
        )
        expired = self.database.mark_riddle_expired(
            due.id,
            now=self.now + timedelta(minutes=1),
        )
        already_expired = self.database.close_riddle_early(
            due.id,
            guild_id=1,
            actor_id=101,
            now=self.now + timedelta(minutes=2),
        )
        self.assertEqual(already_expired.status, ManualCloseStatus.ALREADY_CLOSED)
        self.assertEqual(already_expired.riddle, expired)
        self.assertIsNone(already_expired.riddle.closed_early_at)

    def test_concurrent_manual_close_calls_only_close_once(self) -> None:
        riddle = self.create_riddle()
        barrier = Barrier(2)

        def close(minutes: int):
            barrier.wait()
            return self.database.close_riddle_early(
                riddle.id,
                guild_id=1,
                actor_id=100,
                now=self.now + timedelta(minutes=minutes),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(close, (1, 2)))

        self.assertEqual(
            sorted(result.status.value for result in results),
            ["already_closed", "success"],
        )
        successful = next(
            result for result in results if result.status is ManualCloseStatus.SUCCESS
        )
        stored = self.database.get_riddle(riddle.id)
        self.assertEqual(stored.status, "expired")
        self.assertEqual(stored.closed_early_at, successful.riddle.closed_early_at)

    def test_manual_close_and_correct_submission_serialize_atomically(self) -> None:
        riddle = self.create_riddle(
            mode="briddle",
            message_id=300,
        )
        barrier = Barrier(2)

        def close():
            barrier.wait()
            return self.database.close_riddle_early(
                riddle.id,
                guild_id=1,
                actor_id=100,
                now=self.now + timedelta(minutes=1),
            )

        def answer():
            barrier.wait()
            return self.database.submit_answer_by_message(
                300,
                501,
                "blue whale",
                now=self.now + timedelta(minutes=1),
            )

        with ThreadPoolExecutor(max_workers=2) as executor:
            close_future = executor.submit(close)
            answer_future = executor.submit(answer)
            close_result = close_future.result()
            answer_result = answer_future.result()

        outcomes = (close_result.status, answer_result.status)
        self.assertIn(
            outcomes,
            (
                (ManualCloseStatus.SUCCESS, SubmissionStatus.EXPIRED),
                (ManualCloseStatus.ALREADY_CLOSED, SubmissionStatus.CORRECT),
            ),
        )
        stored = self.database.get_riddle(riddle.id)
        if close_result.status is ManualCloseStatus.SUCCESS:
            self.assertEqual(stored.status, "expired")
            self.assertEqual(stored.closed_early_at, self.now + timedelta(minutes=1))
            self.assertEqual(self.database.list_winner_ids(riddle.id), ())
            self.assertEqual(
                self.database.get_answer_attempt_count(riddle.id, 501),
                0,
            )
        else:
            self.assertEqual(stored.status, "solved")
            self.assertIsNone(stored.closed_early_at)
            self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))
            self.assertEqual(
                self.database.get_answer_attempt_count(riddle.id, 501),
                1,
            )

    def test_user_summary_and_deletion_remove_all_user_references(self) -> None:
        owned = self.create_riddle(creator_id=501)
        answered = self.create_riddle(
            creator_id=502,
            message_id=300,
            public_answers=True,
        )
        deferred = self.create_riddle(
            mode="briddle",
            creator_id=503,
            wrong_answer_visibility="after_close",
        )
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
        self.database.register_public_answer_message(
            message_id=400,
            riddle_id=answered.id,
            user_id=501,
            attempt_count=1,
            posted_at=self.now + timedelta(minutes=1),
        )
        self.database.submit_answer(
            deferred.id,
            501,
            "個人データとして削除される終了後公開の誤答",
            now=self.now + timedelta(minutes=2),
        )

        summary = self.database.get_user_data_summary(1, 501)
        self.assertEqual(summary.created_riddles, 1)
        self.assertEqual(summary.active_created_riddles, 1)
        self.assertEqual(summary.correct_riddles, 1)
        self.assertEqual(summary.first_wins, 1)
        self.assertEqual(summary.answer_attempts, 2)
        self.assertEqual(summary.public_answer_messages, 1)
        self.assertEqual(summary.deferred_answers, 1)
        self.assertTrue(summary.has_preferences)

        deletion = self.database.delete_user_data(1, 501)
        self.assertEqual(deletion.deleted_riddles, 1)
        self.assertEqual(deletion.deleted_winner_entries, 1)
        self.assertEqual(deletion.deleted_answer_attempts, 2)
        self.assertEqual(deletion.deleted_public_answer_messages, 1)
        self.assertEqual(deletion.deleted_deferred_answers, 1)
        self.assertTrue(deletion.deleted_preferences)
        self.assertIsNone(self.database.get_riddle(owned.id))
        remaining = self.database.get_riddle(answered.id)
        self.assertIsNotNone(remaining)
        self.assertIsNone(remaining.winner_id)
        self.assertEqual(self.database.list_winner_ids(answered.id), ())
        self.assertEqual(self.database.get_answer_attempt_count(answered.id, 501), 0)
        self.assertIsNone(self.database.get_public_answer_message(400))
        self.assertIsNotNone(self.database.get_riddle(deferred.id))
        self.assertEqual(
            self.database.list_pending_deferred_answers(deferred.id),
            [],
        )
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
        riddle = self.create_riddle(public_answers=True)
        deferred = self.create_riddle(
            mode="briddle",
            creator_id=102,
            wrong_answer_visibility="after_close",
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "blue whale",
            now=self.now + timedelta(minutes=1),
        )
        self.database.register_public_answer_message(
            message_id=400,
            riddle_id=riddle.id,
            user_id=501,
            attempt_count=1,
        )
        self.database.submit_answer(
            deferred.id,
            502,
            "サーバー削除対象の終了後公開誤答",
            now=self.now + timedelta(minutes=2),
        )

        deletion = self.database.delete_guild_data(1)
        self.assertEqual(deletion.deleted_riddles, 2)
        self.assertEqual(deletion.deleted_winner_entries, 1)
        self.assertEqual(deletion.deleted_answer_attempts, 2)
        self.assertEqual(deletion.deleted_public_answer_messages, 1)
        self.assertEqual(deletion.deleted_deferred_answers, 1)
        self.assertEqual(deletion.deleted_user_preferences, 1)
        self.assertTrue(deletion.deleted_settings)
        self.assertEqual(self.database.list_guild_ids(), [])
        self.assertIsNone(self.database.get_riddle(riddle.id))
        self.assertIsNone(self.database.get_riddle(deferred.id))

    def test_manual_delete_can_remove_an_active_problem(self) -> None:
        riddle = self.create_riddle()
        self.assertIsNone(self.database.delete_finished_riddle(riddle.id))
        self.assertEqual(self.database.delete_riddle(riddle.id), riddle)
        self.assertIsNone(self.database.get_riddle(riddle.id))


if __name__ == "__main__":
    unittest.main()
