from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock

from riddle_bot.database import Riddle, RiddleDatabase
from riddle_bot.discord_commands import (
    ANSWER_BUTTON_CUSTOM_ID,
    AnswerModal,
    AnswerView,
    ClosedAnswerView,
    RiddleCog,
)


def make_riddle(**overrides: object) -> Riddle:
    values: dict[str, object] = {
        "id": 1,
        "guild_id": 10,
        "channel_id": 20,
        "thread_id": 30,
        "message_id": 40,
        "creator_id": 50,
        "mode": "riddle",
        "question": "パンはパンでも食べられないパンは？",
        "answer_display": "フライパン",
        "answers_json": '["フライパン"]',
        "deadline_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "status": "active",
        "winner_id": None,
        "created_at": datetime.now(timezone.utc),
    }
    values.update(overrides)
    return Riddle(**values)  # type: ignore[arg-type]


class DiscordComponentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.cog = RiddleCog(
            bot=SimpleNamespace(),  # type: ignore[arg-type]
            database=SimpleNamespace(),  # type: ignore[arg-type]
        )

    def test_answer_view_is_persistent_and_uses_stable_custom_id(self) -> None:
        view = AnswerView(self.cog)

        self.assertTrue(view.is_persistent())
        self.assertEqual(len(view.children), 1)
        self.assertEqual(view.children[0].custom_id, ANSWER_BUTTON_CUSTOM_ID)

    def test_closed_view_disables_the_answer_button(self) -> None:
        view = ClosedAnswerView()

        self.assertTrue(view.children[0].disabled)
        self.assertEqual(view.children[0].custom_id, ANSWER_BUTTON_CUSTOM_ID)

    def test_modal_uses_private_text_input(self) -> None:
        modal = AnswerModal(self.cog, source_message_id=123)

        self.assertEqual(modal.source_message_id, 123)
        self.assertEqual(modal.answer_input.max_length, 500)
        self.assertTrue(modal.answer_input.required)

    def test_all_required_application_commands_are_registered(self) -> None:
        command_names = {command.name for command in self.cog.get_app_commands()}

        self.assertEqual(
            command_names,
            {
                "briddle",
                "riddle",
                "delete",
                "list",
                "pref",
                "status",
                "mydata",
                "deletemydata",
            },
        )

    def test_open_message_never_contains_the_secret_answer(self) -> None:
        riddle = make_riddle(answer_display="secret-answer")

        content = self.cog._open_content(
            riddle,
            creator_name="作成者",
            thread=None,
        )

        self.assertNotIn("secret-answer", content)
        self.assertIn(riddle.question, content)

    def test_final_message_controls_winner_mentions(self) -> None:
        riddle = make_riddle(status="expired")

        content, no_mentions = self.cog._finished_content(
            riddle,
            [100, 101],
            False,
            None,
        )
        mentioned_content, mentions = self.cog._finished_content(
            riddle,
            [100, 101],
            True,
            None,
        )

        self.assertIn(riddle.answer_display, content)
        self.assertFalse(no_mentions.users)
        self.assertIn("<@100>", mentioned_content)
        self.assertEqual([user.id for user in mentions.users], [100, 101])

    def test_preference_value_parsers(self) -> None:
        self.assertTrue(self.cog._parse_boolean("true"))
        self.assertFalse(self.cog._parse_boolean("OFF"))
        with self.assertRaises(ValueError):
            self.cog._parse_boolean("sometimes")


class FakeAnswerInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.edited_content: str | None = None

    async def edit_original_response(self, *, content: str) -> None:
        self.edited_content = content


class DiscordDatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_temporary_directory)
        database_path = Path(self.temporary_directory.name) / "riddles.db"
        self.database = RiddleDatabase(database_path)
        self.cog = RiddleCog(
            bot=SimpleNamespace(),  # type: ignore[arg-type]
            database=self.database,
        )
        self.now = datetime.now(timezone.utc)

    async def _cleanup_temporary_directory(self) -> None:
        self.temporary_directory.cleanup()

    def create_bound_riddle(
        self,
        *,
        mode: str,
        message_id: int,
        deadline_at: datetime | None = None,
    ) -> Riddle:
        return self.database.create_riddle(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            mode=mode,
            question="テスト問題",
            answer_display="正解",
            deadline_at=deadline_at or self.now + timedelta(hours=1),
            thread_id=message_id + 1,
            message_id=message_id,
        )

    async def test_riddle_keeps_correctness_private_but_records_winner(self) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=300)
        wrong_interaction = FakeAnswerInteraction(501)
        correct_interaction = FakeAnswerInteraction(502)

        await self.cog.handle_answer(
            wrong_interaction,  # type: ignore[arg-type]
            source_message_id=300,
            submitted_answer="不正解",
        )
        await self.cog.handle_answer(
            correct_interaction,  # type: ignore[arg-type]
            source_message_id=300,
            submitted_answer="正解",
        )

        self.assertEqual(
            wrong_interaction.edited_content,
            "回答を受け付けました。結果は期限に発表します。",
        )
        self.assertEqual(
            correct_interaction.edited_content,
            wrong_interaction.edited_content,
        )
        self.assertEqual(self.database.list_winner_ids(riddle.id), (502,))

    async def test_briddle_first_correct_answer_triggers_finalization(self) -> None:
        riddle = self.create_bound_riddle(mode="briddle", message_id=400)
        interaction = FakeAnswerInteraction(501)
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]

        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=400,
            submitted_answer="正解",
        )

        self.assertIn("正解です", interaction.edited_content or "")
        self.assertEqual(self.database.get_riddle(riddle.id).status, "solved")
        self.cog._publish_finalization.assert_awaited_once_with(riddle.id)

    async def test_answer_from_parent_button_falls_back_to_thread_id(self) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=450)
        interaction = FakeAnswerInteraction(501)

        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=riddle.thread_id or 0,
            submitted_answer="正解",
        )

        self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))
        self.assertEqual(
            interaction.edited_content,
            "回答を受け付けました。結果は期限に発表します。",
        )

    async def test_late_answer_is_expired_and_finalized(self) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=500,
            deadline_at=self.now - timedelta(seconds=1),
        )
        interaction = FakeAnswerInteraction(501)
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]

        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=500,
            submitted_answer="正解",
        )

        self.assertIn("期限", interaction.edited_content or "")
        self.assertEqual(self.database.get_riddle(riddle.id).status, "expired")
        self.cog._publish_finalization.assert_awaited_once_with(riddle.id)


if __name__ == "__main__":
    unittest.main()
