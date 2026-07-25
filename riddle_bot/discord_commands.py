from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from riddle_bot.core import deadline_from_term, prepare_answers
from riddle_bot.database import (
    DEFAULT_ANSWER_ACCEPT_EMOJI,
    DEFAULT_GOOD_QUESTION_EMOJI,
    DEFAULT_OGIRI_MVP_EMOJI,
    OFFICIAL_EVALUATION_MARK_EMOJI,
    ActiveRiddleLimitError,
    ManualAcceptanceStatus,
    Riddle,
    RiddleDatabase,
    SubmissionStatus,
    emoji_identity_key,
)

logger = logging.getLogger(__name__)

ANSWER_BUTTON_CUSTOM_ID = "riddle-bot:answer:v1"
QUESTION_MAX_LENGTH = 1_000
ANSWER_MAX_LENGTH = 500
HINT_MAX_LENGTH = 1_000
ANSWER_COOLDOWN_SECONDS = 2.0
MEDIA_MAX_SIZE_BYTES = 25 * 1024 * 1024
MEDIA_CONTENT_TYPE_PREFIXES = ("image/", "audio/", "video/")
BLOCKED_MEDIA_CONTENT_TYPES = frozenset({"image/svg+xml"})
ACCEPT_ANSWER_EMOJI = DEFAULT_ANSWER_ACCEPT_EMOJI
OGIRI_MVP_EMOJI = DEFAULT_OGIRI_MVP_EMOJI
GOOD_QUESTION_EMOJI = DEFAULT_GOOD_QUESTION_EMOJI
ACCEPTED_ANSWER_EMOJI = OFFICIAL_EVALUATION_MARK_EMOJI

NO_MENTIONS = discord.AllowedMentions.none()
HINT_CONTROL_CHARACTER_TRANSLATION = str.maketrans(
    {
        "\\": "＼",
        "|": "｜",
        "@": "＠",
        "<": "＜",
        ">": "＞",
    }
)


async def send_ephemeral(interaction: discord.Interaction, content: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(
            content,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )
    else:
        await interaction.response.send_message(
            content,
            ephemeral=True,
            allowed_mentions=NO_MENTIONS,
        )


def is_administrator(interaction: discord.Interaction) -> bool:
    return (
        isinstance(interaction.user, discord.Member)
        and interaction.user.guild_permissions.administrator
    )


def status_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw)


class ClosedAnswerView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            discord.ui.Button(
                label="受付終了",
                style=discord.ButtonStyle.secondary,
                custom_id=ANSWER_BUTTON_CUSTOM_ID,
                disabled=True,
            )
        )


class AnswerModal(discord.ui.Modal, title="なぞなぞに回答"):
    def __init__(
        self,
        cog: RiddleCog,
        source_message_id: int,
        *,
        public_answers: bool = False,
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.source_message_id = source_message_id
        self.public_answers = public_answers
        self.answer_input = discord.ui.TextInput(
            style=discord.TextStyle.short,
            placeholder="答えを入力してください",
            required=True,
            min_length=1,
            max_length=ANSWER_MAX_LENGTH,
            custom_id="riddle-bot:answer-text",
        )
        self.add_item(
            discord.ui.Label(
                text="回答",
                description=(
                    "入力内容は回答スレッドへ公開されます。"
                    if public_answers
                    else "入力内容は他の参加者には表示されません。"
                ),
                component=self.answer_input,
            )
        )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self.cog.handle_answer(
            interaction,
            source_message_id=self.source_message_id,
            submitted_answer=self.answer_input.value,
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
    ) -> None:
        logger.exception(
            "Unhandled answer modal error (type=%s)",
            type(error).__name__,
        )
        try:
            await send_ephemeral(
                interaction,
                "エラー！：回答を処理できませんでした。管理者に問い合わせてください。",
            )
        except discord.HTTPException:
            logger.warning("Failed to report modal error to the user")


class AnswerView(discord.ui.View):
    def __init__(self, cog: RiddleCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="回答する",
        style=discord.ButtonStyle.primary,
        custom_id=ANSWER_BUTTON_CUSTOM_ID,
        emoji="✍️",
    )
    async def answer(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button,
    ) -> None:
        if not self.cog.is_allowed_guild(interaction):
            await send_ephemeral(
                interaction,
                "エラー！：このBotは別のDiscordサーバー向けに設定されています。",
            )
            return
        if interaction.message is None:
            await send_ephemeral(
                interaction,
                "エラー！：問題を特定できませんでした。",
            )
            return
        riddle = await self.cog.get_riddle_for_source(interaction.message.id)
        if riddle is None or status_value(riddle.status) != "active":
            await send_ephemeral(
                interaction,
                "エラー！：この問題の回答受付は終了しています。",
            )
            return
        await interaction.response.send_modal(
            AnswerModal(
                self.cog,
                interaction.message.id,
                public_answers=riddle.public_answers,
            )
        )

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        _item: discord.ui.Item[Any],
    ) -> None:
        logger.exception(
            "Unhandled answer button error (type=%s)",
            type(error).__name__,
        )
        try:
            await send_ephemeral(
                interaction,
                "エラー！：回答画面を開けませんでした。管理者に問い合わせてください。",
            )
        except discord.HTTPException:
            logger.warning("Failed to report button error to the user")


