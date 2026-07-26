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
    ACCEPT_ANSWER_EMOJI,
    ACCEPTED_ANSWER_EMOJI,
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
        deferred_modal = AnswerModal(
            self.cog,
            source_message_id=123,
            answer_notice="不正解は問題終了時に公開されます。",
        )

        self.assertEqual(modal.source_message_id, 123)
        self.assertEqual(modal.answer_input.max_length, 500)
        self.assertTrue(modal.answer_input.required)
        self.assertFalse(modal.public_answers)
        self.assertTrue(public_modal.public_answers)
        self.assertIn("公開", public_modal.children[0].description)
        self.assertIn("問題終了時", deferred_modal.children[0].description)

    def test_unicode_reaction_matching_ignores_variation_selectors(self) -> None:
        payload_emoji = SimpleNamespace(id=None, name="☑")

        self.assertTrue(
            self.cog._emoji_matches(  # type: ignore[arg-type]
                payload_emoji,
                "☑️",
            )
        )

    def test_all_required_application_commands_are_registered(self) -> None:
        command_names = {command.name for command in self.cog.get_app_commands()}

        self.assertEqual(
            command_names,
            {
                "briddle",
                "riddle",
                "ogiri",
                "hint",
                "help",
                "close",
                "delete",
                "list",
                "pref",
                "mypref",
                "status",
                "mydata",
                "deletemydata",
            },
        )

    def test_delete_command_only_accepts_riddle_id(self) -> None:
        command = next(
            command
            for command in self.cog.get_app_commands()
            if command.name == "delete"
        )

        self.assertEqual(
            [parameter.name for parameter in command.parameters],
            ["riddle_id"],
        )

    def test_creation_commands_accept_one_optional_media_attachment(self) -> None:
        commands_by_name = {
            command.name: command for command in self.cog.get_app_commands()
        }

        for command_name in ("briddle", "riddle", "ogiri"):
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

    def test_hint_accepts_optional_text_and_media(self) -> None:
        command = next(
            command for command in self.cog.get_app_commands() if command.name == "hint"
        )
        parameters = {parameter.name: parameter for parameter in command.parameters}

        self.assertFalse(parameters["text"].required)
        self.assertEqual(
            parameters["media"].type,
            discord.AppCommandOptionType.attachment,
        )
        self.assertFalse(parameters["media"].required)

    def test_ogiri_has_no_secret_answer_parameter(self) -> None:
        command = next(
            command
            for command in self.cog.get_app_commands()
            if command.name == "ogiri"
        )
        parameters = {parameter.name: parameter for parameter in command.parameters}

        self.assertIn("theme", parameters)
        self.assertNotIn("answer", parameters)
        self.assertEqual(parameters["answer_limit"].min_value, 0)
        self.assertEqual(parameters["answer_limit"].max_value, 100)

    def test_briddle_accepts_winner_limit_and_wrong_answer_visibility(self) -> None:
        command = next(
            command
            for command in self.cog.get_app_commands()
            if command.name == "briddle"
        )
        parameters = {parameter.name: parameter for parameter in command.parameters}

        self.assertEqual(parameters["winner_limit"].min_value, 1)
        self.assertEqual(parameters["winner_limit"].max_value, 100)
        self.assertFalse(parameters["answer_limit"].required)
        self.assertEqual(parameters["answer_limit"].min_value, 1)
        self.assertEqual(parameters["answer_limit"].max_value, 100)
        self.assertEqual(
            {choice.value for choice in parameters["wrong_answers"].choices},
            {"private", "immediate", "after_close"},
        )

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

    def test_briddle_messages_explain_winner_and_wrong_answer_settings(self) -> None:
        riddle = make_riddle(
            mode="briddle",
            winner_limit=3,
            wrong_answer_visibility="after_close",
            answer_limit=100,
        )

        open_content = self.cog._open_content(
            riddle,
            creator_name="作成者",
            thread=None,
        )
        prompt_content = self.cog._answer_prompt_content(riddle)
        modal_notice = self.cog._answer_modal_notice(riddle)

        self.assertIn("3人正解", open_content)
        self.assertIn("問題終了時に一括公開", open_content)
        self.assertIn("1人100回", open_content)
        self.assertIn("回答経験者", open_content)
        self.assertIn("3人の正解", prompt_content)
        self.assertIn("問題終了時", prompt_content)
        self.assertIn("1人100回", prompt_content)
        self.assertIn("回答したことのある", prompt_content)
        self.assertIn("問題終了時", modal_notice or "")
        self.assertIn("メンション対象", modal_notice or "")

    def test_final_message_controls_winner_mentions(self) -> None:
        riddle = make_riddle(status="expired")
        first_winner_at = riddle.created_at + timedelta(minutes=1, seconds=35)

        content, no_mentions = self.cog._finished_content(
            riddle,
            [100, 101],
            False,
            None,
            first_winner_at=first_winner_at,
        )
        mentioned_content, mentions = self.cog._finished_content(
            riddle,
            [100, 101],
            True,
            None,
        )

        self.assertIn(riddle.answer_display, content)
        self.assertIn("最初の正解まで：** 1分35秒", content)
        self.assertIn("感想戦", content)
        self.assertFalse(no_mentions.users)
        self.assertIn("<@100>", mentioned_content)
        self.assertEqual([user.id for user in mentions.users], [100, 101])

    def test_elapsed_time_uses_readable_two_unit_format(self) -> None:
        started_at = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(
            self.cog._format_elapsed_time(
                started_at,
                started_at + timedelta(days=1, hours=2, minutes=3),
            ),
            "1日2時間",
        )
        self.assertEqual(
            self.cog._format_elapsed_time(
                started_at,
                started_at + timedelta(seconds=42),
            ),
            "42秒",
        )
        self.assertEqual(
            self.cog._format_elapsed_time(
                started_at,
                started_at - timedelta(seconds=1),
            ),
            "0秒",
        )

    def test_ogiri_messages_use_manual_judging_wording(self) -> None:
        riddle = make_riddle(
            answer_display="",
            answers_json="[]",
            public_answers=True,
            manual_judging=True,
            status="expired",
        )

        open_content = self.cog._open_content(
            riddle,
            creator_name="出題者",
            thread=None,
        )
        final_content, _mentions = self.cog._finished_content(
            riddle,
            [100],
            False,
            None,
        )

        self.assertIn("大喜利", open_content)
        self.assertIn(ACCEPT_ANSWER_EMOJI, open_content)
        self.assertIn("採用された回答者", final_content)
        self.assertIn(ACCEPTED_ANSWER_EMOJI, final_content)
        self.assertNotIn("正解：", final_content)

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

    def test_reaction_emoji_parser_accepts_one_unicode_or_guild_emoji(self) -> None:
        class FakeEmoji:
            def __str__(self) -> str:
                return "<:party:123456789012345>"

            def is_usable(self) -> bool:
                return True

        guild = SimpleNamespace(
            get_emoji=lambda emoji_id: (
                FakeEmoji() if emoji_id == 123456789012345 else None
            )
        )

        self.assertEqual(self.cog._parse_reaction_emoji(guild, "✅"), "✅")
        self.assertEqual(self.cog._parse_reaction_emoji(guild, "👨‍👩‍👧‍👦"), "👨‍👩‍👧‍👦")
        self.assertEqual(self.cog._parse_reaction_emoji(guild, "🇯🇵"), "🇯🇵")
        self.assertEqual(
            self.cog._parse_reaction_emoji(
                guild,
                "<:party:123456789012345>",
            ),
            "<:party:123456789012345>",
        )
        for invalid in (
            "",
            "hello",
            "✅✅",
            "<:missing:999999999999999>",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.cog._parse_reaction_emoji(guild, invalid)

        custom = self.cog._reaction_value("<:party:123456789012345>")
        self.assertIsInstance(custom, discord.PartialEmoji)
        self.assertEqual(custom.id, 123456789012345)
        self.assertTrue(
            self.cog._emoji_matches(
                discord.PartialEmoji(name="renamed", id=123456789012345),
                "<:party:123456789012345>",
            )
        )


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
            add_reactions=True,
        )


