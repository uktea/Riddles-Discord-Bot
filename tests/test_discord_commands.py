from __future__ import annotations

import asyncio
import io
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from discord import app_commands

from riddle_bot.database import Riddle, RiddleDatabase
from riddle_bot.discord_commands import (
    ANSWER_BUTTON_CUSTOM_ID,
    MEDIA_MAX_SIZE_BYTES,
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
        public_modal = AnswerModal(
            self.cog,
            source_message_id=123,
            public_answers=True,
        )

        self.assertEqual(modal.source_message_id, 123)
        self.assertEqual(modal.answer_input.max_length, 500)
        self.assertTrue(modal.answer_input.required)
        self.assertFalse(modal.public_answers)
        self.assertTrue(public_modal.public_answers)
        self.assertIn("公開", public_modal.children[0].description)

    def test_all_required_application_commands_are_registered(self) -> None:
        command_names = {command.name for command in self.cog.get_app_commands()}

        self.assertEqual(
            command_names,
            {
                "briddle",
                "riddle",
                "help",
                "delete",
                "list",
                "pref",
                "mypref",
                "status",
                "mydata",
                "deletemydata",
            },
        )

    def test_creation_commands_accept_one_optional_media_attachment(self) -> None:
        commands_by_name = {
            command.name: command for command in self.cog.get_app_commands()
        }

        for command_name in ("briddle", "riddle"):
            with self.subTest(command=command_name):
                parameters = {
                    parameter.name: parameter
                    for parameter in commands_by_name[command_name].parameters
                }
                media = parameters["media"]
                self.assertEqual(
                    media.type,
                    discord.AppCommandOptionType.attachment,
                )
                self.assertFalse(media.required)

    def test_riddle_accepts_optional_personal_setting_overrides(self) -> None:
        command = next(
            command
            for command in self.cog.get_app_commands()
            if command.name == "riddle"
        )
        parameters = {parameter.name: parameter for parameter in command.parameters}

        self.assertEqual(
            parameters["public_answers"].type,
            discord.AppCommandOptionType.boolean,
        )
        self.assertFalse(parameters["public_answers"].required)
        self.assertEqual(
            parameters["answer_limit"].type,
            discord.AppCommandOptionType.integer,
        )
        self.assertFalse(parameters["answer_limit"].required)
        self.assertEqual(parameters["answer_limit"].min_value, 0)
        self.assertEqual(parameters["answer_limit"].max_value, 100)

    def test_media_validation_accepts_image_audio_and_video(self) -> None:
        for content_type in (
            "image/png",
            "audio/mpeg",
            "video/mp4; charset=binary",
        ):
            with self.subTest(content_type=content_type):
                media = SimpleNamespace(
                    content_type=content_type,
                    size=1024,
                )
                self.cog._validate_media_attachment(
                    media,  # type: ignore[arg-type]
                    server_size_limit=MEDIA_MAX_SIZE_BYTES,
                )

    def test_media_validation_rejects_non_media_svg_and_unknown_types(self) -> None:
        for content_type in ("application/pdf", "image/svg+xml", None):
            with self.subTest(content_type=content_type):
                media = SimpleNamespace(
                    content_type=content_type,
                    size=1024,
                )
                with self.assertRaisesRegex(ValueError, "画像・音声・動画"):
                    self.cog._validate_media_attachment(
                        media,  # type: ignore[arg-type]
                        server_size_limit=MEDIA_MAX_SIZE_BYTES,
                    )

        disguised_name = SimpleNamespace(
            content_type="image/png",
            filename="diagram.SVG",
            size=1024,
        )
        with self.assertRaisesRegex(ValueError, "SVG"):
            self.cog._validate_media_attachment(
                disguised_name,  # type: ignore[arg-type]
                server_size_limit=MEDIA_MAX_SIZE_BYTES,
            )

    def test_media_content_validation_detects_disguised_svg(self) -> None:
        disguised = discord.File(
            io.BytesIO(
                b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg">'
            ),
            filename="diagram.png",
        )
        safe = discord.File(
            io.BytesIO(b"\x89PNG\r\n\x1a\nnot-svg"),
            filename="diagram.png",
        )
        self.addCleanup(disguised.close)
        self.addCleanup(safe.close)

        self.assertTrue(self.cog._file_looks_like_svg(disguised))
        self.assertFalse(self.cog._file_looks_like_svg(safe))

    def test_media_validation_uses_smaller_of_bot_and_server_limits(self) -> None:
        media = SimpleNamespace(
            content_type="image/webp",
            size=11 * 1024 * 1024,
        )
        with self.assertRaisesRegex(ValueError, "10 MiB"):
            self.cog._validate_media_attachment(
                media,  # type: ignore[arg-type]
                server_size_limit=10 * 1024 * 1024,
            )

        media.size = MEDIA_MAX_SIZE_BYTES + 1
        with self.assertRaisesRegex(ValueError, "25 MiB"):
            self.cog._validate_media_attachment(
                media,  # type: ignore[arg-type]
                server_size_limit=50 * 1024 * 1024,
            )

    def test_help_content_is_dynamic_and_within_discord_limit(self) -> None:
        content = self.cog._help_content()

        for command in self.cog.get_app_commands():
            self.assertIn(f"/{command.name}", content)
        self.assertLessEqual(len(content), 2_000)

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
        self.assertIsNone(self.cog._parse_answer_limit("0"))
        self.assertIsNone(self.cog._parse_answer_limit("無制限"))
        self.assertEqual(self.cog._parse_answer_limit("25"), 25)
        with self.assertRaises(ValueError):
            self.cog._parse_boolean("sometimes")
        with self.assertRaises(ValueError):
            self.cog._parse_answer_limit("101")


class DiscordGuildRestrictionTests(unittest.IsolatedAsyncioTestCase):
    def make_interaction(self, guild_id: int) -> SimpleNamespace:
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        )
        return SimpleNamespace(
            guild_id=guild_id,
            guild=SimpleNamespace(id=guild_id),
            user=SimpleNamespace(id=1),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

    async def test_configured_guild_is_allowed_and_other_guild_is_rejected(
        self,
    ) -> None:
        cog = RiddleCog(
            bot=SimpleNamespace(config=SimpleNamespace(guild_id=10)),  # type: ignore[arg-type]
            database=SimpleNamespace(),  # type: ignore[arg-type]
        )
        allowed = self.make_interaction(10)
        rejected = self.make_interaction(11)

        self.assertTrue(await cog.interaction_check(allowed))  # type: ignore[arg-type]
        self.assertFalse(await cog.interaction_check(rejected))  # type: ignore[arg-type]
        rejected.response.send_message.assert_awaited_once()

    async def test_unconfigured_guild_allows_existing_multi_guild_behavior(
        self,
    ) -> None:
        cog = RiddleCog(
            bot=SimpleNamespace(config=SimpleNamespace(guild_id=None)),  # type: ignore[arg-type]
            database=SimpleNamespace(),  # type: ignore[arg-type]
        )

        self.assertTrue(
            await cog.interaction_check(self.make_interaction(999))  # type: ignore[arg-type]
        )

    async def test_answer_from_other_guild_stops_before_database_access(self) -> None:
        cog = RiddleCog(
            bot=SimpleNamespace(config=SimpleNamespace(guild_id=10)),  # type: ignore[arg-type]
            database=SimpleNamespace(),  # type: ignore[arg-type]
        )
        cog._db = AsyncMock()  # type: ignore[method-assign]
        interaction = self.make_interaction(11)

        await cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=123,
            submitted_answer="外部サーバーからの回答",
        )

        cog._db.assert_not_awaited()
        interaction.edit_original_response.assert_awaited_once()

    async def test_allowlist_check_failure_does_not_send_second_error(self) -> None:
        cog = RiddleCog(
            bot=SimpleNamespace(config=SimpleNamespace(guild_id=10)),  # type: ignore[arg-type]
            database=SimpleNamespace(),  # type: ignore[arg-type]
        )
        rejected = self.make_interaction(11)

        self.assertFalse(await cog.interaction_check(rejected))  # type: ignore[arg-type]
        await cog.cog_app_command_error(
            rejected,  # type: ignore[arg-type]
            app_commands.CheckFailure(),
        )

        rejected.response.send_message.assert_awaited_once()
        rejected.followup.send.assert_not_awaited()


