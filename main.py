from __future__ import annotations

import logging
import sys

from dotenv import load_dotenv

from riddle_bot.config import BotConfig, ConfigurationError
from riddle_bot.discord_app import run_bot


def configure_logging(level: int) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def main() -> int:
    load_dotenv()
    try:
        config = BotConfig.from_environment()
    except ConfigurationError as exc:
        print(f"設定エラー: {exc}", file=sys.stderr)
        return 2

    configure_logging(config.log_level)
    run_bot(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