class RiddleCog(commands.Cog):
    def __init__(
        self,
        bot: commands.Bot,
        database: RiddleDatabase,
    ) -> None:
        self.bot = bot
        self.database = database
        self._answer_cooldowns: dict[tuple[int, int], float] = {}
        # 回答受付・公開投稿・終了処理を直列化し、終了後に遅れて公開回答が
        # スレッドを再オープンする競合を防ぐ。
        self._riddle_operation_lock = asyncio.Lock()
        # /delete purge:true による意図的なthread削除を、
        # on_thread_deleteが途中でDB削除しないよう識別する。
        self._purging_thread_ids: set[int] = set()

    async def _db(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        """同期SQLite操作をDiscordのイベントループ外で実行する。"""

        return await asyncio.to_thread(function, *args, **kwargs)

    def is_allowed_guild(self, interaction: discord.Interaction) -> bool:
        """設定済みの場合は、対象サーバーからの操作だけを許可する。"""

        guild_id = getattr(interaction, "guild_id", None)
        if guild_id is None:
            guild = getattr(interaction, "guild", None)
            guild_id = getattr(guild, "id", None)
        return self._is_allowed_guild_id(guild_id)

    def _is_allowed_guild_id(self, guild_id: int | None) -> bool:
        config = getattr(self.bot, "config", None)
        configured_guild_id = getattr(config, "guild_id", None)
        if configured_guild_id is None:
            return True
        return guild_id == configured_guild_id

    @staticmethod
    def _reaction_value(value: str) -> str | discord.PartialEmoji:
        parsed = discord.PartialEmoji.from_str(value)
        return parsed if parsed.id is not None else value

    @staticmethod
    def _emoji_matches(payload_emoji: discord.PartialEmoji, configured: str) -> bool:
        expected = discord.PartialEmoji.from_str(configured)
        if expected.id is not None:
            return payload_emoji.id == expected.id
        return (
            payload_emoji.id is None
            and payload_emoji.name is not None
            and emoji_identity_key(payload_emoji.name) == emoji_identity_key(configured)
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.is_allowed_guild(interaction):
            return True
        await send_ephemeral(
            interaction,
            "エラー！：このBotは別のDiscordサーバー向けに設定されています。",
        )
        return False

    async def get_riddle_for_source(self, source_message_id: int) -> Riddle | None:
        """回答ボタンのメッセージIDから問題を特定する。"""

        riddle = await self._db(
            self.database.get_riddle_by_message,
            source_message_id,
        )
        if riddle is None:
            # 公開スレッドの起点メッセージIDはthread IDと同じ。
            riddle = await self._db(
                self.database.get_riddle_by_thread,
                source_message_id,
            )
        return riddle

    async def cog_load(self) -> None:
        self.deadline_worker.start()

    def cog_unload(self) -> None:
        self.deadline_worker.cancel()

    async def reconcile_guild_data(self) -> None:
        current_guild_ids = {guild.id for guild in self.bot.guilds}
        stored_guild_ids = set(await self._db(self.database.list_guild_ids))
        for guild_id in stored_guild_ids - current_guild_ids:
            await self._db(self.database.delete_guild_data, guild_id)
            logger.info("Removed orphaned local guild data for %d", guild_id)

        # 結果発表前に再起動した問題もここで即座に再処理する。
        await self._process_deadlines_once()

    @tasks.loop(seconds=15.0, reconnect=True)
    async def deadline_worker(self) -> None:
        try:
            await self._process_deadlines_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            # 1回のDB/Discord障害で定期処理そのものを停止させない。
            logger.error(
                "Deadline worker iteration failed (type=%s)",
                type(error).__name__,
                exc_info=error,
            )

    @deadline_worker.before_loop
    async def before_deadline_worker(self) -> None:
        await self.bot.wait_until_ready()

    @deadline_worker.error
    async def deadline_worker_error(self, error: BaseException) -> None:
        logger.error(
            "Deadline worker iteration failed (type=%s)",
            type(error).__name__,
            exc_info=error,
        )

    async def _process_deadlines_once(self) -> None:
        now = datetime.now(timezone.utc)
        for riddle in await self._db(self.database.list_due_riddles, now):
            async with self._riddle_operation_lock:
                await self._db(
                    self.database.mark_riddle_expired,
                    riddle.id,
                    now=now,
                )

        for riddle in await self._db(self.database.list_pending_finalizations):
            try:
                await self._publish_finalization(riddle.id)
            except discord.HTTPException as exc:
                logger.warning(
                    "Will retry finalizing riddle %d after Discord error %s",
                    riddle.id,
                    type(exc).__name__,
                )
            except Exception as exc:
                logger.exception(
                    "Will retry finalizing riddle %d after %s",
                    riddle.id,
                    type(exc).__name__,
                )

    async def _resolve_channel(
        self,
        channel_id: int,
    ) -> discord.abc.GuildChannel | discord.Thread | None:
        cached = self.bot.get_channel(channel_id)
        if cached is not None:
            return cached
        try:
            fetched = await self.bot.fetch_channel(channel_id)
        except discord.NotFound:
            return None
        return fetched

    async def _publish_finalization(self, riddle_id: int) -> None:
        async with self._riddle_operation_lock:
            riddle = await self._db(self.database.get_riddle, riddle_id)
            if riddle is None or status_value(riddle.status) not in {
                "solved",
                "expired",
            }:
                return

            winner_ids = await self._db(self.database.list_winner_ids, riddle.id)
            mvp_user_ids = (
                await self._db(self.database.list_mvp_user_ids, riddle.id)
                if riddle.manual_judging
                else ()
            )
            settings = await self._db(
                self.database.get_guild_settings,
                riddle.guild_id,
            )
            guild = self.bot.get_guild(riddle.guild_id)
            content, allowed_mentions = self._finished_content(
                riddle,
                winner_ids,
                settings.mention_winners,
                guild,
                mvp_user_ids=mvp_user_ids,
            )

            thread: discord.Thread | None = None
            if riddle.thread_id is not None:
                resolved = await self._resolve_channel(riddle.thread_id)
                if isinstance(resolved, discord.Thread):
                    thread = resolved

            published = False
            starter_missing = riddle.thread_id is None
            starter_forbidden = False
            if riddle.thread_id is not None:
                channel = await self._resolve_channel(riddle.channel_id)
                if isinstance(channel, discord.TextChannel):
                    try:
                        # 公開threadと起点messageは同じsnowflake IDを共有する。
                        message = await channel.fetch_message(riddle.thread_id)
                        await message.edit(
                            content=content,
                            view=ClosedAnswerView(),
                            allowed_mentions=allowed_mentions,
                        )
                        published = True
                    except discord.NotFound:
                        starter_missing = True
                        logger.info(
                            "Starter message for riddle %d no longer exists",
                            riddle.id,
                        )
                    except discord.Forbidden:
                        starter_forbidden = True
                        logger.warning(
                            "Cannot edit starter message for riddle %d",
                            riddle.id,
                        )
                else:
                    starter_missing = True

            if not published and thread is not None:
                try:
                    if thread.archived:
                        await thread.edit(archived=False)
                    await thread.send(
                        content,
                        allowed_mentions=allowed_mentions,
                    )
                    published = True
                except discord.Forbidden:
                    logger.warning(
                        "Cannot publish fallback result for riddle %d",
                        riddle.id,
                    )

            if thread is not None and riddle.message_id is not None:
                try:
                    if thread.archived:
                        await thread.edit(archived=False)
                    prompt = await thread.fetch_message(riddle.message_id)
                    await prompt.edit(
                        content="回答受付は終了しました。開始メッセージで結果を確認してください。",
                        view=ClosedAnswerView(),
                        allowed_mentions=NO_MENTIONS,
                    )
                except discord.NotFound:
                    logger.info(
                        "Answer prompt for riddle %d no longer exists",
                        riddle.id,
                    )
                except discord.Forbidden:
                    logger.warning(
                        "Cannot disable answer prompt for riddle %d",
                        riddle.id,
                    )

            # 対象が削除済みならローカルデータだけ破棄する。対象は存在するが
            # 権限不足なら、管理者が権限を直した後に再試行できるよう保持する。
            if not published and (starter_forbidden or thread is not None):
                raise RuntimeError("結果を表示するDiscord権限が不足しています")
            if not published and not starter_missing:
                raise RuntimeError("結果を表示できるDiscordメッセージがありません")

            if thread is not None:
                try:
                    await thread.edit(archived=True, locked=True)
                except (discord.Forbidden, discord.NotFound):
                    logger.warning(
                        "Could not archive and lock thread for riddle %d",
                        riddle.id,
                    )

            await self._db(self.database.delete_finished_riddle, riddle.id)

    def _finished_content(
        self,
        riddle: Riddle,
        winner_ids: list[int],
        mention_winners: bool,
        guild: discord.Guild | None,
        *,
        mvp_user_ids: tuple[int, ...] | list[int] = (),
    ) -> tuple[str, discord.AllowedMentions]:
        def display_users(user_ids: list[int] | tuple[int, ...]) -> str:
            displayed_user_ids = list(user_ids[:20])
            if not displayed_user_ids:
                return "なし"
            if mention_winners:
                displayed_names = "、".join(
                    f"<@{user_id}>" for user_id in displayed_user_ids
                )
            else:
                names: list[str] = []
                for user_id in displayed_user_ids:
                    member = guild.get_member(user_id) if guild is not None else None
                    names.append(
                        member.display_name
                        if member is not None
                        else f"ユーザー {user_id}"
                    )
                displayed_names = "、".join(names)
            if len(user_ids) > len(displayed_user_ids):
                displayed_names += f"、ほか{len(user_ids) - len(displayed_user_ids)}名"
            return displayed_names

        displayed_ids = winner_ids[:20]
        winners = display_users(winner_ids)
        mvp_users = display_users(mvp_user_ids)

        reason = (
            "最初の正解者が決まりました"
            if status_value(riddle.status) == "solved"
            else "期限になりました"
        )
        shown_question = (
            riddle.question
            if len(riddle.question) <= 700
            else riddle.question[:697] + "…"
        )
        if riddle.manual_judging:
            content = (
                f"**大喜利 #{riddle.id} — 終了**\n"
                f"{shown_question}\n\n"
                f"{reason}。\n"
                f"**MVP：** {mvp_users}\n"
                f"**採用された回答者：** {winners}\n"
                "採用・MVPに選ばれた回答は、"
                "スレッド内の🏆リアクションで確認できます。"
            )
        else:
            content = (
                f"**なぞなぞ #{riddle.id} — 終了**\n"
                f"{shown_question}\n\n"
                f"{reason}。\n"
                f"**正解：** {riddle.answer_display}\n"
                f"**正解者：** {winners}"
            )
        if len(content) > 2_000:
            content = content[:1_950] + "\n…（表示を省略しました）"

        mention_ids = list(dict.fromkeys([*displayed_ids, *list(mvp_user_ids[:20])]))
        allowed_users = (
            [discord.Object(id=user_id) for user_id in mention_ids]
            if mention_winners
            else False
        )
        allowed_mentions = discord.AllowedMentions(
            everyone=False,
            roles=False,
            users=allowed_users,
            replied_user=False,
        )
        return content, allowed_mentions

    async def handle_answer(
        self,
        interaction: discord.Interaction,
        *,
        source_message_id: int,
        submitted_answer: str,
    ) -> None:
        if not self.is_allowed_guild(interaction):
            submitted_answer = ""
            await interaction.edit_original_response(
                content="エラー！：このBotは別のDiscordサーバー向けに設定されています。"
            )
            return

        user_id = interaction.user.id
        cooldown_key = (source_message_id, user_id)
        now_monotonic = time.monotonic()
        last_submission = self._answer_cooldowns.get(cooldown_key)
        if (
            last_submission is not None
            and now_monotonic - last_submission < ANSWER_COOLDOWN_SECONDS
        ):
            await interaction.edit_original_response(
                content="エラー！：回答の連続送信は少し待ってから試してください。"
            )
            return
        self._answer_cooldowns[cooldown_key] = now_monotonic
        if len(self._answer_cooldowns) > 5_000:
            cutoff = now_monotonic - 300
            self._answer_cooldowns = {
                key: submitted_at
                for key, submitted_at in self._answer_cooldowns.items()
                if submitted_at >= cutoff
            }

        publication_failed = False
        try:
            async with self._riddle_operation_lock:
                result = await self._db(
                    self.database.submit_answer_by_message,
                    source_message_id,
                    user_id,
                    submitted_answer,
                    now=datetime.now(timezone.utc),
                )
                if result.status is SubmissionStatus.NOT_FOUND:
                    # 公開スレッドの起点メッセージIDはthread IDと同じ。
                    # 親チャンネル側のボタンから回答した場合はこちらで特定する。
                    result = await self._db(
                        self.database.submit_answer_by_thread,
                        source_message_id,
                        user_id,
                        submitted_answer,
                        now=datetime.now(timezone.utc),
                    )
                riddle = result.riddle
                if (
                    riddle is not None
                    and riddle.mode == "riddle"
                    and riddle.public_answers
                    and result.attempt_recorded
                ):
                    try:
                        published = await self._publish_public_answer(
                            riddle,
                            interaction.user,
                            submitted_answer,
                            result.attempt_count,
                        )
                        publication_failed = not published
                    except Exception as error:  # noqa: BLE001
                        publication_failed = True
                        logger.warning(
                            "Could not publish answer for riddle %d user=%d (%s)",
                            riddle.id,
                            user_id,
                            type(error).__name__,
                        )
        finally:
            # 回答本文をインスタンス属性やログへ残さない。
            submitted_answer = ""

        result_status = status_value(result.status)
        riddle = result.riddle

        if result_status in {"not_found", "closed"} or riddle is None:
            await interaction.edit_original_response(
                content="エラー！：この問題の回答受付は終了しています。"
            )
            return

        if result_status == "expired":
            await interaction.edit_original_response(
                content="エラー！：回答期限を過ぎています。"
            )
            await self._db(
                self.database.mark_riddle_expired,
                riddle.id,
                now=datetime.now(timezone.utc),
            )
            await self._publish_finalization(riddle.id)
            return

        if result_status == "limit_reached":
            await interaction.edit_original_response(
                content=(
                    f"エラー！：この問題への回答は1人{riddle.answer_limit}回までです。"
                )
            )
            return

        if riddle.mode == "riddle":
            remaining = (
                ""
                if result.remaining_attempts is None
                else f"\n残り回答回数：{result.remaining_attempts}回"
            )
            publication_notice = (
                "\nエラー！：回答は受け付けましたが、"
                "公開回答をスレッドへ表示できませんでした。"
                if publication_failed
                else ""
            )
            await interaction.edit_original_response(
                content=(
                    "回答を受け付けました。結果は期限に発表します。"
                    f"{remaining}{publication_notice}"
                )
            )
            return

        if result_status == "wrong":
            await interaction.edit_original_response(content="不正解です。")
            return

        if result_status == "correct":
            await interaction.edit_original_response(
                content="正解です！ 最初の正解者として記録しました。"
            )
            await self._publish_finalization(riddle.id)
            return

        await interaction.edit_original_response(
            content="この問題には既に正解者がいます。"
        )

    async def _publish_public_answer(
        self,
        riddle: Riddle,
        user: discord.abc.User,
        answer: str,
        attempt_count: int,
    ) -> bool:
        if riddle.thread_id is None:
            return False
        resolved = await self._resolve_channel(riddle.thread_id)
        if not isinstance(resolved, discord.Thread):
            return False
        if resolved.archived:
            await resolved.edit(archived=False)

        raw_display_name = (
            getattr(user, "display_name", None)
            or getattr(user, "name", None)
            or "ユーザー"
        )
        display_name = discord.utils.escape_markdown(raw_display_name)
        shown_answer = discord.utils.escape_markdown(
            discord.utils.escape_mentions(answer)
        )
        message = await resolved.send(
            f"**{display_name}の回答（{attempt_count}回目）：** {shown_answer}",
            allowed_mentions=NO_MENTIONS,
            suppress_embeds=True,
        )
        try:
            await self._db(
                self.database.register_public_answer_message,
                message_id=message.id,
                riddle_id=riddle.id,
                user_id=user.id,
                attempt_count=attempt_count,
                posted_at=datetime.now(timezone.utc),
            )
        except Exception:
            # 対応情報を永続化できない回答は再起動後に採用できないため、
            # Discord側だけに孤立させない。
            try:
                await message.delete()
            except discord.HTTPException:
                logger.warning(
                    "Could not remove unregistered public answer riddle=%d",
                    riddle.id,
                )
            raise
        seed_emojis = [riddle.answer_accept_emoji]
        if riddle.manual_judging:
            seed_emojis.append(riddle.ogiri_mvp_emoji)
        for configured_emoji in seed_emojis:
            try:
                # 出題者は既存の設定絵文字をクリックするだけで評価できる。
                await message.add_reaction(self._reaction_value(configured_emoji))
            except discord.HTTPException:
                # 絵文字を新しく付ける権限がある場合は手動追加でも評価できる。
                logger.warning(
                    "Could not seed evaluation reaction riddle=%d message=%d",
                    riddle.id,
                    message.id,
                )
        return True

    @app_commands.command(
        name="briddle",
        description="最初の正解者が出た時点で答えを発表するなぞなぞを作成します",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        riddle="表示する問題文",
        answer="正解。別解は | で区切れます（他の利用者には表示されません）",
        term="回答期限。30m、2h、1d形式。省略時は24h",
        media="問題に添付する画像・音声・動画（1ファイルまで）",
    )
    async def briddle(
        self,
        interaction: discord.Interaction,
        riddle: str,
        answer: str,
        term: str | None = None,
        media: discord.Attachment | None = None,
    ) -> None:
        await self._create_riddle(
            interaction,
            mode="briddle",
            question=riddle,
            answer=answer,
            term=term,
            media=media,
            public_answers=False,
            answer_limit=None,
        )

    @app_commands.command(
        name="riddle",
        description="期限まで答えを伏せ、複数人の正解を受け付けるなぞなぞを作成します",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        riddle="表示する問題文",
        answer="正解。別解は | で区切れます（他の利用者には表示されません）",
        term="回答期限。30m、2h、1d形式。省略時は24h",
        public_answers="回答文をスレッドへ公開。省略時は個人設定",
        answer_limit="1人あたりの回答回数。0は無制限、省略時は個人設定",
        media="問題に添付する画像・音声・動画（1ファイルまで）",
    )
    async def riddle(
        self,
        interaction: discord.Interaction,
        riddle: str,
        answer: str,
        term: str | None = None,
        public_answers: bool | None = None,
        answer_limit: app_commands.Range[int, 0, 100] | None = None,
        media: discord.Attachment | None = None,
    ) -> None:
        await self._create_riddle(
            interaction,
            mode="riddle",
            question=riddle,
            answer=answer,
            term=term,
            media=media,
            public_answers=public_answers,
            answer_limit=answer_limit,
        )

    @app_commands.command(
        name="ogiri",
        description="回答を公開し、出題者が設定絵文字で採用する大喜利を作成します",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        theme="表示するお題",
        term="回答期限。30m、2h、1d形式。省略時は24h",
        answer_limit="1人あたりの回答回数。0は無制限、省略時は個人設定",
        media="お題に添付する画像・音声・動画（1ファイルまで）",
    )
    async def ogiri(
        self,
        interaction: discord.Interaction,
        theme: str,
        term: str | None = None,
        answer_limit: app_commands.Range[int, 0, 100] | None = None,
        media: discord.Attachment | None = None,
    ) -> None:
        await self._create_riddle(
            interaction,
            mode="riddle",
            question=theme,
            answer=None,
            term=term,
            media=media,
            public_answers=True,
            answer_limit=answer_limit,
            manual_judging=True,
        )

    async def _create_riddle(
        self,
        interaction: discord.Interaction,
        *,
        mode: str,
        question: str,
        answer: str | None,
        term: str | None,
        media: discord.Attachment | None,
        public_answers: bool | None = None,
        answer_limit: int | None = None,
        manual_judging: bool = False,
    ) -> None:
        guild = interaction.guild
        channel = interaction.channel
        if guild is None or not isinstance(channel, discord.TextChannel):
            await send_ephemeral(
                interaction,
                "エラー！：問題はサーバー内の通常テキストチャンネルで作成してください。",
            )
            return

        question = question.strip()
        answer = "" if answer is None else answer.strip()
        if not question:
            await send_ephemeral(interaction, "エラー！：問題文を入力してください。")
            return
        if len(question) > QUESTION_MAX_LENGTH:
            await send_ephemeral(
                interaction,
                f"エラー！：問題文は{QUESTION_MAX_LENGTH}文字以内にしてください。",
            )
            return
        if not manual_judging and not answer:
            await send_ephemeral(interaction, "エラー！：正解を入力してください。")
            return
        if not manual_judging and len(answer) > ANSWER_MAX_LENGTH:
            await send_ephemeral(
                interaction,
                f"エラー！：正解は{ANSWER_MAX_LENGTH}文字以内にしてください。",
            )
            return

        try:
            if not manual_judging:
                prepare_answers(answer)
            deadline = deadline_from_term(
                term,
                now=datetime.now(timezone.utc),
            )
            if media is not None:
                self._validate_media_attachment(
                    media,
                    server_size_limit=guild.filesize_limit,
                )
        except ValueError as exc:
            await send_ephemeral(interaction, f"エラー！：{exc}")
            return

        settings, preferences = await asyncio.gather(
            self._db(self.database.get_guild_settings, guild.id),
            self._db(
                self.database.get_user_preferences,
                guild.id,
                interaction.user.id,
            ),
        )
        if mode == "riddle":
            effective_public_answers = (
                preferences.public_answers if public_answers is None else public_answers
            )
            effective_answer_limit = (
                preferences.answer_limit
                if answer_limit is None
                else (None if answer_limit == 0 else answer_limit)
            )
        else:
            effective_public_answers = False
            effective_answer_limit = None
        if manual_judging:
            effective_public_answers = True

        if (
            settings.allowed_channel_id is not None
            and channel.id != settings.allowed_channel_id
        ):
            await send_ephemeral(
                interaction,
                f"エラー！：問題を作成できるのは <#{settings.allowed_channel_id}> です。",
            )
            return

        missing_permissions = self._missing_bot_permissions(
            guild,
            channel,
            needs_media=media is not None,
            needs_answer_reactions=True,
        )
        if missing_permissions:
            await send_ephemeral(
                interaction,
                "エラー！：Botの権限が不足しています（"
                + "、".join(missing_permissions)
                + "）。管理者に問い合わせてください。",
            )
            return

        await interaction.response.defer(thinking=True)
        created: Riddle | None = None
        thread: discord.Thread | None = None
        try:
            created = await self._db(
                self.database.create_riddle,
                guild_id=guild.id,
                channel_id=channel.id,
                creator_id=interaction.user.id,
                mode=mode,
                question=question,
                answer_display=answer,
                deadline_at=deadline,
                public_answers=effective_public_answers,
                answer_limit=effective_answer_limit,
                manual_judging=manual_judging,
                answer_accept_emoji=settings.answer_accept_emoji,
                ogiri_mvp_emoji=settings.ogiri_mvp_emoji,
                good_question_emoji=settings.good_question_emoji,
            )

            initial_content = self._open_content(
                created,
                creator_name=interaction.user.display_name,
                thread=None,
            )
            attachments: list[discord.File] = []
            if media is not None:
                uploaded_file = await media.to_file(spoiler=media.is_spoiler())
                if self._file_looks_like_svg(uploaded_file):
                    uploaded_file.close()
                    raise ValueError("SVGは添付できません。")
                attachments.append(uploaded_file)
            message = await interaction.edit_original_response(
                content=initial_content,
                attachments=attachments,
                allowed_mentions=NO_MENTIONS,
            )
            thread_label = "大喜利" if created.manual_judging else "なぞなぞ"
            thread = await message.create_thread(
                name=f"{thread_label}-{created.id}",
                auto_archive_duration=self._auto_archive_duration(created.deadline_at),
                reason=f"{thread_label} #{created.id} の回答スレッド",
            )
            prompt_message = await thread.send(
                "回答フォームを準備しています…",
                allowed_mentions=NO_MENTIONS,
            )
            created = await self._db(
                self.database.bind_discord_ids,
                created.id,
                thread_id=thread.id,
                message_id=prompt_message.id,
                channel_id=channel.id,
            )
            await message.edit(
                content=self._open_content(
                    created,
                    creator_name=interaction.user.display_name,
                    thread=thread,
                ),
                view=AnswerView(self),
                allowed_mentions=NO_MENTIONS,
            )
            await prompt_message.edit(
                content=self._answer_prompt_content(created),
                view=AnswerView(self),
                allowed_mentions=NO_MENTIONS,
            )
            await message.add_reaction(
                self._reaction_value(created.good_question_emoji)
            )
            logger.info(
                "Created riddle id=%d guild=%d mode=%s manual_judging=%s",
                created.id,
                guild.id,
                mode,
                manual_judging,
            )
        except ActiveRiddleLimitError:
            await interaction.edit_original_response(
                content=(
                    f"エラー！：未終了の問題は1人{settings.max_active}件までです。"
                    "既存の問題が終了してから作成してください。"
                ),
                view=None,
                attachments=[],
                allowed_mentions=NO_MENTIONS,
            )
        except (discord.Forbidden, discord.HTTPException, ValueError):
            logger.exception(
                "Failed to create riddle Discord resources (guild=%d)",
                guild.id,
            )
            if created is not None:
                await self._db(self.database.delete_riddle, created.id)
            if thread is not None:
                try:
                    await thread.edit(archived=True, locked=True)
                except discord.HTTPException:
                    pass
            await interaction.edit_original_response(
                content=(
                    "エラー！：問題またはスレッドを作成できませんでした。"
                    "Botの権限を確認し、解決しない場合は管理者に問い合わせてください。"
                ),
                view=None,
                attachments=[],
                allowed_mentions=NO_MENTIONS,
            )
        finally:
            answer = ""

    def _missing_bot_permissions(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        *,
        needs_media: bool = False,
        needs_answer_reactions: bool = False,
    ) -> list[str]:
        member = guild.me
        if member is None:
            return ["Botメンバー情報の取得"]
        permissions = channel.permissions_for(member)
        required = [
            ("view_channel", "チャンネルの閲覧"),
            ("send_messages", "メッセージの送信"),
            ("read_message_history", "メッセージ履歴の閲覧"),
            ("create_public_threads", "公開スレッドの作成"),
            ("send_messages_in_threads", "スレッド内の送信"),
            ("manage_threads", "スレッドの管理"),
        ]
        if needs_media:
            required.append(("attach_files", "ファイルの添付"))
        if needs_answer_reactions:
            required.append(("add_reactions", "リアクションの追加"))
        return [
            label
            for attribute, label in required
            if not getattr(permissions, attribute, False)
        ]

    def _validate_media_attachment(
        self,
        media: discord.Attachment,
        *,
        server_size_limit: int,
    ) -> None:
        content_type = (media.content_type or "").partition(";")[0].strip().casefold()
        filename = str(getattr(media, "filename", "")).casefold()
        if (
            not content_type.startswith(MEDIA_CONTENT_TYPE_PREFIXES)
            or content_type in BLOCKED_MEDIA_CONTENT_TYPES
            or filename.endswith((".svg", ".svgz"))
        ):
            raise ValueError(
                "添付できるのは画像・音声・動画です（SVGは添付できません）。"
            )

        effective_limit = min(MEDIA_MAX_SIZE_BYTES, server_size_limit)
        if media.size <= 0:
            raise ValueError("空のメディアファイルは添付できません。")
        if media.size > effective_limit:
            limit_mib = effective_limit / (1024 * 1024)
            raise ValueError(f"メディアは{limit_mib:g} MiB以下にしてください。")

    def _file_looks_like_svg(self, uploaded_file: discord.File) -> bool:
        """MIMEや拡張子を偽装したSVGを、アップロード前に検出する。"""

        stream = uploaded_file.fp
        try:
            position = stream.tell()
            head = stream.read(65_536)
            stream.seek(position)
        except (AttributeError, OSError):
            return False
        if isinstance(head, str):
            head = head.encode("utf-8", errors="ignore")
        normalized = head.lstrip(b"\xef\xbb\xbf\x00\t\r\n ").lower()
        return b"<svg" in normalized

    def _auto_archive_duration(self, deadline: datetime) -> int:
        seconds = max(
            0.0,
            (deadline - datetime.now(timezone.utc)).total_seconds(),
        )
        if seconds <= 3_600:
            return 60
        if seconds <= 86_400:
            return 1_440
        if seconds <= 259_200:
            return 4_320
        return 10_080

    def _open_content(
        self,
        riddle: Riddle,
        *,
        creator_name: str,
        thread: discord.Thread | None,
    ) -> str:
        if riddle.manual_judging:
            heading = "大喜利"
            mode_description = (
                "回答を公開し、出題者が"
                f"{riddle.answer_accept_emoji}で採用・"
                f"{riddle.ogiri_mvp_emoji}でMVP選出"
            )
        else:
            heading = "なぞなぞ"
            mode_description = (
                "最初の正解で発表"
                if riddle.mode == "briddle"
                else "期限まで正誤を非公開"
            )
        deadline_timestamp = int(riddle.deadline_at.timestamp())
        thread_line = f"\n回答スレッド：{thread.mention}" if thread is not None else ""
        settings_lines = ""
        if riddle.mode == "riddle":
            visibility = (
                "公開（正誤は期限まで非公開）" if riddle.public_answers else "非公開"
            )
            limit = (
                "無制限"
                if riddle.answer_limit is None
                else f"1人{riddle.answer_limit}回"
            )
            settings_lines = f"\n回答文：{visibility}\n回答回数：{limit}"
        return (
            f"**{heading} #{riddle.id}**\n"
            f"{riddle.question}\n\n"
            f"作成者：{creator_name}\n"
            f"方式：{mode_description}\n"
            f"期限：<t:{deadline_timestamp}:F>（<t:{deadline_timestamp}:R>）"
            f"{settings_lines}"
            f"{thread_line}\n"
            "下の「回答する」ボタンから回答してください。\n"
            f"良い問題だと思ったら {riddle.good_question_emoji} で評価できます。"
        )

    def _answer_prompt_content(self, riddle: Riddle) -> str:
        if riddle.manual_judging:
            visibility = (
                "入力した回答文は回答者名とともにこのスレッドへ公開されます。"
                f"出題者は回答の{riddle.answer_accept_emoji}で採用、"
                f"{riddle.ogiri_mvp_emoji}でMVPにできます。"
                "Botの🏆が採用またはMVP記録済みの印です。"
            )
        elif riddle.public_answers:
            visibility = (
                "入力した回答文は回答者名とともにこのスレッドへ公開されます。"
                "自動判定の正誤は期限まで表示されません。"
                "出題者は想定外の回答でも"
                f"{riddle.answer_accept_emoji}を押して正解として採用できます。"
                "Botの🏆が採用済みの印です。"
            )
        else:
            visibility = "入力内容は他の参加者には表示されません。"
        limit = (
            ""
            if riddle.answer_limit is None
            else f" 1人{riddle.answer_limit}回まで回答できます。"
        )
        return f"下の「回答する」ボタンから答えを入力してください。{visibility}{limit}"

    @app_commands.command(
        name="hint",
        description="自分の受付中問題へスポイラー形式のヒントを追加します",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        riddle_id="ヒントを追加する問題ID",
        text="ヒント本文（本文またはメディアのどちらかは必須）",
        media="スポイラー表示する画像・音声・動画（1ファイルまで）",
    )
    async def hint(
        self,
        interaction: discord.Interaction,
        riddle_id: int,
        text: str | None = None,
        media: discord.Attachment | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None or riddle_id <= 0:
            await send_ephemeral(interaction, "エラー！：問題IDが不正です。")
            return

        hint_text = text.strip() if text is not None else ""
        if not hint_text and media is None:
            await send_ephemeral(
                interaction,
                "エラー！：ヒント本文またはメディアを指定してください。",
            )
            return
        if len(hint_text) > HINT_MAX_LENGTH:
            await send_ephemeral(
                interaction,
                f"エラー！：ヒント本文は{HINT_MAX_LENGTH}文字以内にしてください。",
            )
            return
        try:
            if media is not None:
                self._validate_media_attachment(
                    media,
                    server_size_limit=guild.filesize_limit,
                )
        except ValueError as exc:
            await send_ephemeral(interaction, f"エラー！：{exc}")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        riddle = await self._db(self.database.get_riddle, riddle_id)
        if riddle is None or riddle.guild_id != guild.id:
            await interaction.edit_original_response(
                content="エラー！：指定された問題が見つかりません。"
            )
            return
        if riddle.creator_id != interaction.user.id and not is_administrator(
            interaction
        ):
            await interaction.edit_original_response(
                content="エラー！：ヒントを追加できるのは作成者または管理者です。"
            )
            return
        if riddle.status != "active":
            await interaction.edit_original_response(
                content="エラー！：この問題の回答受付は終了しています。"
            )
            return

        uploaded_file: discord.File | None = None
        try:
            if media is not None:
                uploaded_file = await media.to_file(spoiler=True)
                if self._file_looks_like_svg(uploaded_file):
                    raise ValueError("SVGは添付できません。")
                original_filename = str(getattr(media, "filename", ""))
                suffix_match = re.search(r"(\.[A-Za-z0-9]{1,8})$", original_filename)
                safe_suffix = suffix_match.group(1).lower() if suffix_match else ""
                uploaded_file.filename = f"SPOILER_hint-{riddle.id}{safe_suffix}"
                uploaded_file.description = None

            should_finalize = False
            async with self._riddle_operation_lock:
                current = await self._db(self.database.get_riddle, riddle.id)
                if current is None or current.status != "active":
                    await interaction.edit_original_response(
                        content="エラー！：この問題の回答受付は終了しています。"
                    )
                    return
                if current.deadline_at <= datetime.now(timezone.utc):
                    await self._db(
                        self.database.mark_riddle_expired,
                        current.id,
                        now=datetime.now(timezone.utc),
                    )
                    should_finalize = True
                else:
                    resolved = (
                        await self._resolve_channel(current.thread_id)
                        if current.thread_id is not None
                        else None
                    )
                    if not isinstance(resolved, discord.Thread):
                        await interaction.edit_original_response(
                            content="エラー！：回答スレッドが見つかりません。"
                        )
                        return
                    if resolved.archived:
                        await resolved.edit(archived=False)

                    content = f"**ヒント（問題 #{current.id}）**"
                    if hint_text:
                        # 外側の ``||`` を閉じたりメンションを発火させる文字を、
                        # 1文字ずつ見た目の近い全角文字へ置き換える。Markdownの
                        # バックスラッシュescapeを使わないため、入力が上限の
                        # 1000文字でもDiscordの2000文字制限を超えない。
                        spoiler_safe_hint = hint_text.translate(
                            HINT_CONTROL_CHARACTER_TRANSLATION
                        )
                        content += f"\n||{spoiler_safe_hint}||"
                    if uploaded_file is not None and not hint_text:
                        content += "\n下のスポイラー付きメディアを開いてください。"
                    send_options: dict[str, Any] = {
                        "allowed_mentions": NO_MENTIONS,
                        "suppress_embeds": True,
                    }
                    if uploaded_file is not None:
                        send_options["file"] = uploaded_file
                    await resolved.send(content, **send_options)

            if should_finalize:
                await interaction.edit_original_response(
                    content="エラー！：回答期限を過ぎているためヒントを追加できません。"
                )
                await self._publish_finalization(riddle.id)
                return

            await interaction.edit_original_response(
                content=f"問題 #{riddle.id} にヒントを追加しました。"
            )
            logger.info(
                "Added hint riddle=%d guild=%d by user=%d media=%s",
                riddle.id,
                guild.id,
                interaction.user.id,
                media is not None,
            )
        except ValueError as exc:
            await interaction.edit_original_response(content=f"エラー！：{exc}")
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.warning(
                "Could not publish hint riddle=%d guild=%d",
                riddle.id,
                guild.id,
                exc_info=True,
            )
            await interaction.edit_original_response(
                content=(
                    "エラー！：ヒントを投稿できませんでした。"
                    "Botのスレッド送信・添付権限を確認してください。"
                )
            )
        finally:
            if uploaded_file is not None:
                uploaded_file.close()
            hint_text = ""

    @app_commands.command(
        name="help",
        description="利用できるコマンド一覧を自分だけに表示します",
    )
    @app_commands.guild_only()
    async def help_command(self, interaction: discord.Interaction) -> None:
        await send_ephemeral(interaction, self._help_content())

    def _help_content(self) -> str:
        heading = "**利用できるコマンド**\n"
        footer = "\n各コマンドを選ぶと、引数の説明もDiscord上に表示されます。"
        lines: list[str] = []
        for command in sorted(self.get_app_commands(), key=lambda item: item.name):
            line = f"`/{command.name}` — {command.description}"
            candidate = heading + "\n".join([*lines, line]) + footer
            if len(candidate) > 2_000:
                omitted = "…（一部のコマンドを省略しました）"
                truncated = heading + "\n".join([*lines, omitted]) + footer
                if len(truncated) <= 2_000:
                    lines.append(omitted)
                break
            lines.append(line)
        return heading + "\n".join(lines) + footer

    @app_commands.command(
        name="delete",
        description="自分の問題を取り消します。purgeで投稿とスレッドも完全削除できます",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        riddle_id="取り消す問題ID",
        purge="trueなら問題投稿と回答スレッドも削除（元に戻せません）",
    )
    async def delete(
        self,
        interaction: discord.Interaction,
        riddle_id: int,
        purge: bool = False,
    ) -> None:
        guild = interaction.guild
        if guild is None or riddle_id <= 0:
            await send_ephemeral(interaction, "エラー！：問題IDが不正です。")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        riddle = await self._db(self.database.get_riddle, riddle_id)
        if riddle is None or riddle.guild_id != guild.id:
            await interaction.edit_original_response(
                content="エラー！：指定された問題が見つかりません。"
            )
            return
        if riddle.status != "active":
            await interaction.edit_original_response(
                content="エラー！：この問題は既に終了しています。"
            )
            return
        if riddle.creator_id != interaction.user.id and not is_administrator(
            interaction
        ):
            await interaction.edit_original_response(
                content="エラー！：この問題を削除できるのは作成者または管理者です。"
            )
            return

        expired_riddle_id: int | None = None
        deleted: Riddle | None = None
        try:
            async with self._riddle_operation_lock:
                current = await self._db(self.database.get_riddle, riddle.id)
                operation_now = datetime.now(timezone.utc)
                if (
                    current is not None
                    and current.guild_id == guild.id
                    and current.status == "active"
                    and current.deadline_at <= operation_now
                ):
                    expired = await self._db(
                        self.database.mark_riddle_expired,
                        current.id,
                        now=operation_now,
                    )
                    if expired is not None:
                        expired_riddle_id = expired.id
                elif (
                    current is not None
                    and current.guild_id == guild.id
                    and current.status == "active"
                ):
                    if purge:
                        await self._purge_discord_problem(
                            current,
                            actor_id=interaction.user.id,
                        )
                    deleted = await self._db(
                        self.database.delete_active_riddle,
                        current.id,
                        now=operation_now,
                    )
                    if deleted is not None and not purge:
                        await self._close_discord_problem(
                            deleted,
                            heading="取消",
                            detail=(
                                f"{interaction.user.display_name}さんが"
                                "問題を取り消しました。"
                            ),
                        )
        except (discord.Forbidden, discord.HTTPException):
            logger.warning(
                "Could not purge Discord resources riddle=%d guild=%d",
                riddle.id,
                guild.id,
                exc_info=True,
            )
            await interaction.edit_original_response(
                content=(
                    "エラー！：問題投稿またはスレッドを完全削除できませんでした。"
                    "Botの権限を確認してください。DBの問題データは保持しているため、"
                    "権限修正後に同じコマンドを再実行できます。"
                )
            )
            return

        if expired_riddle_id is not None:
            await interaction.edit_original_response(
                content="エラー！：回答期限を過ぎているため削除できません。"
            )
            await self._publish_finalization(expired_riddle_id)
            return

        if deleted is None:
            await interaction.edit_original_response(
                content="エラー！：この問題は既に終了または削除されています。"
            )
            return

        await interaction.edit_original_response(
            content=(
                f"問題 #{riddle.id} とDiscord上の投稿・スレッドを完全削除しました。"
                if purge
                else f"問題 #{riddle.id} を取り消しました。"
            )
        )
        logger.info(
            "Deleted riddle id=%d guild=%d by user=%d",
            riddle.id,
            guild.id,
            interaction.user.id,
        )

    async def _purge_discord_problem(
        self,
        riddle: Riddle,
        *,
        actor_id: int,
    ) -> None:
        """スレッドと起点メッセージを削除する。失敗時は呼び出し側で再試行する。"""

        if riddle.thread_id is not None:
            resolved = await self._resolve_channel(riddle.thread_id)
            if isinstance(resolved, discord.Thread):
                self._purging_thread_ids.add(resolved.id)
                try:
                    await resolved.delete(
                        reason=(
                            f"Riddle #{riddle.id} purged by Discord user {actor_id}"
                        )
                    )
                except discord.NotFound:
                    self._purging_thread_ids.discard(resolved.id)
                except discord.HTTPException:
                    self._purging_thread_ids.discard(resolved.id)
                    raise

        channel = await self._resolve_channel(riddle.channel_id)
        if isinstance(channel, discord.TextChannel) and riddle.thread_id is not None:
            try:
                starter = await channel.fetch_message(riddle.thread_id)
                await starter.delete()
            except discord.NotFound:
                pass

    async def _close_discord_problem(
        self,
        riddle: Riddle,
        *,
        heading: str,
        detail: str,
    ) -> None:
        problem_label = "大喜利" if riddle.manual_judging else "なぞなぞ"
        content = (
            f"**{problem_label} #{riddle.id} — {heading}**\n"
            f"{riddle.question}\n\n{detail}"
        )
        channel = await self._resolve_channel(riddle.channel_id)
        if isinstance(channel, discord.TextChannel) and riddle.thread_id is not None:
            try:
                message = await channel.fetch_message(riddle.thread_id)
                await message.edit(
                    content=content,
                    view=ClosedAnswerView(),
                    allowed_mentions=NO_MENTIONS,
                )
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                logger.warning(
                    "Could not update starter message for closed riddle %d",
                    riddle.id,
                )

        if riddle.thread_id is not None:
            resolved = await self._resolve_channel(riddle.thread_id)
            if isinstance(resolved, discord.Thread):
                try:
                    if resolved.archived:
                        await resolved.edit(archived=False)
                    if riddle.message_id is not None:
                        try:
                            prompt = await resolved.fetch_message(riddle.message_id)
                            await prompt.edit(
                                content="回答受付は終了しました。",
                                view=ClosedAnswerView(),
                                allowed_mentions=NO_MENTIONS,
                            )
                        except discord.NotFound:
                            pass
                    await resolved.edit(archived=True, locked=True)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    logger.warning(
                        "Could not archive thread for closed riddle %d",
                        riddle.id,
                    )

    @app_commands.command(
        name="list",
        description="受付中の問題一覧を自分だけに表示します",
    )
    @app_commands.guild_only()
    async def list_riddles(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await send_ephemeral(
                interaction, "エラー！：サーバー内で実行してください。"
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        creator_id = None if is_administrator(interaction) else interaction.user.id
        riddles = await self._db(
            self.database.list_active_riddles,
            guild.id,
            creator_id=creator_id,
        )
        if not riddles:
            await interaction.edit_original_response(
                content="現在表示できる未終了の問題はありません。"
            )
            return

        displayed = riddles[:25]
        lines = [
            (
                f"`#{item.id}` {self._question_preview(item.question)} "
                f"— <t:{int(item.deadline_at.timestamp())}:R>"
            )
            for item in displayed
        ]
        if len(riddles) > len(displayed):
            lines.append(f"…ほか{len(riddles) - len(displayed)}件")
        prefix = "サーバー内" if creator_id is None else "あなたが作成した"
        await interaction.edit_original_response(
            content=f"**{prefix}未終了問題**\n" + "\n".join(lines)
        )

    def _question_preview(self, question: str) -> str:
        preview = " ".join(question.split())
        return preview if len(preview) <= 60 else preview[:57] + "…"

    @app_commands.command(
        name="pref",
        description="管理者がBotのサーバー設定を変更します",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        prefid="変更する設定",
        prefvalue="設定値",
    )
    @app_commands.choices(
        prefid=[
            app_commands.Choice(
                name="問題作成チャンネル (allowed_channel)",
                value="allowed_channel",
            ),
            app_commands.Choice(
                name="1人あたりの未終了問題数 (max_active)",
                value="max_active",
            ),
            app_commands.Choice(
                name="正解者をメンション (mention_winners)",
                value="mention_winners",
            ),
            app_commands.Choice(
                name="正解・採用リアクション (answer_accept_emoji)",
                value="answer_accept_emoji",
            ),
            app_commands.Choice(
                name="大喜利MVPリアクション (ogiri_mvp_emoji)",
                value="ogiri_mvp_emoji",
            ),
            app_commands.Choice(
                name="良問リアクション (good_question_emoji)",
                value="good_question_emoji",
            ),
        ]
    )
    async def pref(
        self,
        interaction: discord.Interaction,
        prefid: app_commands.Choice[str],
        prefvalue: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await send_ephemeral(
                interaction, "エラー！：サーバー内で実行してください。"
            )
            return
        if not is_administrator(interaction):
            await send_ephemeral(
                interaction,
                "エラー！：このコマンドは管理者だけが実行できます。",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        setting_name = prefid.value
        try:
            if setting_name == "allowed_channel":
                channel_id = self._parse_allowed_channel(
                    guild,
                    interaction.channel,
                    prefvalue,
                )
                updated = await self._db(
                    self.database.update_guild_settings,
                    guild.id,
                    allowed_channel_id=channel_id,
                )
                shown_value = (
                    "制限なし"
                    if updated.allowed_channel_id is None
                    else f"<#{updated.allowed_channel_id}>"
                )
            elif setting_name == "max_active":
                max_active = int(prefvalue.strip())
                if not 1 <= max_active <= 100:
                    raise ValueError("max_active は1〜100で指定してください。")
                updated = await self._db(
                    self.database.update_guild_settings,
                    guild.id,
                    max_active=max_active,
                )
                shown_value = str(updated.max_active)
            elif setting_name == "mention_winners":
                mention_winners = self._parse_boolean(prefvalue)
                updated = await self._db(
                    self.database.update_guild_settings,
                    guild.id,
                    mention_winners=mention_winners,
                )
                shown_value = "true" if updated.mention_winners else "false"
            else:
                emoji = self._parse_reaction_emoji(guild, prefvalue)
                updated = await self._db(
                    self.database.update_guild_settings,
                    guild.id,
                    **{setting_name: emoji},
                )
                shown_value = getattr(updated, setting_name)
        except (TypeError, ValueError) as exc:
            await interaction.edit_original_response(content=f"エラー！：{exc}")
            return

        await interaction.edit_original_response(
            content=f"`{setting_name}` を `{shown_value}` に変更しました。"
        )
        logger.info(
            "Updated guild preference %s guild=%d by user=%d",
            setting_name,
            guild.id,
            interaction.user.id,
        )

    @app_commands.command(
        name="mypref",
        description="自分が作るriddleの既定設定を変更します",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        prefid="変更する個人設定",
        prefvalue="設定値",
    )
    @app_commands.choices(
        prefid=[
            app_commands.Choice(
                name="回答文を公開 (public_answers)",
                value="public_answers",
            ),
            app_commands.Choice(
                name="1人あたりの回答回数 (answer_limit)",
                value="answer_limit",
            ),
        ]
    )
    async def mypref(
        self,
        interaction: discord.Interaction,
        prefid: app_commands.Choice[str],
        prefvalue: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await send_ephemeral(
                interaction, "エラー！：サーバー内で実行してください。"
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        setting_name = prefid.value
        try:
            if setting_name == "public_answers":
                public_answers = self._parse_boolean(prefvalue)
                updated = await self._db(
                    self.database.update_user_preferences,
                    guild.id,
                    interaction.user.id,
                    public_answers=public_answers,
                )
                shown_value = "true" if updated.public_answers else "false"
            else:
                answer_limit = self._parse_answer_limit(prefvalue)
                updated = await self._db(
                    self.database.update_user_preferences,
                    guild.id,
                    interaction.user.id,
                    answer_limit=answer_limit,
                )
                shown_value = (
                    "0（無制限）"
                    if updated.answer_limit is None
                    else str(updated.answer_limit)
                )
        except (TypeError, ValueError) as exc:
            await interaction.edit_original_response(content=f"エラー！：{exc}")
            return

        await interaction.edit_original_response(
            content=(
                f"`{setting_name}` の個人既定値を `{shown_value}` に変更しました。"
                "\n各 `/riddle` の引数で一時的に上書きできます。"
            )
        )
        logger.info(
            "Updated user preference %s guild=%d user=%d",
            setting_name,
            guild.id,
            interaction.user.id,
        )

    def _parse_allowed_channel(
        self,
        guild: discord.Guild,
        current_channel: discord.abc.GuildChannel | discord.Thread | None,
        raw_value: str,
    ) -> int | None:
        value = raw_value.strip().lower()
        if value == "none":
            return None
        if value == "current":
            if not isinstance(current_channel, discord.TextChannel):
                raise ValueError("current は通常テキストチャンネルで実行してください。")
            return current_channel.id

        mention_match = re.fullmatch(r"<#(\d+)>", value)
        numeric_value = mention_match.group(1) if mention_match else value
        try:
            channel_id = int(numeric_value)
        except ValueError as exc:
            raise ValueError(
                "allowed_channel は current、none、チャンネルID、"
                "またはチャンネルメンションで指定してください。"
            ) from exc
        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            # 型の誤用ではなく、利用者が指定したID値が対象外。
            raise ValueError(  # noqa: TRY004
                "指定したテキストチャンネルが見つかりません。"
            )
        return channel.id

    def _parse_boolean(self, value: str) -> bool:
        normalized = value.strip().casefold()
        if normalized in {"true", "1", "yes", "on", "有効"}:
            return True
        if normalized in {"false", "0", "no", "off", "無効"}:
            return False
        raise ValueError("true または false を指定してください。")

    def _parse_answer_limit(self, value: str) -> int | None:
        normalized = value.strip().casefold()
        if normalized in {"0", "none", "unlimited", "無制限"}:
            return None
        try:
            limit = int(normalized)
        except ValueError as exc:
            raise ValueError(
                "answer_limit は0（無制限）または1〜100で指定してください。"
            ) from exc
        if not 1 <= limit <= 100:
            raise ValueError(
                "answer_limit は0（無制限）または1〜100で指定してください。"
            )
        return limit

    def _parse_reaction_emoji(
        self,
        guild: discord.Guild,
        value: str,
    ) -> str:
        normalized = value.strip()
        custom_match = re.fullmatch(
            r"<a?:[A-Za-z0-9_]{2,32}:(\d{15,22})>",
            normalized,
        )
        if custom_match is not None:
            emoji = guild.get_emoji(int(custom_match.group(1)))
            if emoji is None or not emoji.is_usable():
                raise ValueError(
                    "このサーバーでBotが利用できるカスタム絵文字を指定してください。"
                )
            return str(emoji)

        if not normalized or len(normalized) > 32:
            raise ValueError(
                "Unicode絵文字1件、またはカスタム絵文字を指定してください。"
            )
        if any(character.isspace() for character in normalized):
            raise ValueError("絵文字には空白を含められません。")

        keycap_sequence = "\N{COMBINING ENCLOSING KEYCAP}" in normalized
        regional_indicators = [
            character
            for character in normalized
            if 0x1F1E6 <= ord(character) <= 0x1F1FF
        ]
        emoji_bases: list[str] = []
        for character in normalized:
            if character in {
                "\N{VARIATION SELECTOR-16}",
                "\N{ZERO WIDTH JOINER}",
                "\N{COMBINING ENCLOSING KEYCAP}",
            }:
                continue
            if keycap_sequence and character in "0123456789#*":
                continue
            category = unicodedata.category(character)
            if category in {"So", "Sk"}:
                if not (0x1F3FB <= ord(character) <= 0x1F3FF):
                    emoji_bases.append(character)
                continue
            if category in {"Mn", "Me", "Cf"}:
                continue
            raise ValueError(
                "Unicode絵文字1件、またはカスタム絵文字を指定してください。"
            )

        joined_sequence = "\N{ZERO WIDTH JOINER}" in normalized
        valid_base_count = (
            keycap_sequence
            or (len(regional_indicators) == 2 and len(emoji_bases) == 2)
            or (joined_sequence and len(emoji_bases) >= 2)
            or len(emoji_bases) == 1
        )
        if not valid_base_count:
            raise ValueError(
                "Unicode絵文字1件、またはカスタム絵文字を指定してください。"
            )
        return normalized

    @app_commands.command(
        name="status",
        description="Botとデータベースの稼働状況を確認します",
    )
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await send_ephemeral(
                interaction, "エラー！：サーバー内で実行してください。"
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        active_count, settings = await asyncio.gather(
            self._db(
                self.database.count_active_riddles,
                guild.id,
            ),
            self._db(
                self.database.get_guild_settings,
                guild.id,
            ),
        )
        latency_ms = round(self.bot.latency * 1_000)
        state = "稼働中" if self.bot.is_ready() else "接続準備中"
        await interaction.edit_original_response(
            content=(
                f"**Botステータス：{state}**\n"
                f"Discord応答遅延：{latency_ms} ms\n"
                f"受付中の問題：{active_count}件\n"
                "データベース：接続正常\n"
                f"正解・採用：{settings.answer_accept_emoji} / "
                f"大喜利MVP：{settings.ogiri_mvp_emoji} / "
                f"良問：{settings.good_question_emoji}"
            )
        )

    @app_commands.command(
        name="mydata",
        description="Botが保存している自分のデータ件数を確認します",
    )
    @app_commands.guild_only()
    async def mydata(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await send_ephemeral(
                interaction, "エラー！：サーバー内で実行してください。"
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        summary = await self._db(
            self.database.get_user_data_summary,
            guild.id,
            interaction.user.id,
        )
        await interaction.edit_original_response(
            content=(
                "**保存中のあなたのデータ**\n"
                f"作成した問題：{summary.created_riddles}件"
                f"（受付中 {summary.active_created_riddles}件）\n"
                f"正解者としての記録：{summary.correct_riddles}件\n"
                f"最初の正解者としての記録：{summary.first_wins}件\n"
                f"受付中問題への回答回数：{summary.answer_attempts}回\n"
                f"リアクション判定用の公開回答対応："
                f"{summary.public_answer_messages}件\n"
                f"個人設定：{'保存あり' if summary.has_preferences else '初期値'}\n\n"
                "問題終了時に問題・回答記録は削除されます。"
            )
        )

    @app_commands.command(
        name="deletemydata",
        description="Botが保存している自分のデータを削除します",
    )
    @app_commands.guild_only()
    @app_commands.describe(
        confirm="作成中の問題も取り消して削除することを確認します",
    )
    async def deletemydata(
        self,
        interaction: discord.Interaction,
        confirm: bool,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await send_ephemeral(
                interaction, "エラー！：サーバー内で実行してください。"
            )
            return
        if not confirm:
            await send_ephemeral(
                interaction,
                "削除は行いませんでした。実行する場合は confirm を true にしてください。",
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with self._riddle_operation_lock:
            authored = await self._db(
                self.database.list_riddles_by_creator,
                guild.id,
                interaction.user.id,
            )
            deletion = await self._db(
                self.database.delete_user_data,
                guild.id,
                interaction.user.id,
            )
            for riddle in authored:
                await self._close_discord_problem(
                    riddle,
                    heading="取消",
                    detail="作成者のデータ削除に伴い、この問題を取り消しました。",
                )

        await interaction.edit_original_response(
            content=(
                "あなたの保存データを削除しました。\n"
                f"削除した問題：{deletion.deleted_riddles}件\n"
                f"削除した正解者記録：{deletion.deleted_winner_entries}件\n"
                f"削除した回答回数：{deletion.deleted_answer_attempts}回\n"
                f"削除した公開回答対応："
                f"{deletion.deleted_public_answer_messages}件\n"
                f"削除した個人設定："
                f"{'あり' if deletion.deleted_preferences else 'なし'}\n"
                "Discord上であなた自身が投稿したメッセージと、"
                "公開回答としてBotが投稿済みのメッセージは削除されません。"
            )
        )
        logger.info(
            "Deleted user data guild=%d user=%d riddles=%d winner_entries=%d",
            guild.id,
            interaction.user.id,
            deletion.deleted_riddles,
            deletion.deleted_winner_entries,
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(
        self,
        payload: discord.RawReactionActionEvent,
    ) -> None:
        """出題者の設定絵文字を、公開回答の採用・MVPとして記録する。"""

        if payload.guild_id is None or not self._is_allowed_guild_id(payload.guild_id):
            return
        bot_user = getattr(self.bot, "user", None)
        if bot_user is not None and payload.user_id == bot_user.id:
            return

        received_at = datetime.now(timezone.utc)
        async with self._riddle_operation_lock:
            answer_message = await self._db(
                self.database.get_public_answer_message,
                payload.message_id,
            )
            if answer_message is None:
                return
            riddle = await self._db(
                self.database.get_riddle,
                answer_message.riddle_id,
            )
            if riddle is None or riddle.guild_id != payload.guild_id:
                return

            if self._emoji_matches(payload.emoji, riddle.answer_accept_emoji):
                evaluation = "accepted"
                result = await self._db(
                    self.database.accept_public_answer_message,
                    payload.message_id,
                    payload.user_id,
                    now=received_at,
                )
                new_status = ManualAcceptanceStatus.ACCEPTED
                acknowledgement_statuses = {
                    ManualAcceptanceStatus.ACCEPTED,
                    ManualAcceptanceStatus.ALREADY_ACCEPTED,
                }
            elif riddle.manual_judging and self._emoji_matches(
                payload.emoji, riddle.ogiri_mvp_emoji
            ):
                evaluation = "mvp"
                result = await self._db(
                    self.database.mark_public_answer_mvp,
                    payload.message_id,
                    payload.user_id,
                    now=received_at,
                )
                new_status = ManualAcceptanceStatus.MVP_MARKED
                acknowledgement_statuses = {
                    ManualAcceptanceStatus.MVP_MARKED,
                    ManualAcceptanceStatus.ALREADY_MVP,
                }
            else:
                return

        if result.status is ManualAcceptanceStatus.EXPIRED:
            if result.riddle is not None:
                await self._publish_finalization(result.riddle.id)
            return
        if result.status not in acknowledgement_statuses:
            return

        resolved = await self._resolve_channel(payload.channel_id)
        if not isinstance(resolved, discord.Thread):
            return
        try:
            message = await resolved.fetch_message(payload.message_id)
            await message.add_reaction(ACCEPTED_ANSWER_EMOJI)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            logger.warning(
                "Could not acknowledge accepted answer message=%d",
                payload.message_id,
            )
            return

        if (
            result.status is new_status
            and result.riddle is not None
            and result.answer_message is not None
        ):
            logger.info(
                "Evaluated public answer riddle=%d answer_user=%d "
                "evaluator=%d evaluation=%s",
                result.riddle.id,
                result.answer_message.user_id,
                payload.user_id,
                evaluation,
            )

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        if thread.id in self._purging_thread_ids:
            self._purging_thread_ids.discard(thread.id)
            return
        async with self._riddle_operation_lock:
            riddle = await self._db(
                self.database.get_riddle_by_thread,
                thread.id,
                active_only=False,
            )
            if riddle is not None:
                await self._db(self.database.delete_riddle, riddle.id)
                logger.info(
                    "Deleted local riddle %d because its Discord thread was deleted",
                    riddle.id,
                )

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CheckFailure) and not self.is_allowed_guild(
            interaction
        ):
            # interaction_check が理由を既に通知しているため、二重応答しない。
            return

        command_name = (
            interaction.command.qualified_name
            if interaction.command is not None
            else "unknown"
        )
        logger.exception(
            "Unhandled application command error command=%s type=%s",
            command_name,
            type(error).__name__,
        )
        try:
            await send_ephemeral(
                interaction,
                "エラー！：処理中に問題が発生しました。"
                "解決しない場合は管理者に問い合わせてください。",
            )
        except discord.HTTPException:
            logger.warning(
                "Failed to report application command error command=%s",
                command_name,
            )