class FakePublicThread:
    def __init__(self, *, archived: bool = False, thread_id: int = 31) -> None:
        self.archived = archived
        self.id = thread_id
        self.edit = AsyncMock()
        self.sent_message = SimpleNamespace(
            id=900,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )
        self.send = AsyncMock(return_value=self.sent_message)
        self.fetch_message = AsyncMock(return_value=self.sent_message)
        self.delete = AsyncMock()


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
            add_reaction=AsyncMock(),
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
        message.add_reaction.assert_awaited_once_with("⭐")
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
            add_reaction=AsyncMock(),
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
            add_reaction=AsyncMock(),
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

    async def test_ogiri_creation_uses_public_manual_judging(self) -> None:
        self.database.update_user_preferences(10, 50, answer_limit=4)
        self.database.update_guild_settings(
            10,
            answer_accept_emoji="☑️",
            ogiri_mvp_emoji="🏅",
            good_question_emoji="💯",
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
            add_reaction=AsyncMock(),
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
                question="写真で一言",
                answer=None,
                term="1h",
                media=None,
                public_answers=True,
                manual_judging=True,
            )

        created = self.database.list_active_riddles(10)[0]
        self.assertTrue(created.manual_judging)
        self.assertTrue(created.public_answers)
        self.assertEqual(created.answer_limit, 4)
        self.assertEqual(created.normalized_answers, ())
        self.assertEqual(created.answer_accept_emoji, "☑️")
        self.assertEqual(created.ogiri_mvp_emoji, "🏅")
        self.assertEqual(created.good_question_emoji, "💯")
        self.assertIn("出題者", prompt.edit.await_args.kwargs["content"])
        self.assertIn("☑️", prompt.edit.await_args.kwargs["content"])
        self.assertIn("🏅", prompt.edit.await_args.kwargs["content"])
        message.add_reaction.assert_awaited_once_with("💯")
        self.assertTrue(
            message.create_thread.await_args.kwargs["name"].startswith("大喜利-")
        )

    async def test_deferred_briddle_defaults_to_finite_answer_limit(self) -> None:
        prompt = SimpleNamespace(id=40, edit=AsyncMock())
        thread = SimpleNamespace(
            id=30,
            mention="<#30>",
            send=AsyncMock(return_value=prompt),
        )
        message = SimpleNamespace(
            create_thread=AsyncMock(return_value=thread),
            edit=AsyncMock(),
            add_reaction=AsyncMock(),
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
                mode="briddle",
                question="終了時公開テスト",
                answer="正解",
                term="1h",
                media=None,
                wrong_answer_visibility="after_close",
            )

        created = self.database.list_active_riddles(10)[0]
        self.assertEqual(created.answer_limit, 100)
        self.assertIn(
            "1人100回",
            prompt.edit.await_args.kwargs["content"],
        )


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
        manual_judging: bool = False,
        winner_limit: int = 1,
        wrong_answer_visibility: str = "private",
        answer_display: str = "正解",
        answer_accept_emoji: str = "✅",
        ogiri_mvp_emoji: str = "👑",
        good_question_emoji: str = "⭐",
    ) -> Riddle:
        return self.database.create_riddle(
            guild_id=1,
            channel_id=10,
            creator_id=100,
            mode=mode,
            question="テスト問題",
            answer_display="" if manual_judging else answer_display,
            deadline_at=deadline_at or self.now + timedelta(hours=1),
            created_at=self.now,
            thread_id=message_id + 1,
            message_id=message_id,
            public_answers=public_answers,
            answer_limit=answer_limit,
            manual_judging=manual_judging,
            winner_limit=winner_limit,
            wrong_answer_visibility=wrong_answer_visibility,
            answer_accept_emoji=answer_accept_emoji,
            ogiri_mvp_emoji=ogiri_mvp_emoji,
            good_question_emoji=good_question_emoji,
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
        self.cog._announce_briddle_correct_answer = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]

        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=400,
            submitted_answer="正解",
        )

        self.assertIn("正解です", interaction.edited_content or "")
        self.assertIn("感想戦", interaction.edited_content or "")
        self.assertEqual(self.database.get_riddle(riddle.id).status, "solved")
        self.cog._announce_briddle_correct_answer.assert_awaited_once()
        self.cog._publish_finalization.assert_awaited_once_with(riddle.id)

    async def test_briddle_waits_for_configured_number_of_winners(self) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=410,
            winner_limit=2,
        )
        self.cog._announce_briddle_correct_answer = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]
        first_interaction = FakeAnswerInteraction(501)
        second_interaction = FakeAnswerInteraction(502)

        await self.cog.handle_answer(
            first_interaction,  # type: ignore[arg-type]
            source_message_id=410,
            submitted_answer="正解",
        )

        self.assertEqual(self.database.get_riddle(riddle.id).status, "active")
        self.assertIn("1/2人", first_interaction.edited_content or "")
        self.cog._publish_finalization.assert_not_awaited()

        await self.cog.handle_answer(
            second_interaction,  # type: ignore[arg-type]
            source_message_id=410,
            submitted_answer="正解",
        )

        self.assertEqual(self.database.get_riddle(riddle.id).status, "solved")
        self.assertEqual(self.database.list_winner_ids(riddle.id), (501, 502))
        self.assertIn("2/2人", second_interaction.edited_content or "")
        self.assertEqual(self.cog._announce_briddle_correct_answer.await_count, 2)
        self.cog._publish_finalization.assert_awaited_once_with(riddle.id)

    async def test_briddle_immediately_publishes_wrong_answer_when_selected(
        self,
    ) -> None:
        self.create_bound_riddle(
            mode="briddle",
            message_id=415,
            wrong_answer_visibility="immediate",
        )
        self.cog._publish_briddle_wrong_answer = AsyncMock(  # type: ignore[method-assign]
            return_value=True
        )
        interaction = FakeAnswerInteraction(501)

        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=415,
            submitted_answer="まちがい",
        )

        self.cog._publish_briddle_wrong_answer.assert_awaited_once()
        self.assertEqual(
            self.cog._publish_briddle_wrong_answer.await_args.args[2],
            "まちがい",
        )
        self.assertIn("スレッドへ公開しました", interaction.edited_content or "")

    async def test_briddle_wrong_answer_publication_blocks_mentions_and_embeds(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=417,
            wrong_answer_visibility="immediate",
        )
        thread = FakePublicThread(archived=True, thread_id=riddle.thread_id or 0)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            published = await self.cog._publish_briddle_wrong_answer(
                riddle,
                SimpleNamespace(id=501, display_name="回答者"),
                "@everyone https://example.invalid/",
                1,
            )

        self.assertTrue(published)
        thread.edit.assert_awaited_once_with(archived=False)
        content = thread.send.await_args.args[0]
        kwargs = thread.send.await_args.kwargs
        self.assertNotIn("@everyone", content)
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertTrue(kwargs["suppress_embeds"])

    async def test_briddle_correct_announcement_mentions_creator_and_participants(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=418,
            winner_limit=2,
            answer_display="秘密の答え",
        )
        self.database.submit_answer(
            riddle.id,
            700,
            "不正解1",
            now=self.now + timedelta(seconds=1),
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "不正解2",
            now=self.now + timedelta(seconds=2),
        )
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            published = await self.cog._announce_briddle_correct_answer(
                riddle,
                SimpleNamespace(id=501, display_name="Alice"),
                1,
            )

        self.assertTrue(published)
        content = thread.send.await_args.args[0]
        kwargs = thread.send.await_args.kwargs
        self.assertIn("Aliceが正解しました", content)
        self.assertIn("1/2人", content)
        self.assertIn("<@100>", content)
        self.assertIn("<@501>", content)
        self.assertIn("<@700>", content)
        self.assertNotIn(riddle.answer_display, content)
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertEqual(
            [user.id for user in kwargs["allowed_mentions"].users],
            [100, 501, 700],
        )
        self.assertFalse(kwargs["allowed_mentions"].roles)

    async def test_briddle_correct_announcement_mentions_creator_without_attempts(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=419,
            winner_limit=2,
        )
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            published = await self.cog._announce_briddle_correct_answer(
                riddle,
                SimpleNamespace(id=501, display_name="Alice"),
                1,
            )

        self.assertTrue(published)
        content = thread.send.await_args.args[0]
        allowed_mentions = thread.send.await_args.kwargs["allowed_mentions"]
        self.assertIn("<@100>", content)
        self.assertEqual(
            [user.id for user in allowed_mentions.users],
            [100],
        )
        self.assertFalse(allowed_mentions.roles)

    async def test_briddle_correct_announcement_batches_all_participants(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=420,
            winner_limit=2,
        )
        participant_ids = tuple(range(1_000, 1_051))
        for offset, participant_id in enumerate(participant_ids, start=1):
            self.database.submit_answer(
                riddle.id,
                participant_id,
                f"不正解{offset}",
                now=self.now + timedelta(seconds=offset),
            )
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            published = await self.cog._announce_briddle_correct_answer(
                riddle,
                SimpleNamespace(id=501, display_name="Alice"),
                1,
            )

        self.assertTrue(published)
        self.assertEqual(thread.send.await_count, 2)
        mentioned_ids = {
            user.id
            for call in thread.send.await_args_list
            for user in call.kwargs["allowed_mentions"].users
        }
        self.assertEqual(mentioned_ids, {riddle.creator_id, *participant_ids})
        self.assertTrue(
            all(
                len(call.kwargs["allowed_mentions"].users) <= 50
                for call in thread.send.await_args_list
            )
        )

    async def test_briddle_correct_announcement_retries_transient_http_error(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=421,
            winner_limit=2,
        )
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        thread.send.side_effect = [
            discord.HTTPException(
                SimpleNamespace(status=500, reason="Server Error"),
                "temporary",
            ),
            thread.sent_message,
        ]
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.asyncio.sleep",
                new=AsyncMock(),
            ) as sleep,
        ):
            published = await self.cog._announce_briddle_correct_answer(
                riddle,
                SimpleNamespace(id=501, display_name="Alice"),
                1,
            )

        self.assertTrue(published)
        self.assertEqual(thread.send.await_count, 2)
        sleep.assert_awaited_once_with(0.5)

    async def test_briddle_defers_wrong_answer_until_close_when_selected(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=420,
            wrong_answer_visibility="after_close",
        )
        interaction = FakeAnswerInteraction(501)

        await self.cog.handle_answer(
            interaction,  # type: ignore[arg-type]
            source_message_id=420,
            submitted_answer="あとで公開する誤答",
        )

        self.assertIn("問題終了時に公開", interaction.edited_content or "")
        self.database.mark_riddle_expired(
            riddle.id,
            now=self.now + timedelta(hours=2),
        )
        pending = self.database.list_pending_deferred_answers(riddle.id)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].answer_body, "あとで公開する誤答")

        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        await self.cog._publish_deferred_briddle_answers(riddle, thread)  # type: ignore[arg-type]

        published_content = thread.send.await_args.args[0]
        published_kwargs = thread.send.await_args.kwargs
        self.assertIn("終了時公開の不正解", published_content)
        self.assertIn("あとで公開する誤答", published_content)
        self.assertFalse(published_kwargs["allowed_mentions"].everyone)
        self.assertTrue(published_kwargs["suppress_embeds"])
        self.assertEqual(
            self.database.list_pending_deferred_answers(riddle.id),
            [],
        )
        await self.cog._publish_deferred_briddle_answers(riddle, thread)  # type: ignore[arg-type]
        self.assertEqual(thread.send.await_count, 1)

    async def test_briddle_answer_limit_rejects_extra_attempt(self) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=423,
            answer_limit=1,
        )
        first_interaction = FakeAnswerInteraction(501)
        second_interaction = FakeAnswerInteraction(501)

        await self.cog.handle_answer(
            first_interaction,  # type: ignore[arg-type]
            source_message_id=riddle.message_id or 0,
            submitted_answer="不正解",
        )
        self.cog._answer_cooldowns.clear()
        await self.cog.handle_answer(
            second_interaction,  # type: ignore[arg-type]
            source_message_id=riddle.message_id or 0,
            submitted_answer="もう一度",
        )

        self.assertIn("1人1回まで", second_interaction.edited_content or "")
        self.assertEqual(
            self.database.get_answer_attempt_count(riddle.id, 501),
            1,
        )

    async def test_deferred_briddle_publication_reads_multiple_pages(self) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=424,
            wrong_answer_visibility="after_close",
        )
        for index in range(101):
            self.database.submit_answer(
                riddle.id,
                501,
                f"ページ境界の誤答{index}",
                now=self.now + timedelta(seconds=index + 1),
            )
        self.database.mark_riddle_expired(
            riddle.id,
            now=self.now + timedelta(hours=2),
        )
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)

        await self.cog._publish_deferred_briddle_answers(riddle, thread)  # type: ignore[arg-type]

        published = "\n".join(call.args[0] for call in thread.send.await_args_list)
        self.assertIn("ページ境界の誤答0", published)
        self.assertIn("ページ境界の誤答100", published)
        self.assertEqual(
            self.database.list_pending_deferred_answers(riddle.id),
            [],
        )

    async def test_finalization_keeps_thread_open_for_discussion(self) -> None:
        riddle = self.create_bound_riddle(mode="briddle", message_id=425)
        self.database.submit_answer(
            riddle.id,
            501,
            "正解",
            now=self.now + timedelta(minutes=1, seconds=35),
        )
        solved = self.database.get_riddle(riddle.id)
        starter = SimpleNamespace(edit=AsyncMock())
        prompt = SimpleNamespace(edit=AsyncMock())
        thread = FakePublicThread(thread_id=solved.thread_id or 0)
        thread.fetch_message = AsyncMock(return_value=prompt)
        parent = FakeTextChannel(channel_id=solved.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                thread if channel_id == solved.thread_id else parent
            )
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.discord.TextChannel",
                FakeTextChannel,
            ),
        ):
            await self.cog._publish_finalization(riddle.id)

        final_content = starter.edit.await_args.kwargs["content"]
        self.assertIn("最初の正解まで：** 1分35秒", final_content)
        self.assertIn("感想戦", final_content)
        self.assertIn("感想戦", prompt.edit.await_args.kwargs["content"])
        thread.edit.assert_awaited_with(archived=False, locked=False)
        self.assertNotIn(
            unittest.mock.call(archived=True, locked=True),
            thread.edit.await_args_list,
        )
        finalized = self.database.get_riddle(riddle.id)
        self.assertIsNotNone(finalized)
        self.assertIsNotNone(finalized.finalized_at)
        self.assertEqual(finalized.thread_id, riddle.thread_id)
        self.assertEqual(finalized.message_id, riddle.message_id)
        self.assertEqual(finalized.channel_id, riddle.channel_id)
        self.assertEqual(finalized.question, "")
        self.assertEqual(finalized.answer_display, "")
        self.assertEqual(finalized.normalized_answers, ())
        self.assertIsNone(finalized.winner_id)
        self.assertEqual(self.database.list_winner_ids(riddle.id), ())
        self.assertEqual(
            self.database.get_answer_attempt_count(riddle.id, 501),
            0,
        )
        self.assertEqual(self.database.list_pending_finalizations(), [])

    async def test_finalization_publishes_deferred_briddle_answers(self) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=427,
            wrong_answer_visibility="after_close",
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "終了時に公開する誤答",
            now=self.now + timedelta(minutes=1),
        )
        self.database.mark_riddle_expired(
            riddle.id,
            now=self.now + timedelta(hours=2),
        )
        expired = self.database.get_riddle(riddle.id)
        starter = SimpleNamespace(edit=AsyncMock())
        prompt = SimpleNamespace(edit=AsyncMock())
        thread = FakePublicThread(thread_id=expired.thread_id or 0)
        thread.fetch_message = AsyncMock(return_value=prompt)
        parent = FakeTextChannel(channel_id=expired.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                thread if channel_id == expired.thread_id else parent
            )
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.discord.TextChannel",
                FakeTextChannel,
            ),
        ):
            await self.cog._publish_finalization(riddle.id)

        self.assertEqual(thread.send.await_count, 1)
        self.assertIn(
            "終了時に公開する誤答",
            thread.send.await_args.args[0],
        )
        finalized = self.database.get_riddle(riddle.id)
        self.assertIsNotNone(finalized)
        self.assertIsNotNone(finalized.finalized_at)
        self.assertEqual(finalized.thread_id, riddle.thread_id)
        self.assertEqual(finalized.message_id, riddle.message_id)
        self.assertEqual(finalized.question, "")
        self.assertEqual(finalized.answer_display, "")
        self.assertEqual(finalized.normalized_answers, ())
        self.assertIsNone(finalized.winner_id)
        self.assertEqual(
            self.database.list_pending_deferred_answers(riddle.id),
            [],
        )
        self.assertEqual(
            self.database.get_answer_attempt_count(riddle.id, 501),
            0,
        )
        self.assertEqual(self.database.list_pending_finalizations(), [])

    async def test_finalization_retries_when_discussion_thread_cannot_reopen(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(mode="briddle", message_id=428)
        self.database.submit_answer(
            riddle.id,
            501,
            "正解",
            now=self.now + timedelta(minutes=1),
        )
        solved = self.database.get_riddle(riddle.id)
        starter = SimpleNamespace(edit=AsyncMock())
        prompt = SimpleNamespace(edit=AsyncMock())
        thread = FakePublicThread(thread_id=solved.thread_id or 0)
        thread.fetch_message = AsyncMock(return_value=prompt)
        thread.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "denied",
        )
        parent = FakeTextChannel(channel_id=solved.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                thread if channel_id == solved.thread_id else parent
            )
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.discord.TextChannel",
                FakeTextChannel,
            ),
            self.assertRaisesRegex(RuntimeError, "感想戦用スレッド"),
        ):
            await self.cog._publish_finalization(riddle.id)

        pending = self.database.get_riddle(riddle.id)
        self.assertIsNotNone(pending)
        self.assertIsNone(pending.finalized_at)
        self.assertEqual(self.database.list_pending_finalizations(), [pending])

    async def test_finalized_riddle_is_not_published_twice_when_called_directly(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=432,
            deadline_at=self.now - timedelta(seconds=1),
        )
        self.database.mark_riddle_expired(riddle.id, now=self.now)
        finalized = self.database.finalize_riddle(riddle.id, now=self.now)
        self.assertIsNotNone(finalized)
        self.cog._resolve_channel = AsyncMock()  # type: ignore[method-assign]

        await self.cog._publish_finalization(riddle.id)

        self.cog._resolve_channel.assert_not_awaited()
        self.assertEqual(self.database.get_riddle(riddle.id), finalized)

    async def test_deferred_answers_wait_until_thread_is_unlocked(self) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=431,
            wrong_answer_visibility="after_close",
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "ロック解除後に公開する誤答",
            now=self.now + timedelta(minutes=1),
        )
        self.database.mark_riddle_expired(
            riddle.id,
            now=self.now + timedelta(hours=2),
        )
        expired = self.database.get_riddle(riddle.id)
        starter = SimpleNamespace(edit=AsyncMock())
        prompt = SimpleNamespace(edit=AsyncMock())
        thread = FakePublicThread(thread_id=expired.thread_id or 0)
        thread.fetch_message = AsyncMock(return_value=prompt)
        thread.edit.side_effect = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "denied",
        )
        parent = FakeTextChannel(channel_id=expired.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                thread if channel_id == expired.thread_id else parent
            )
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.discord.TextChannel",
                FakeTextChannel,
            ),
            self.assertRaisesRegex(RuntimeError, "感想戦用スレッド"),
        ):
            await self.cog._publish_finalization(riddle.id)

        thread.send.assert_not_awaited()
        self.assertEqual(
            len(self.database.list_pending_deferred_answers(riddle.id)),
            1,
        )
        self.assertIsNotNone(self.database.get_riddle(riddle.id))

    async def test_finalization_warns_if_deferred_answer_thread_is_missing(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=429,
            wrong_answer_visibility="after_close",
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "公開できない誤答",
            now=self.now + timedelta(minutes=1),
        )
        self.database.mark_riddle_expired(
            riddle.id,
            now=self.now + timedelta(hours=2),
        )
        expired = self.database.get_riddle(riddle.id)
        starter = SimpleNamespace(edit=AsyncMock())
        parent = FakeTextChannel(channel_id=expired.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                None if channel_id == expired.thread_id else parent
            )
        )

        with patch(
            "riddle_bot.discord_commands.discord.TextChannel",
            FakeTextChannel,
        ):
            await self.cog._publish_finalization(riddle.id)

        final_content = starter.edit.await_args.kwargs["content"]
        self.assertIn("不正解は表示できませんでした", final_content)
        self.assertIsNone(self.database.get_riddle(riddle.id))

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

    async def test_public_answer_failure_log_never_contains_answer_body(self) -> None:
        self.create_bound_riddle(
            mode="riddle",
            message_id=650,
            public_answers=True,
        )
        secret_answer = "LOG-NOT-ALLOWED-ANSWER"
        self.cog._publish_public_answer = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError(secret_answer)
        )
        interaction = FakeAnswerInteraction(501)

        with self.assertLogs(
            "riddle_bot.discord_commands",
            level="WARNING",
        ) as captured:
            await self.cog.handle_answer(
                interaction,  # type: ignore[arg-type]
                source_message_id=650,
                submitted_answer=secret_answer,
            )

        self.assertNotIn(secret_answer, "\n".join(captured.output))
        self.assertIn(
            "公開回答をスレッドへ表示できません", interaction.edited_content or ""
        )

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
        self.database.submit_answer(
            riddle.id,
            501,
            "@everyone https://example.invalid/",
            now=self.now + timedelta(minutes=1),
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
                SimpleNamespace(id=501, display_name="回答者"),
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
        thread.sent_message.add_reaction.assert_awaited_once_with("✅")

    async def test_hint_text_and_media_are_forced_into_spoilers(self) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=760,
            public_answers=True,
        )
        uploaded_file = discord.File(
            io.BytesIO(b"\x89PNG\r\n\x1a\nsafe"),
            filename="original-secret-name.png",
            description="secret description",
        )
        media = SimpleNamespace(
            content_type="image/png",
            filename="original-secret-name.png",
            size=12,
            to_file=AsyncMock(return_value=uploaded_file),
        )
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                id=1,
                filesize_limit=MEDIA_MAX_SIZE_BYTES,
            ),
            guild_id=1,
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            await self.cog.hint.callback(
                self.cog,
                interaction,  # type: ignore[arg-type]
                riddle.id,
                "中で || 閉じる @everyone "
                "[リンク](https://example.invalid/||URLから脱出||)",
                media,  # type: ignore[arg-type]
            )

        media.to_file.assert_awaited_once_with(spoiler=True)
        content = thread.send.await_args.args[0]
        options = thread.send.await_args.kwargs
        self.assertIn("｜｜", content)
        self.assertNotIn("@everyone", content)
        self.assertEqual(content.count("||"), 2)
        self.assertTrue(options["suppress_embeds"])
        self.assertFalse(options["allowed_mentions"].everyone)
        self.assertIs(options["file"], uploaded_file)
        self.assertTrue(uploaded_file.spoiler)
        self.assertEqual(uploaded_file.filename, f"SPOILER_hint-{riddle.id}.png")
        self.assertIsNone(uploaded_file.description)
        self.assertIn(
            "ヒントを追加しました",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_hint_requires_text_or_media(self) -> None:
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            response=SimpleNamespace(
                is_done=lambda: False,
                send_message=AsyncMock(),
            ),
        )

        await self.cog.hint.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            1,
            None,
            None,
        )

        interaction.response.send_message.assert_awaited_once()
        self.assertIn(
            "本文またはメディア",
            interaction.response.send_message.await_args.args[0],
        )

    async def test_max_length_markdown_hint_stays_within_discord_limit(self) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=770)
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(
                id=1,
                filesize_limit=MEDIA_MAX_SIZE_BYTES,
            ),
            guild_id=1,
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            await self.cog.hint.callback(
                self.cog,
                interaction,  # type: ignore[arg-type]
                riddle.id,
                "_*" * 500,
                None,
            )

        content = thread.send.await_args.args[0]
        self.assertLessEqual(len(content), 2_000)
        self.assertEqual(content.count("||"), 2)

    async def test_creator_reactions_accept_and_mark_ogiri_mvp(self) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=780,
            public_answers=True,
            manual_judging=True,
            answer_accept_emoji="<:accept:123456789012345>",
        )
        for user_id, message_id, attempt_count in (
            (501, 900, 1),
            (502, 901, 1),
        ):
            self.database.submit_answer(
                riddle.id,
                user_id,
                f"回答{user_id}",
                now=self.now + timedelta(minutes=1),
            )
            self.database.register_public_answer_message(
                message_id=message_id,
                riddle_id=riddle.id,
                user_id=user_id,
                attempt_count=attempt_count,
            )

        messages = {
            message_id: SimpleNamespace(add_reaction=AsyncMock())
            for message_id in (900, 901)
        }
        messages[901].add_reaction.side_effect = [
            discord.HTTPException(
                SimpleNamespace(status=500, reason="Server Error"),
                "temporary failure",
            ),
            None,
        ]
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        thread.fetch_message = AsyncMock(
            side_effect=lambda message_id: messages[message_id]
        )
        self.cog.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            config=SimpleNamespace(guild_id=1),
        )
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            return_value=thread
        )

        def payload(
            *,
            user_id: int,
            message_id: int,
            emoji_name: str,
            emoji_id: int | None,
        ) -> SimpleNamespace:
            return SimpleNamespace(
                guild_id=1,
                channel_id=riddle.thread_id,
                message_id=message_id,
                user_id=user_id,
                emoji=SimpleNamespace(name=emoji_name, id=emoji_id),
            )

        with patch(
            "riddle_bot.discord_commands.discord.Thread",
            FakePublicThread,
        ):
            await self.cog.on_raw_reaction_add(
                payload(
                    user_id=777,
                    message_id=900,
                    emoji_name="accept",
                    emoji_id=123456789012345,
                )  # type: ignore[arg-type]
            )
            await self.cog.on_raw_reaction_add(
                payload(
                    user_id=100,
                    message_id=900,
                    emoji_name="accept",
                    emoji_id=123456789012345,
                )  # type: ignore[arg-type]
            )
            await self.cog.on_raw_reaction_add(
                payload(
                    user_id=100,
                    message_id=901,
                    emoji_name="👑",
                    emoji_id=None,
                )  # type: ignore[arg-type]
            )
            # 最初の公式マーク追加が失敗しても、同一イベントの再送で修復する。
            await self.cog.on_raw_reaction_add(
                payload(
                    user_id=100,
                    message_id=901,
                    emoji_name="👑",
                    emoji_id=None,
                )  # type: ignore[arg-type]
            )

        self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))
        self.assertEqual(self.database.list_mvp_user_ids(riddle.id), (502,))
        messages[900].add_reaction.assert_awaited_once_with("🏆")
        self.assertEqual(messages[901].add_reaction.await_count, 2)
        messages[901].add_reaction.assert_awaited_with("🏆")

    async def test_reaction_judging_uses_event_arrival_time_before_lock_wait(
        self,
    ) -> None:
        deadline = self.now + timedelta(seconds=1)
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=790,
            deadline_at=deadline,
            public_answers=True,
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "想定外だけど正解",
            now=self.now,
        )
        self.database.register_public_answer_message(
            message_id=910,
            riddle_id=riddle.id,
            user_id=501,
            attempt_count=1,
        )
        message = SimpleNamespace(add_reaction=AsyncMock())
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        thread.fetch_message = AsyncMock(return_value=message)
        self.cog.bot = SimpleNamespace(
            user=SimpleNamespace(id=999),
            config=SimpleNamespace(guild_id=1),
        )
        self.cog._resolve_channel = AsyncMock(return_value=thread)  # type: ignore[method-assign]
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]

        clock = {"now": self.now}

        class AdvancingLock:
            async def __aenter__(inner_self):
                clock["now"] = deadline + timedelta(seconds=1)
                return inner_self

            async def __aexit__(
                self,
                _exception_type: object,
                _exception: object,
                _traceback: object,
            ) -> None:
                return None

        class ControlledDateTime:
            @classmethod
            def now(cls, _timezone: object = None) -> datetime:
                return clock["now"]

        self.cog._riddle_operation_lock = AdvancingLock()  # type: ignore[assignment]
        payload = SimpleNamespace(
            guild_id=1,
            channel_id=riddle.thread_id,
            message_id=910,
            user_id=100,
            emoji=SimpleNamespace(name="✅", id=None),
        )

        with (
            patch("riddle_bot.discord_commands.datetime", ControlledDateTime),
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
        ):
            await self.cog.on_raw_reaction_add(payload)  # type: ignore[arg-type]

        self.assertEqual(self.database.list_winner_ids(riddle.id), (501,))
        message.add_reaction.assert_awaited_once_with("🏆")
        self.cog._publish_finalization.assert_not_awaited()

    async def test_pref_updates_evaluation_emoji_defaults(self) -> None:
        guild = SimpleNamespace(id=1, get_emoji=lambda _emoji_id: None)
        interaction = SimpleNamespace(
            guild=guild,
            guild_id=1,
            user=SimpleNamespace(id=501),
            channel=None,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with patch(
            "riddle_bot.discord_commands.is_administrator",
            return_value=True,
        ):
            for setting_name, emoji in (
                ("answer_accept_emoji", "☑️"),
                ("ogiri_mvp_emoji", "🏅"),
                ("good_question_emoji", "💯"),
            ):
                await self.cog.pref.callback(
                    self.cog,
                    interaction,  # type: ignore[arg-type]
                    SimpleNamespace(value=setting_name),
                    emoji,
                )

        settings = self.database.get_guild_settings(1)
        self.assertEqual(settings.answer_accept_emoji, "☑️")
        self.assertEqual(settings.ogiri_mvp_emoji, "🏅")
        self.assertEqual(settings.good_question_emoji, "💯")

    async def test_pref_rejects_reserved_official_evaluation_mark(self) -> None:
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1, get_emoji=lambda _emoji_id: None),
            guild_id=1,
            user=SimpleNamespace(id=501),
            channel=None,
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with patch(
            "riddle_bot.discord_commands.is_administrator",
            return_value=True,
        ):
            await self.cog.pref.callback(
                self.cog,
                interaction,  # type: ignore[arg-type]
                SimpleNamespace(value="answer_accept_emoji"),
                "🏆️",
            )

        content = interaction.edit_original_response.await_args.kwargs["content"]
        self.assertIn("公式記録マーク", content)
        self.assertEqual(
            self.database.get_guild_settings(1).answer_accept_emoji,
            "✅",
        )

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

    async def test_creator_can_close_every_problem_mode(self) -> None:
        riddles = (
            self.create_bound_riddle(mode="riddle", message_id=801),
            self.create_bound_riddle(mode="briddle", message_id=802),
            self.create_bound_riddle(
                mode="riddle",
                message_id=803,
                public_answers=True,
                manual_judging=True,
            ),
        )
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]

        for riddle in riddles:
            with self.subTest(riddle_id=riddle.id):
                interaction = SimpleNamespace(
                    guild=SimpleNamespace(id=1),
                    guild_id=1,
                    user=SimpleNamespace(id=100, display_name="作成者"),
                    response=SimpleNamespace(defer=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )

                await self.cog.close_riddle.callback(
                    self.cog,
                    interaction,  # type: ignore[arg-type]
                    riddle.id,
                )

                closed = self.database.get_riddle(riddle.id)
                self.assertEqual(closed.status, "expired")
                self.assertIsNotNone(closed.closed_early_at)
                self.assertIsNone(closed.finalized_at)
                self.assertIn(
                    "結果を発表しました",
                    interaction.edit_original_response.await_args.kwargs["content"],
                )

        self.assertEqual(self.cog._publish_finalization.await_count, 3)

    async def test_close_briddle_publishes_partial_result_and_deferred_answers(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(
            mode="briddle",
            message_id=804,
            winner_limit=2,
            wrong_answer_visibility="after_close",
        )
        self.database.submit_answer(
            riddle.id,
            501,
            "あとで公開する誤答",
            now=self.now + timedelta(minutes=1),
        )
        self.database.submit_answer(
            riddle.id,
            502,
            "正解",
            now=self.now + timedelta(minutes=2),
        )
        starter = SimpleNamespace(edit=AsyncMock())
        prompt = SimpleNamespace(edit=AsyncMock())
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        thread.fetch_message = AsyncMock(return_value=prompt)
        parent = FakeTextChannel(channel_id=riddle.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog.bot = SimpleNamespace(get_guild=lambda _guild_id: None)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                thread if channel_id == riddle.thread_id else parent
            )
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.discord.TextChannel",
                FakeTextChannel,
            ),
        ):
            await self.cog.close_riddle.callback(
                self.cog,
                interaction,  # type: ignore[arg-type]
                riddle.id,
            )

        final_content = starter.edit.await_args.kwargs["content"]
        self.assertIn("回答受付を締め切りました", final_content)
        self.assertIn("<@502>", final_content)
        self.assertIn("あとで公開する誤答", thread.send.await_args.args[0])
        finalized = self.database.get_riddle(riddle.id)
        self.assertIsNotNone(finalized.finalized_at)
        self.assertIsNotNone(finalized.closed_early_at)
        self.assertEqual(finalized.thread_id, riddle.thread_id)
        self.assertEqual(finalized.question, "")
        self.assertEqual(finalized.answer_display, "")
        self.assertEqual(finalized.normalized_answers, ())
        self.assertIsNone(finalized.winner_id)
        self.assertEqual(self.database.list_winner_ids(riddle.id), ())
        self.assertEqual(
            self.database.list_pending_deferred_answers(riddle.id),
            [],
        )
        self.assertIn(
            "結果を発表しました",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_close_rejects_noncreator_and_other_guild(self) -> None:
        unauthorized = self.create_bound_riddle(mode="riddle", message_id=805)
        wrong_guild = self.create_bound_riddle(mode="riddle", message_id=806)
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]

        unauthorized_interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=501, display_name="別ユーザー"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await self.cog.close_riddle.callback(
            self.cog,
            unauthorized_interaction,  # type: ignore[arg-type]
            unauthorized.id,
        )

        wrong_guild_interaction = SimpleNamespace(
            guild=SimpleNamespace(id=2),
            guild_id=2,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        await self.cog.close_riddle.callback(
            self.cog,
            wrong_guild_interaction,  # type: ignore[arg-type]
            wrong_guild.id,
        )

        self.assertIn(
            "作成者または管理者",
            unauthorized_interaction.edit_original_response.await_args.kwargs[
                "content"
            ],
        )
        self.assertIn(
            "見つかりません",
            wrong_guild_interaction.edit_original_response.await_args.kwargs["content"],
        )
        self.assertEqual(self.database.get_riddle(unauthorized.id).status, "active")
        self.assertEqual(self.database.get_riddle(wrong_guild.id).status, "active")
        self.cog._publish_finalization.assert_not_awaited()

    async def test_admin_can_close_and_repeated_close_is_rejected(self) -> None:
        admin_riddle = self.create_bound_riddle(mode="riddle", message_id=807)
        repeated_riddle = self.create_bound_riddle(mode="riddle", message_id=808)
        self.cog._publish_finalization = AsyncMock()  # type: ignore[method-assign]
        admin_interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=999, display_name="管理者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        creator_interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with patch(
            "riddle_bot.discord_commands.is_administrator",
            return_value=True,
        ):
            await self.cog.close_riddle.callback(
                self.cog,
                admin_interaction,  # type: ignore[arg-type]
                admin_riddle.id,
            )
        await self.cog.close_riddle.callback(
            self.cog,
            creator_interaction,  # type: ignore[arg-type]
            repeated_riddle.id,
        )
        await self.cog.close_riddle.callback(
            self.cog,
            creator_interaction,  # type: ignore[arg-type]
            repeated_riddle.id,
        )

        self.assertEqual(self.database.get_riddle(admin_riddle.id).status, "expired")
        self.assertEqual(self.database.get_riddle(repeated_riddle.id).status, "expired")
        self.assertIn(
            "既に回答受付を終了",
            creator_interaction.edit_original_response.await_args.kwargs["content"],
        )
        self.assertEqual(self.cog._publish_finalization.await_count, 2)

    async def test_close_publish_failure_keeps_problem_pending_for_retry(self) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=809)
        self.cog._publish_finalization = AsyncMock(  # type: ignore[method-assign]
            side_effect=RuntimeError("Discord unavailable")
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await self.cog.close_riddle.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            riddle.id,
        )

        pending = self.database.get_riddle(riddle.id)
        self.assertEqual(pending.status, "expired")
        self.assertIsNotNone(pending.closed_early_at)
        self.assertIsNone(pending.finalized_at)
        self.assertEqual(self.database.list_pending_finalizations(), [pending])
        self.assertIn(
            "定期処理で再試行",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_public_answer_finishes_before_delete_purges_thread(self) -> None:
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
        self.cog._purge_discord_problem = AsyncMock()  # type: ignore[method-assign]
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
        self.cog._purge_discord_problem.assert_not_awaited()

        allow_publication_to_finish.set()
        await asyncio.gather(answer_task, delete_task)

        self.cog._purge_discord_problem.assert_awaited_once()
        self.assertIsNone(self.database.get_riddle(riddle.id))

    async def test_delete_removes_active_thread_starter_and_database(self) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=820)
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        starter = SimpleNamespace(delete=AsyncMock())
        parent = FakeTextChannel(channel_id=riddle.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                thread if channel_id == riddle.thread_id else parent
            )
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.discord.TextChannel",
                FakeTextChannel,
            ),
        ):
            await self.cog.delete.callback(
                self.cog,
                interaction,  # type: ignore[arg-type]
                riddle.id,
            )

        thread.delete.assert_awaited_once()
        parent.fetch_message.assert_awaited_once_with(riddle.thread_id)
        starter.delete.assert_awaited_once()
        self.assertIsNone(self.database.get_riddle(riddle.id))
        self.assertIn(
            "完全削除",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_delete_removes_finalized_thread_starter_and_database(self) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=825,
            deadline_at=self.now - timedelta(seconds=1),
        )
        self.database.mark_riddle_expired(riddle.id, now=self.now)
        finalized = self.database.finalize_riddle(riddle.id, now=self.now)
        self.assertIsNotNone(finalized.finalized_at)
        thread = FakePublicThread(thread_id=riddle.thread_id or 0)
        starter = SimpleNamespace(delete=AsyncMock())
        parent = FakeTextChannel(channel_id=riddle.channel_id)
        parent.fetch_message = AsyncMock(return_value=starter)
        self.cog._resolve_channel = AsyncMock(  # type: ignore[method-assign]
            side_effect=lambda channel_id: (
                thread if channel_id == riddle.thread_id else parent
            )
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        with (
            patch(
                "riddle_bot.discord_commands.discord.Thread",
                FakePublicThread,
            ),
            patch(
                "riddle_bot.discord_commands.discord.TextChannel",
                FakeTextChannel,
            ),
        ):
            await self.cog.delete.callback(
                self.cog,
                interaction,  # type: ignore[arg-type]
                riddle.id,
            )

        thread.delete.assert_awaited_once()
        starter.delete.assert_awaited_once()
        self.assertIsNone(self.database.get_riddle(riddle.id))
        self.assertIn(
            "完全削除",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_delete_keeps_database_when_discord_fails(self) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=830)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        forbidden = discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"),
            "denied",
        )
        self.cog._purge_discord_problem = AsyncMock(  # type: ignore[method-assign]
            side_effect=forbidden
        )

        await self.cog.delete.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            riddle.id,
        )

        self.assertIsNotNone(self.database.get_riddle(riddle.id))
        self.assertIn(
            "DBの問題データは保持",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_delete_rechecks_riddle_before_discord_deletion(self) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=840)
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        self.cog._db = AsyncMock(side_effect=[riddle, None])  # type: ignore[method-assign]
        self.cog._purge_discord_problem = AsyncMock()  # type: ignore[method-assign]

        await self.cog.delete.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            riddle.id,
        )

        self.cog._purge_discord_problem.assert_not_awaited()
        self.assertIn(
            "既に削除",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_delete_completely_removes_problem_after_deadline(self) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=850,
            deadline_at=self.now - timedelta(seconds=1),
        )
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100, display_name="作成者"),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )
        self.cog._purge_discord_problem = AsyncMock()  # type: ignore[method-assign]

        await self.cog.delete.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            riddle.id,
        )

        self.cog._purge_discord_problem.assert_awaited_once()
        self.assertIsNone(self.database.get_riddle(riddle.id))
        self.assertIn(
            "完全削除",
            interaction.edit_original_response.await_args.kwargs["content"],
        )

    async def test_raw_thread_delete_removes_active_and_finalized_rows(self) -> None:
        active = self.create_bound_riddle(mode="riddle", message_id=860)
        finished = self.create_bound_riddle(
            mode="riddle",
            message_id=861,
            deadline_at=self.now - timedelta(seconds=1),
        )
        self.database.mark_riddle_expired(finished.id, now=self.now)
        self.database.finalize_riddle(finished.id, now=self.now)

        for riddle in (active, finished):
            with self.subTest(riddle_id=riddle.id):
                await self.cog.on_raw_thread_delete(
                    SimpleNamespace(thread_id=riddle.thread_id)  # type: ignore[arg-type]
                )
                self.assertIsNone(self.database.get_riddle(riddle.id))

    async def test_raw_thread_delete_ignores_bot_purge_until_command_cleanup(
        self,
    ) -> None:
        riddle = self.create_bound_riddle(mode="riddle", message_id=862)
        self.cog._purging_thread_ids.add(riddle.thread_id or 0)

        await self.cog.on_raw_thread_delete(
            SimpleNamespace(thread_id=riddle.thread_id)  # type: ignore[arg-type]
        )

        self.assertIsNotNone(self.database.get_riddle(riddle.id))
        self.assertNotIn(riddle.thread_id, self.cog._purging_thread_ids)

    async def test_deletemydata_does_not_edit_finalized_result_thread(self) -> None:
        riddle = self.create_bound_riddle(
            mode="riddle",
            message_id=863,
            deadline_at=self.now - timedelta(seconds=1),
        )
        self.database.mark_riddle_expired(riddle.id, now=self.now)
        finalized = self.database.finalize_riddle(riddle.id, now=self.now)
        self.assertIsNotNone(finalized.finalized_at)
        self.cog._close_discord_problem = AsyncMock()  # type: ignore[method-assign]
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            guild_id=1,
            user=SimpleNamespace(id=100),
            response=SimpleNamespace(defer=AsyncMock()),
            edit_original_response=AsyncMock(),
        )

        await self.cog.deletemydata.callback(
            self.cog,
            interaction,  # type: ignore[arg-type]
            True,
        )

        self.cog._close_discord_problem.assert_not_awaited()
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