class DiscordHelpTests(unittest.IsolatedAsyncioTestCase):
    async def test_help_is_sent_ephemerally(self) -> None:
        cog = RiddleCog(
            bot=SimpleNamespace(),  # type: ignore[arg-type]
            database=SimpleNamespace(),  # type: ignore[arg-type]
        )
        response = SimpleNamespace(
            is_done=lambda: False,
            send_message=AsyncMock(),
        )
        interaction = SimpleNamespace(
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

        await cog.help_command.callback(
            cog,
            interaction,  # type: ignore[arg-type]
        )

        response.send_message.assert_awaited_once()
        args, kwargs = response.send_message.await_args
        self.assertTrue(kwargs["ephemeral"])
        self.assertIn("/help", args[0])


class FakeTextChannel:
    def __init__(self, channel_id: int = 20) -> None:
        self.id = channel_id

    def permissions_for(self, _member: object) -> SimpleNamespace:
        return SimpleNamespace(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            create_public_threads=True,
            send_messages_in_threads=True,
            manage_threads=True,
            attach_files=True,
        )


class FakePublicThread:
    def __init__(self, *, archived: bool = False) -> None:
        self.archived = archived
        self.edit = AsyncMock()
        self.send = AsyncMock()


class DiscordRiddleMediaFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addAsyncCleanup(self._cleanup_temporary_directory)
        database_path = Path(self.temporary_directory.name) / "riddles.db"
        self.database = RiddleDatabase(database_path)
        self.cog = RiddleCog(
            bot=SimpleNamespace(),  # type: ignore[arg-type]
            database=self.database,
        )

    async def _cleanup_temporary_directory(self) -> None:
        self.temporary_directory.cleanup()

    def make_interaction(
        self,
        *,
        edit_original_response: AsyncMock,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            guild=SimpleNamespace(
                id=10,
                me=object(),
                filesize_limit=MEDIA_MAX_SIZE_BYTES,
            ),
            channel=FakeTextChannel(),
            user=SimpleNamespace(id=50, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=edit_original_response,
        )

    def make_media(self) -> tuple[SimpleNamespace, discord.File]:
        uploaded_file = discord.File(
            io.BytesIO(b"test-image"),
            filename="question.png",
        )
        media = SimpleNamespace(
            content_type="image/png",
            size=10,
            is_spoiler=lambda: False,
            to_file=AsyncMock(return_value=uploaded_file),
        )
        return media, uploaded_file

    async def test_media_is_reuploaded_to_public_starter_message(self) -> None:
        prompt = SimpleNamespace(id=40, edit=AsyncMock())
        thread = SimpleNamespace(
            id=30,
            mention="<#30>",
            send=AsyncMock(return_value=prompt),
        )
        message = SimpleNamespace(
            create_thread=AsyncMock(return_value=thread),
            edit=AsyncMock(),
        )
        edit_original_response = AsyncMock(return_value=message)
        interaction = self.make_interaction(
            edit_original_response=edit_original_response,
        )
        media, uploaded_file = self.make_media()

        with patch(
            "riddle_bot.discord_commands.discord.TextChannel",
            FakeTextChannel,
        ):
            await self.cog._create_riddle(
                interaction,  # type: ignore[arg-type]
                mode="riddle",
                question="画像つき問題",
                answer="正解",
                term="1h",
                media=media,  # type: ignore[arg-type]
            )

        media.to_file.assert_awaited_once_with(spoiler=False)
        _, initial_kwargs = edit_original_response.await_args
        self.assertEqual(initial_kwargs["attachments"], [uploaded_file])
        self.assertEqual(len(self.database.list_active_riddles(10)), 1)

    async def test_media_is_removed_when_thread_creation_fails(self) -> None:
        message = SimpleNamespace(
            create_thread=AsyncMock(side_effect=ValueError("test failure")),
        )
        edit_original_response = AsyncMock(side_effect=[message, None])
        interaction = self.make_interaction(
            edit_original_response=edit_original_response,
        )
        media, uploaded_file = self.make_media()

        with patch(
            "riddle_bot.discord_commands.discord.TextChannel",
            FakeTextChannel,
        ):
            await self.cog._create_riddle(
                interaction,  # type: ignore[arg-type]
                mode="riddle",
                question="作成失敗テスト",
                answer="正解",
                term="1h",
                media=media,  # type: ignore[arg-type]
            )

        self.assertEqual(edit_original_response.await_count, 2)
        first_kwargs = edit_original_response.await_args_list[0].kwargs
        failure_kwargs = edit_original_response.await_args_list[1].kwargs
        self.assertEqual(first_kwargs["attachments"], [uploaded_file])
        self.assertEqual(failure_kwargs["attachments"], [])
        self.assertEqual(self.database.list_active_riddles(10), [])

    async def test_personal_defaults_are_resolved_when_riddle_is_created(
        self,
    ) -> None:
        self.database.update_user_preferences(
            10,
            50,
            public_answers=True,
            answer_limit=3,
        )
        prompt = SimpleNamespace(id=40, edit=AsyncMock())
        thread = SimpleNamespace(
            id=30,
            mention="<#30>",
            send=AsyncMock(return_value=prompt),
        )
        message = SimpleNamespace(
            create_thread=AsyncMock(return_value=thread),
            edit=AsyncMock(),
        )
        interaction = self.make_interaction(
            edit_original_response=AsyncMock(return_value=message),
        )

        with patch(
            "riddle_bot.discord_commands.discord.TextChannel",
            FakeTextChannel,
        ):
            await self.cog._create_riddle(
                interaction,  # type: ignore[arg-type]
                mode="riddle",
                question="個人設定テスト",
                answer="正解",
                term="1h",
                media=None,
            )

        created = self.database.list_active_riddles(10)[0]
        self.assertTrue(created.public_answers)
        self.assertEqual(created.answer_limit, 3)
        prompt_content = prompt.edit.await_args.kwargs["content"]
        self.assertIn("公開", prompt_content)
        self.assertIn("3回", prompt_content)

    async def test_per_riddle_options_override_personal_defaults(self) -> None:
        self.database.update_user_preferences(
            10,
            50,
            public_answers=True,
            answer_limit=3,
        )
        prompt = SimpleNamespace(id=40, edit=AsyncMock())
        thread = SimpleNamespace(
            id=30,
            mention="<#30>",
            send=AsyncMock(return_value=prompt),
        )
        message = SimpleNamespace(
            create_thread=AsyncMock(return_value=thread),
            edit=AsyncMock(),
        )
        interaction = self.make_interaction(
            edit_original_response=AsyncMock(return_value=message),
        )

        with patch(
            "riddle_bot.discord_commands.discord.TextChannel",
            FakeTextChannel,
        ):
            await self.cog._create_riddle(
                interaction,  # type: ignore[arg-type]
                mode="riddle",
                question="上書きテスト",
                answer="正解",
                term="1h",
                media=None,
                public_answers=False,
                answer_limit=0,
            )

        created = self.database.list_active_riddles(10)[0]
        self.assertFalse(created.public_answers)
        self.assertIsNone(created.answer_limit)


class FakeAnswerInteraction:
    def __init__(self, user_id: int, guild_id: int = 1) -> None:
        self.user = SimpleNamespace(id=user_id, display_name=f"回答者{user_id}")
        self.guild_id = guild_id
        self.guild = SimpleNamespace(id=guild_id)
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
        public_answers: bool = False,
        answer_limit: int | None = None,
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
            public_answers=public_answers,
            answer_limit=answer_limit,
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

    async def test_public_riddle_posts_wrong_and_correct_answers_without_result(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=600,
            public_answers=True,
        )
        self.cog._publish_public_answer = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        wrong_interaction = FakeAnswerInteraction(501)
        correct_interaction = FakeAnswerInteraction(502)

        await self.cog.handle_answer(
            wrong_interaction,  # type: ignore[arg-type]
            source_message_id=600,
            submitted_answer="まちがい",
        )
        await self.cog.handle_answer(
            correct_interaction,  # type: ignore[arg-type]
            source_message_id=600,
            submitted_answer="正解",
        )

        self.assertEqual(
            wrong_interaction.edited_content,
            correct_interaction.edited_content,
        )
        self.assertNotIn("正解です", correct_interaction.edited_content or "")
        self.assertEqual(self.cog._publish_public_answer.await_count, 2)
        first_call = self.cog._publish_public_answer.await_args_list[0]
        self.assertEqual(first_call.args[0].id, riddle.id)
        self.assertEqual(first_call.args[2], "まちがい")

    async def test_answer_limit_rejects_extra_submission_without_publication(
        self,
    ) -> None:
        self.create_bound_riddle(
            mode="riddle",
            message_id=700,
            public_answers=True,
            answer_limit=1,
        )
        self.cog._publish_public_answer = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        interaction = FakeAnswerInteraction(501)

        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=700,
            submitted_answer="1回目",
        )
        self.cog._answer_cooldowns.clear()
        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=700,
            submitted_answer="2回目",
        )

        self.assertIn("1人1回まで", interaction.edited_content or "")
        self.assertEqual(self.cog._publish_public_answer.await_count, 1)

    async def test_public_answer_unarchives_thread_and_blocks_mentions_and_embeds(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=750,
            public_answers=True,
        )
        thread = FakePublicThread(archived=True)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            published = await self.cog._publish_public_answer(
                riddle,
                SimpleNamespace(display_name="回答者"),
                "@everyone https://example.invalid/",
                1,
            )

        self.assertTrue(published)
        thread.edit.assert_awaited_once_with(archived=False)
        thread.send.assert_awaited_once()
        content = thread.send.await_args.args[0]
        kwargs = thread.send.await_args.kwargs
        self.assertNotIn("@everyone", content)
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertTrue(kwargs["suppress_embeds"])

    async def test_mypref_updates_personal_defaults(self) -> None:
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=501),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await self.cog.mypref.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            SimpleNamespace(value="public_answers"),
            "true",
        )
        await self.cog.mypref.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            SimpleNamespace(value="answer_limit"),
            "4",
        )

        preferences = self.database.get_user_preferences(1, 501)
        self.assertTrue(preferences.public_answers)
        self.assertEqual(preferences.answer_limit, 4)

    async def test_public_answer_finishes_before_delete_closes_thread(self) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=800,
            public_answers=True,
        )
        publication_started = asyncio.Event()
        allow_publication_to_finish = asyncio.Event()

        async def delayed_publication(*_args: object) -> bool:
            publication_started.set()
            await allow_publication_to_finish.wait()
            return True

        self.cog._publish_public_answer = AsyncMock(  # type: ignore[method-assign]
            side_effect=delayed_publication
        )
        self.cog._close_discord_problem = AsyncMock()  # type: ignore[method-assign]
        answer_interaction = FakeAnswerInteraction(501)
        delete_interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        answer_task = asyncio.create_task(
            self.cog.handle_answer(
                answer_interaction,  # type: ignore[arg-type]
                source_message_id=800,
                submitted_answer="公開する回答",
            )
        )
        await publication_started.wait()
        delete_task = asyncio.create_task(
            self.cog.delete.callback(
                self.cog,
                delete_interaction,  # type: ignore[arg-type]
                riddle.id,
            )
        )
        await asyncio.sleep(0)
        self.cog._close_discord_problem.assert_not_awaited()

        allow_publication_to_finish.set()
        await asyncio.gather(answer_task, delete_task)

        self.cog._close_discord_problem.assert_awaited_once()
        self.assertIsNone(self.database.get_riddle(riddle.id))

    async def test_deletemydata_warns_that_public_discord_answers_remain(
        self,
    ) -> None:
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=501),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await self.cog.deletemydata.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            True,
        )

        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("公開回答としてBotが投稿済み", content)


if __name__ == "__main__":
    unittest.main()
