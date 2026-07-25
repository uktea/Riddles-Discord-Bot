from __future__ import annotations

import logging
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from riddle_bot.config import BotConfig, ConfigurationError


class BotConfigTests(unittest.TestCase):
    def test_loads_required_and_default_values(self) -> None:
        with patch.dict(os.environ, {"DISCORD_TOKEN": "test-token"}, clear=True):
            config = BotConfig.from_environment()

        self.assertEqual(config.token, "test-token")
        self.assertIsNone(config.guild_id)
        self.assertEqual(config.database_path, Path("data/riddles.db"))
        self.assertEqual(config.log_level, logging.INFO)

    def test_loads_all_overrides(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "DISCORD_GUILD_ID": "123456789",
            "DATABASE_PATH": "state/test.db",
            "LOG_LEVEL": "debug",
        }
        with patch.dict(os.environ, environment, clear=True):
            config = BotConfig.from_environment()

        self.assertEqual(config.guild_id, 123456789)
        self.assertEqual(config.database_path, Path("state/test.db"))
        self.assertEqual(config.log_level, logging.DEBUG)

    def test_rejects_missing_token(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            BotConfig.from_environment()

    def test_rejects_invalid_guild_id(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "DISCORD_GUILD_ID": "not-a-number",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            BotConfig.from_environment()

    def test_rejects_invalid_log_level(self) -> None:
        environment = {
            "DISCORD_TOKEN": "test-token",
            "LOG_LEVEL": "VERBOSE",
        }
        with (
            patch.dict(os.environ, environment, clear=True),
            self.assertRaises(ConfigurationError),
        ):
            BotConfig.from_environment()


if __name__ == "__main__":
    unittest.main()
