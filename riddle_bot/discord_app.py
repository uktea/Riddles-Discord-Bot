from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

from riddle_bot.config import BotConfig
from riddle_bot.database import RiddleDatabase
from riddle_bot.discord_commands import AnswerView, RiddleCog
from riddle_bot.instance_lock import InstanceAlreadyRunningError, SingleInstanceLock

logger = logging.getLogger(__name__)


class RiddleBot(commands.Bot):
    def __init__(self, config: BotConfig, database: RiddleDatabase) -> None:
        intents = discord.Intents.default()
        intents.message_content = False
        intents.reactions = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.config = config
        self.database = database
        self.riddle_cog: RiddleCog | None = None
        self._startup_reconciled = False

    async def setup_hook(self) -> None:
        await asyncio.to_thread(self.database.initialize)

        cog = RiddleCog(self, self.database)
        await self.add_cog(cog)
        self.riddle_cog = cog

        # timeout=None と固定custom_idを持つViewを再登録し、再起動後も
        # 既存の「回答する」ボタンを処理できるようにする。
        self.add_view(AnswerView(cog))

        if self.config.guild_id is not None:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            logger.info(
                "Synced %d application commands to guild %d",
                len(synced),
                self.config.guild_id,
            )
        else:
            synced = await self.tree.sync()
            logger.info("Synced %d global application commands", len(synced))

    async def on_ready(self) -> None:
        if self.user is None:
            return
        logger.info(
            "Logged in as %s (%d); guilds=%d",
            self.user,
            self.user.id,
            len(self.guilds),
        )

        if not self._startup_reconciled and self.riddle_cog is not None:
            self._startup_reconciled = True
            await self.riddle_cog.reconcile_guild_data()

    async def on_guild_remove(self, guild: discord.Guild) -> None:
        await asyncio.to_thread(self.database.delete_guild_data, guild.id)
        logger.info("Deleted local data for guild %d after bot removal", guild.id)


def run_bot(config: BotConfig) -> None:
    database_path = Path(config.database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)
    database = RiddleDatabase(database_path)

    try:
        with SingleInstanceLock.for_database(database_path):
            bot = RiddleBot(config, database)
            bot.run(config.token, log_handler=None)
    except InstanceAlreadyRunningError as exc:
        raise SystemExit(
            "同じデータベースを使用するBotが既に起動しています。"
            "二重起動していないか確認してください。"
        ) from exc
