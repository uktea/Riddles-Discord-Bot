from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path


class ConfigurationError(ValueError):
    """起動に必要な設定が不正な場合のエラー。"""


@dataclass(frozen=True, slots=True)
class BotConfig:
    token: str
    guild_id: int | None
    database_path: Path
    log_level: int

    @classmethod
    def from_environment(cls) -> BotConfig:
        token = os.getenv("DISCORD_TOKEN", "").strip()
        if not token:
            raise ConfigurationError(
                "DISCORD_TOKEN が設定されていません。.env.example を参考に設定してください。"
            )

        raw_guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
        guild_id: int | None = None
        if raw_guild_id:
            try:
                guild_id = int(raw_guild_id)
            except ValueError as exc:
                raise ConfigurationError(
                    "DISCORD_GUILD_ID にはDiscordサーバーの数値IDを指定してください。"
                ) from exc
            if guild_id <= 0:
                raise ConfigurationError(
                    "DISCORD_GUILD_ID は正の整数で指定してください。"
                )

        database_path = Path(
            os.getenv("DATABASE_PATH", "data/riddles.db").strip() or "data/riddles.db"
        )

        level_name = os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO"
        log_level = logging.getLevelNamesMapping().get(level_name)
        if not isinstance(log_level, int):
            valid_levels = "DEBUG, INFO, WARNING, ERROR, CRITICAL"
            raise ConfigurationError(
                f"LOG_LEVEL が不正です。次のいずれかを指定してください: {valid_levels}"
            )

        return cls(
            token=token,
            guild_id=guild_id,
            database_path=database_path,
            log_level=log_level,
        )
