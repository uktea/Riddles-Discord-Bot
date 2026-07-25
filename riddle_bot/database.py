"""なぞなぞとサーバー設定を保存するSQLiteリポジトリ。"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from .core import (
    answer_matches,
    as_utc,
    prepare_answers,
    validate_answer_limit,
    validate_mode,
)

DEFAULT_MAX_ACTIVE = 10
DEFAULT_MENTION_WINNERS = True
DEFAULT_PUBLIC_ANSWERS = False
DEFAULT_ANSWER_LIMIT: int | None = None
DEFAULT_MANUAL_JUDGING = False
DEFAULT_ANSWER_ACCEPT_EMOJI = "✅"
DEFAULT_OGIRI_MVP_EMOJI = "👑"
DEFAULT_GOOD_QUESTION_EMOJI = "⭐"
OFFICIAL_EVALUATION_MARK_EMOJI = "🏆"
CURRENT_SCHEMA_VERSION = 3
ACTIVE_STATUS = "active"
SOLVED_STATUS = "solved"
EXPIRED_STATUS = "expired"
_UNSET = object()


class RiddleDatabaseError(RuntimeError):
    """永続化層で扱う業務エラーの基底クラス。"""


class RiddleNotFoundError(RiddleDatabaseError):
    """指定された問題が存在しない場合のエラー。"""


class ActiveRiddleLimitError(RiddleDatabaseError):
    """作成者の未終了問題数がサーバー設定の上限に達した場合のエラー。"""

    def __init__(self, current: int, limit: int) -> None:
        self.current = current
        self.limit = limit
        super().__init__(f"未終了の問題が上限（{limit}件）に達しています。")


class DiscordBindingError(RiddleDatabaseError):
    """問題へDiscord IDを矛盾する形で再設定しようとした場合のエラー。"""


class UnsupportedSchemaVersionError(RiddleDatabaseError):
    """実行中コードより新しいDBスキーマを開こうとした場合のエラー。"""


class SubmissionStatus(str, Enum):
    """回答受付の結果。"""

    CORRECT = "correct"
    WRONG = "wrong"
    ALREADY_CORRECT = "already_correct"
    CLOSED = "closed"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"
    LIMIT_REACHED = "limit_reached"


class ManualAcceptanceStatus(str, Enum):
    """公開回答への出題者採用の結果。"""

    ACCEPTED = "accepted"
    ALREADY_ACCEPTED = "already_accepted"
    MVP_MARKED = "mvp_marked"
    ALREADY_MVP = "already_mvp"
    NOT_OGIRI = "not_ogiri"
    FORBIDDEN = "forbidden"
    CLOSED = "closed"
    EXPIRED = "expired"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class Riddle:
    """DBへ保存する問題モデル。"""

    id: int
    guild_id: int
    channel_id: int
    thread_id: int | None
    message_id: int | None
    creator_id: int
    mode: str
    question: str
    answer_display: str
    answers_json: str
    deadline_at: datetime
    status: str
    winner_id: int | None
    created_at: datetime
    public_answers: bool = DEFAULT_PUBLIC_ANSWERS
    answer_limit: int | None = DEFAULT_ANSWER_LIMIT
    manual_judging: bool = DEFAULT_MANUAL_JUDGING
    answer_accept_emoji: str = DEFAULT_ANSWER_ACCEPT_EMOJI
    ogiri_mvp_emoji: str = DEFAULT_OGIRI_MVP_EMOJI
    good_question_emoji: str = DEFAULT_GOOD_QUESTION_EMOJI

    @property
    def normalized_answers(self) -> tuple[str, ...]:
        values = json.loads(self.answers_json)
        if not isinstance(values, list) or not all(
            isinstance(value, str) for value in values
        ):
            raise ValueError("answers_json の形式が不正です。")
        return tuple(values)


# 呼び出し側で用途が明瞭になるように別名も公開する。
ActiveRiddle = Riddle


@dataclass(frozen=True, slots=True)
class GuildSettings:
    guild_id: int
    allowed_channel_id: int | None = None
    max_active: int = DEFAULT_MAX_ACTIVE
    mention_winners: bool = DEFAULT_MENTION_WINNERS
    answer_accept_emoji: str = DEFAULT_ANSWER_ACCEPT_EMOJI
    ogiri_mvp_emoji: str = DEFAULT_OGIRI_MVP_EMOJI
    good_question_emoji: str = DEFAULT_GOOD_QUESTION_EMOJI


@dataclass(frozen=True, slots=True)
class UserPreferences:
    """サーバーごとに保持する、問題作成時の個人既定値。"""

    guild_id: int
    user_id: int
    public_answers: bool = DEFAULT_PUBLIC_ANSWERS
    answer_limit: int | None = DEFAULT_ANSWER_LIMIT


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    status: SubmissionStatus
    riddle: Riddle | None
    winner_ids: tuple[int, ...] = ()
    attempt_count: int = 0
    answer_limit: int | None = None

    @property
    def accepted(self) -> bool:
        return self.status is SubmissionStatus.CORRECT

    @property
    def outcome(self) -> SubmissionStatus:
        """``status`` の後方互換用の読みやすい別名。"""

        return self.status

    @property
    def riddle_id(self) -> int | None:
        return self.riddle.id if self.riddle is not None else None

    @property
    def attempt_recorded(self) -> bool:
        """今回の送信が回答回数へ加算されたか返す。"""

        return self.status in {
            SubmissionStatus.CORRECT,
            SubmissionStatus.WRONG,
            SubmissionStatus.ALREADY_CORRECT,
        }

    @property
    def remaining_attempts(self) -> int | None:
        """残り回答回数を返す。無制限の場合は ``None``。"""

        if self.answer_limit is None:
            return None
        return max(0, self.answer_limit - self.attempt_count)


@dataclass(frozen=True, slots=True)
class PublicAnswerMessage:
    """公開回答本文を除いた、Discordメッセージとの対応情報。"""

    message_id: int
    riddle_id: int
    user_id: int
    attempt_count: int
    posted_at: datetime
    accepted_at: datetime | None = None
    mvp_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ManualAcceptanceResult:
    status: ManualAcceptanceStatus
    riddle: Riddle | None
    answer_message: PublicAnswerMessage | None = None


@dataclass(frozen=True, slots=True)
class UserDataSummary:
    guild_id: int
    user_id: int
    created_riddles: int
    active_created_riddles: int
    correct_riddles: int
    first_wins: int
    answer_attempts: int = 0
    public_answer_messages: int = 0
    has_preferences: bool = False


@dataclass(frozen=True, slots=True)
class UserDataDeletion:
    guild_id: int
    user_id: int
    deleted_riddles: int
    deleted_winner_entries: int
    cleared_winner_references: int
    deleted_answer_attempts: int = 0
    deleted_public_answer_messages: int = 0
    deleted_preferences: bool = False


@dataclass(frozen=True, slots=True)
class GuildDataDeletion:
    guild_id: int
    deleted_riddles: int
    deleted_winner_entries: int
    deleted_settings: bool
    deleted_answer_attempts: int = 0
    deleted_public_answer_messages: int = 0
    deleted_user_preferences: int = 0


def _require_positive_integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} は正の整数で指定してください。")
    return value


def _validate_emoji_setting(value: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} は文字列で指定してください。")
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 100
        or any(character.isspace() for character in normalized)
    ):
        raise ValueError(f"{field_name} は空白を含まない絵文字1件で指定してください。")
    if emoji_identity_key(normalized) == emoji_identity_key(
        OFFICIAL_EVALUATION_MARK_EMOJI
    ):
        raise ValueError(
            f"{OFFICIAL_EVALUATION_MARK_EMOJI} はBotの公式記録マーク用のため"
            "設定できません。"
        )
    return normalized


def emoji_identity_key(value: str) -> tuple[str, str]:
    """Discord上で同じリアクションになる絵文字の比較キーを返す。"""

    custom_emoji = re.fullmatch(r"<a?:[^:>\s]+:(\d+)>", value)
    if custom_emoji is not None:
        # カスタム絵文字は改名後もsnowflake IDが同じ。
        return ("custom", custom_emoji.group(1))

    normalized = unicodedata.normalize("NFC", value)
    normalized = normalized.replace("\N{VARIATION SELECTOR-15}", "").replace(
        "\N{VARIATION SELECTOR-16}",
        "",
    )
    return ("unicode", normalized)


def _require_distinct_evaluation_emojis(
    answer_accept_emoji: str,
    ogiri_mvp_emoji: str,
    good_question_emoji: str,
) -> None:
    identities = {
        emoji_identity_key(answer_accept_emoji),
        emoji_identity_key(ogiri_mvp_emoji),
        emoji_identity_key(good_question_emoji),
    }
    if len(identities) != 3:
        raise ValueError("3種類の評価絵文字はそれぞれ別の絵文字にしてください。")


def _datetime_to_text(value: datetime) -> str:
    return as_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime_from_text(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return as_utc(parsed)


def _riddle_from_row(row: sqlite3.Row) -> Riddle:
    return Riddle(
        id=row["id"],
        guild_id=row["guild_id"],
        channel_id=row["channel_id"],
        thread_id=row["thread_id"],
        message_id=row["message_id"],
        creator_id=row["creator_id"],
        mode=row["mode"],
        question=row["question"],
        answer_display=row["answer_display"],
        answers_json=row["answers_json"],
        deadline_at=_datetime_from_text(row["deadline_at"]),
        status=row["status"],
        winner_id=row["winner_id"],
        created_at=_datetime_from_text(row["created_at"]),
        public_answers=bool(row["public_answers"]),
        answer_limit=row["answer_limit"],
        manual_judging=bool(row["manual_judging"]),
        answer_accept_emoji=row["answer_accept_emoji"],
        ogiri_mvp_emoji=row["ogiri_mvp_emoji"],
        good_question_emoji=row["good_question_emoji"],
    )


def _settings_from_row(row: sqlite3.Row) -> GuildSettings:
    return GuildSettings(
        guild_id=row["guild_id"],
        allowed_channel_id=row["allowed_channel_id"],
        max_active=row["max_active"],
        mention_winners=bool(row["mention_winners"]),
        answer_accept_emoji=row["answer_accept_emoji"],
        ogiri_mvp_emoji=row["ogiri_mvp_emoji"],
        good_question_emoji=row["good_question_emoji"],
    )


def _user_preferences_from_row(row: sqlite3.Row) -> UserPreferences:
    return UserPreferences(
        guild_id=row["guild_id"],
        user_id=row["user_id"],
        public_answers=bool(row["public_answers"]),
        answer_limit=row["answer_limit"],
    )


def _public_answer_message_from_row(row: sqlite3.Row) -> PublicAnswerMessage:
    accepted_at = row["accepted_at"]
    mvp_at = row["mvp_at"]
    return PublicAnswerMessage(
        message_id=row["message_id"],
        riddle_id=row["riddle_id"],
        user_id=row["user_id"],
        attempt_count=row["attempt_count"],
        posted_at=_datetime_from_text(row["posted_at"]),
        accepted_at=(
            _datetime_from_text(accepted_at) if accepted_at is not None else None
        ),
        mvp_at=_datetime_from_text(mvp_at) if mvp_at is not None else None,
    )


class RiddleDatabase:
    """操作ごとに接続を開く、スレッドセーフなSQLiteリポジトリ。"""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        raw_path = os.fspath(path)
        if raw_path == ":memory:":
            raise ValueError(
                "操作ごとに接続するため :memory: は利用できません。一時ファイルを指定してください。"
            )
        database_path = Path(raw_path)
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self.path = database_path
        self.initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.path,
            timeout=10.0,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 10000")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write_connection(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        """必要なテーブルと索引を冪等に作成する。"""

        with self._connect() as connection:
            schema_version = connection.execute("PRAGMA user_version").fetchone()[0]
            if schema_version > CURRENT_SCHEMA_VERSION:
                raise UnsupportedSchemaVersionError(
                    "このデータベースは新しいバージョンのBotで作成されています"
                    f"（DB: v{schema_version} / 対応: v{CURRENT_SCHEMA_VERSION}）。"
                    "対応するBotへ更新してください。"
                )
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    allowed_channel_id INTEGER,
                    max_active INTEGER NOT NULL DEFAULT 10
                        CHECK (max_active >= 1),
                    mention_winners INTEGER NOT NULL DEFAULT 1
                        CHECK (mention_winners IN (0, 1)),
                    answer_accept_emoji TEXT NOT NULL DEFAULT '✅',
                    ogiri_mvp_emoji TEXT NOT NULL DEFAULT '👑',
                    good_question_emoji TEXT NOT NULL DEFAULT '⭐'
                );

                CREATE TABLE IF NOT EXISTS riddles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    channel_id INTEGER NOT NULL,
                    thread_id INTEGER,
                    message_id INTEGER,
                    creator_id INTEGER NOT NULL,
                    mode TEXT NOT NULL CHECK (mode IN ('briddle', 'riddle')),
                    question TEXT NOT NULL,
                    answer_display TEXT NOT NULL,
                    answers_json TEXT NOT NULL,
                    deadline_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active', 'solved', 'expired')),
                    winner_id INTEGER,
                    created_at TEXT NOT NULL,
                    public_answers INTEGER NOT NULL DEFAULT 0
                        CHECK (public_answers IN (0, 1)),
                    answer_limit INTEGER
                        CHECK (
                            answer_limit IS NULL
                            OR answer_limit BETWEEN 1 AND 100
                        ),
                    manual_judging INTEGER NOT NULL DEFAULT 0
                        CHECK (manual_judging IN (0, 1)),
                    answer_accept_emoji TEXT NOT NULL DEFAULT '✅',
                    ogiri_mvp_emoji TEXT NOT NULL DEFAULT '👑',
                    good_question_emoji TEXT NOT NULL DEFAULT '⭐'
                );

                CREATE TABLE IF NOT EXISTS riddle_winners (
                    riddle_id INTEGER NOT NULL
                        REFERENCES riddles(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    answered_at TEXT NOT NULL,
                    PRIMARY KEY (riddle_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS riddle_attempts (
                    riddle_id INTEGER NOT NULL
                        REFERENCES riddles(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL
                        CHECK (attempt_count >= 1),
                    last_answered_at TEXT NOT NULL,
                    PRIMARY KEY (riddle_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS riddle_answer_messages (
                    message_id INTEGER PRIMARY KEY,
                    riddle_id INTEGER NOT NULL
                        REFERENCES riddles(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    attempt_count INTEGER NOT NULL
                        CHECK (attempt_count >= 1),
                    posted_at TEXT NOT NULL,
                    accepted_at TEXT,
                    mvp_at TEXT,
                    UNIQUE (riddle_id, user_id, attempt_count)
                );

                CREATE TABLE IF NOT EXISTS user_preferences (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    public_answers INTEGER NOT NULL DEFAULT 0
                        CHECK (public_answers IN (0, 1)),
                    answer_limit INTEGER
                        CHECK (
                            answer_limit IS NULL
                            OR answer_limit BETWEEN 1 AND 100
                        ),
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_riddles_thread_id
                    ON riddles(thread_id) WHERE thread_id IS NOT NULL;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_riddles_message_id
                    ON riddles(message_id) WHERE message_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_riddles_active_creator
                    ON riddles(guild_id, creator_id, status);
                CREATE INDEX IF NOT EXISTS idx_riddles_due
                    ON riddles(status, deadline_at);
                CREATE INDEX IF NOT EXISTS idx_riddle_winners_user
                    ON riddle_winners(user_id);
                CREATE INDEX IF NOT EXISTS idx_riddle_attempts_user
                    ON riddle_attempts(user_id);
                CREATE INDEX IF NOT EXISTS idx_riddle_answer_messages_user
                    ON riddle_answer_messages(user_id);
                CREATE INDEX IF NOT EXISTS idx_user_preferences_user
                    ON user_preferences(user_id);
                """
            )
            # CREATE TABLE IF NOT EXISTS は既存テーブルへ列を追加しないため、
            # V1のDBもデータを保持したまま新しい問題設定へ移行する。
            guild_setting_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(guild_settings)"
                ).fetchall()
            }
            for column_name, default_value in (
                ("answer_accept_emoji", DEFAULT_ANSWER_ACCEPT_EMOJI),
                ("ogiri_mvp_emoji", DEFAULT_OGIRI_MVP_EMOJI),
                ("good_question_emoji", DEFAULT_GOOD_QUESTION_EMOJI),
            ):
                if column_name not in guild_setting_columns:
                    escaped_default = default_value.replace("'", "''")
                    connection.execute(
                        f"""
                        ALTER TABLE guild_settings
                        ADD COLUMN {column_name} TEXT NOT NULL
                            DEFAULT '{escaped_default}'
                        """
                    )
            riddle_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(riddles)").fetchall()
            }
            if "public_answers" not in riddle_columns:
                connection.execute(
                    """
                    ALTER TABLE riddles
                    ADD COLUMN public_answers INTEGER NOT NULL DEFAULT 0
                        CHECK (public_answers IN (0, 1))
                    """
                )
            if "answer_limit" not in riddle_columns:
                connection.execute(
                    """
                    ALTER TABLE riddles
                    ADD COLUMN answer_limit INTEGER
                        CHECK (
                            answer_limit IS NULL
                            OR answer_limit BETWEEN 1 AND 100
                        )
                    """
                )
            if "manual_judging" not in riddle_columns:
                connection.execute(
                    """
                    ALTER TABLE riddles
                    ADD COLUMN manual_judging INTEGER NOT NULL DEFAULT 0
                        CHECK (manual_judging IN (0, 1))
                    """
                )
            for column_name, default_value in (
                ("answer_accept_emoji", DEFAULT_ANSWER_ACCEPT_EMOJI),
                ("ogiri_mvp_emoji", DEFAULT_OGIRI_MVP_EMOJI),
                ("good_question_emoji", DEFAULT_GOOD_QUESTION_EMOJI),
            ):
                if column_name not in riddle_columns:
                    escaped_default = default_value.replace("'", "''")
                    connection.execute(
                        f"""
                        ALTER TABLE riddles
                        ADD COLUMN {column_name} TEXT NOT NULL
                            DEFAULT '{escaped_default}'
                        """
                    )
            answer_message_columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(riddle_answer_messages)"
                ).fetchall()
            }
            if "mvp_at" not in answer_message_columns:
                connection.execute(
                    """
                    ALTER TABLE riddle_answer_messages
                    ADD COLUMN mvp_at TEXT
                    """
                )
            connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

    def create_riddle(
        self,
        *,
        guild_id: int,
        channel_id: int,
        creator_id: int,
        mode: str,
        question: str,
        answer_display: str,
        deadline_at: datetime,
        thread_id: int | None = None,
        message_id: int | None = None,
        created_at: datetime | None = None,
        public_answers: bool = DEFAULT_PUBLIC_ANSWERS,
        answer_limit: int | None = DEFAULT_ANSWER_LIMIT,
        manual_judging: bool = DEFAULT_MANUAL_JUDGING,
        answer_accept_emoji: str = DEFAULT_ANSWER_ACCEPT_EMOJI,
        ogiri_mvp_emoji: str = DEFAULT_OGIRI_MVP_EMOJI,
        good_question_emoji: str = DEFAULT_GOOD_QUESTION_EMOJI,
    ) -> Riddle:
        """問題を作成し、作成者単位の未終了問題数上限を原子的に守る。"""

        guild_id = _require_positive_integer(guild_id, "guild_id")
        channel_id = _require_positive_integer(channel_id, "channel_id")
        creator_id = _require_positive_integer(creator_id, "creator_id")
        if thread_id is not None:
            thread_id = _require_positive_integer(thread_id, "thread_id")
        if message_id is not None:
            message_id = _require_positive_integer(message_id, "message_id")
        if not isinstance(public_answers, bool):
            raise TypeError("public_answers は真偽値で指定してください。")
        if not isinstance(manual_judging, bool):
            raise TypeError("manual_judging は真偽値で指定してください。")
        answer_accept_emoji = _validate_emoji_setting(
            answer_accept_emoji,
            "answer_accept_emoji",
        )
        ogiri_mvp_emoji = _validate_emoji_setting(
            ogiri_mvp_emoji,
            "ogiri_mvp_emoji",
        )
        good_question_emoji = _validate_emoji_setting(
            good_question_emoji,
            "good_question_emoji",
        )
        _require_distinct_evaluation_emojis(
            answer_accept_emoji,
            ogiri_mvp_emoji,
            good_question_emoji,
        )
        answer_limit = validate_answer_limit(answer_limit)
        normalized_mode = validate_mode(mode)
        if not isinstance(question, str) or not question.strip():
            raise ValueError("問題文を空にはできません。")
        if manual_judging:
            if normalized_mode != "riddle":
                raise ValueError("手動採用方式はriddleでのみ利用できます。")
            if not public_answers:
                raise ValueError("手動採用方式では回答公開を有効にしてください。")
            if answer_display:
                raise ValueError("手動採用方式では正解文字列を指定できません。")
            normalized_answers: tuple[str, ...] = ()
        else:
            normalized_answers = prepare_answers(answer_display)
        deadline_text = _datetime_to_text(deadline_at)
        created_text = _datetime_to_text(created_at or datetime.now(timezone.utc))
        answers_json = json.dumps(
            normalized_answers,
            ensure_ascii=False,
            separators=(",", ":"),
        )

        with self._write_connection() as connection:
            settings_row = connection.execute(
                "SELECT max_active FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            limit = (
                settings_row["max_active"]
                if settings_row is not None
                else DEFAULT_MAX_ACTIVE
            )
            current = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddles
                WHERE guild_id = ? AND creator_id = ? AND status = 'active'
                """,
                (guild_id, creator_id),
            ).fetchone()["count"]
            if current >= limit:
                raise ActiveRiddleLimitError(current=current, limit=limit)

            try:
                cursor = connection.execute(
                    """
                    INSERT INTO riddles (
                        guild_id, channel_id, thread_id, message_id, creator_id,
                        mode, question, answer_display, answers_json, deadline_at,
                        status, winner_id, created_at, public_answers, answer_limit,
                        manual_judging, answer_accept_emoji, ogiri_mvp_emoji,
                        good_question_emoji
                    )
                    VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                        'active', NULL, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        guild_id,
                        channel_id,
                        thread_id,
                        message_id,
                        creator_id,
                        normalized_mode,
                        question,
                        answer_display,
                        answers_json,
                        deadline_text,
                        created_text,
                        int(public_answers),
                        answer_limit,
                        int(manual_judging),
                        answer_accept_emoji,
                        ogiri_mvp_emoji,
                        good_question_emoji,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                if "thread_id" in str(exc) or "message_id" in str(exc):
                    raise DiscordBindingError(
                        "そのDiscordメッセージまたはスレッドは既に別の問題へ紐付いています。"
                    ) from exc
                raise
            row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (cursor.lastrowid,),
            ).fetchone()
        return _riddle_from_row(row)

    def bind_discord_ids(
        self,
        riddle_id: int,
        *,
        thread_id: int | None = None,
        message_id: int | None = None,
        channel_id: int | None = None,
    ) -> Riddle:
        """作成済み問題にDiscord側で確定したIDを冪等に紐付ける。"""

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        if thread_id is None and message_id is None and channel_id is None:
            raise ValueError("紐付けるDiscord IDを1件以上指定してください。")
        if thread_id is not None:
            thread_id = _require_positive_integer(thread_id, "thread_id")
        if message_id is not None:
            message_id = _require_positive_integer(message_id, "message_id")
        if channel_id is not None:
            channel_id = _require_positive_integer(channel_id, "channel_id")

        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle_id,),
            ).fetchone()
            if row is None:
                raise RiddleNotFoundError("指定された問題が見つかりません。")

            for field_name, requested in (
                ("thread_id", thread_id),
                ("message_id", message_id),
            ):
                existing = row[field_name]
                if (
                    requested is not None
                    and existing is not None
                    and existing != requested
                ):
                    raise DiscordBindingError(
                        f"{field_name} は既に別の値へ紐付いています。"
                    )

            try:
                connection.execute(
                    """
                    UPDATE riddles
                    SET thread_id = COALESCE(?, thread_id),
                        message_id = COALESCE(?, message_id),
                        channel_id = COALESCE(?, channel_id)
                    WHERE id = ?
                    """,
                    (thread_id, message_id, channel_id, riddle_id),
                )
            except sqlite3.IntegrityError as exc:
                raise DiscordBindingError(
                    "そのDiscordメッセージまたはスレッドは既に別の問題へ紐付いています。"
                ) from exc
            updated = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle_id,),
            ).fetchone()
        return _riddle_from_row(updated)

    def get_riddle(self, riddle_id: int) -> Riddle | None:
        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle_id,),
            ).fetchone()
        return _riddle_from_row(row) if row is not None else None

    def _get_riddle_by_discord_id(
        self,
        column: str,
        value: int,
        *,
        active_only: bool,
    ) -> Riddle | None:
        value = _require_positive_integer(value, column)
        status_clause = " AND status = 'active'" if active_only else ""
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM riddles WHERE {column} = ?{status_clause}",
                (value,),
            ).fetchone()
        return _riddle_from_row(row) if row is not None else None

    def get_riddle_by_thread(
        self,
        thread_id: int,
        *,
        active_only: bool = False,
    ) -> Riddle | None:
        return self._get_riddle_by_discord_id(
            "thread_id",
            thread_id,
            active_only=active_only,
        )

    def get_riddle_by_message(
        self,
        message_id: int,
        *,
        active_only: bool = False,
    ) -> Riddle | None:
        return self._get_riddle_by_discord_id(
            "message_id",
            message_id,
            active_only=active_only,
        )

    def list_active_riddles(
        self,
        guild_id: int,
        *,
        creator_id: int | None = None,
    ) -> list[Riddle]:
        guild_id = _require_positive_integer(guild_id, "guild_id")
        parameters: tuple[int, ...]
        creator_clause = ""
        if creator_id is None:
            parameters = (guild_id,)
        else:
            creator_id = _require_positive_integer(creator_id, "creator_id")
            parameters = (guild_id, creator_id)
            creator_clause = " AND creator_id = ?"
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM riddles
                WHERE guild_id = ? AND status = 'active'{creator_clause}
                ORDER BY deadline_at, id
                """,
                parameters,
            ).fetchall()
        return [_riddle_from_row(row) for row in rows]

    def list_riddles_by_creator(
        self,
        guild_id: int,
        creator_id: int,
    ) -> list[Riddle]:
        """個人データ削除前のDiscord後始末用に、作成問題を全状態で返す。"""

        guild_id = _require_positive_integer(guild_id, "guild_id")
        creator_id = _require_positive_integer(creator_id, "creator_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM riddles
                WHERE guild_id = ? AND creator_id = ?
                ORDER BY created_at, id
                """,
                (guild_id, creator_id),
            ).fetchall()
        return [_riddle_from_row(row) for row in rows]

    def count_active_riddles(
        self,
        guild_id: int,
        *,
        creator_id: int | None = None,
    ) -> int:
        guild_id = _require_positive_integer(guild_id, "guild_id")
        parameters: tuple[int, ...]
        creator_clause = ""
        if creator_id is None:
            parameters = (guild_id,)
        else:
            creator_id = _require_positive_integer(creator_id, "creator_id")
            parameters = (guild_id, creator_id)
            creator_clause = " AND creator_id = ?"
        with self._connect() as connection:
            row = connection.execute(
                f"""
                SELECT COUNT(*) AS count FROM riddles
                WHERE guild_id = ? AND status = 'active'{creator_clause}
                """,
                parameters,
            ).fetchone()
        return row["count"]

    def list_due_riddles(self, now: datetime | None = None) -> list[Riddle]:
        """期限を迎えたactive問題を返す（この操作だけでは状態を変えない）。"""

        deadline = _datetime_to_text(now or datetime.now(timezone.utc))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM riddles
                WHERE status = 'active' AND deadline_at <= ?
                ORDER BY deadline_at, id
                """,
                (deadline,),
            ).fetchall()
        return [_riddle_from_row(row) for row in rows]

    def mark_riddle_expired(
        self,
        riddle_id: int,
        *,
        now: datetime | None = None,
    ) -> Riddle | None:
        """期限到達済みのactive問題をexpiredへ移す。"""

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        current_time = _datetime_to_text(now or datetime.now(timezone.utc))
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle_id,),
            ).fetchone()
            if row is None:
                return None
            if row["status"] == EXPIRED_STATUS:
                return _riddle_from_row(row)
            if row["status"] != ACTIVE_STATUS or row["deadline_at"] > current_time:
                return None
            connection.execute(
                "UPDATE riddles SET status = 'expired' WHERE id = ? AND status = 'active'",
                (riddle_id,),
            )
            updated = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle_id,),
            ).fetchone()
        return _riddle_from_row(updated)

    def list_pending_finalizations(
        self,
        guild_id: int | None = None,
    ) -> list[Riddle]:
        """結果通知が成功するまで保持しているsolved/expired問題を返す。"""

        guild_clause = ""
        parameters: tuple[int, ...] = ()
        if guild_id is not None:
            guild_id = _require_positive_integer(guild_id, "guild_id")
            guild_clause = " AND guild_id = ?"
            parameters = (guild_id,)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM riddles
                WHERE status IN ('solved', 'expired'){guild_clause}
                ORDER BY deadline_at, id
                """,
                parameters,
            ).fetchall()
        return [_riddle_from_row(row) for row in rows]

    def list_guild_ids(self) -> list[int]:
        """設定・個人設定・問題データが存在する全guild IDを返す。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT guild_id FROM guild_settings
                UNION
                SELECT guild_id FROM user_preferences
                UNION
                SELECT guild_id FROM riddles
                ORDER BY guild_id
                """
            ).fetchall()
        return [row["guild_id"] for row in rows]

    def submit_answer(
        self,
        riddle_id: int,
        user_id: int,
        answer: str,
        *,
        now: datetime | None = None,
    ) -> SubmissionResult:
        """問題IDを使って回答を原子的に受付する。"""

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        return self._submit_answer(
            lookup_column="id",
            lookup_value=riddle_id,
            user_id=user_id,
            answer=answer,
            now=now,
        )

    def submit_answer_by_thread(
        self,
        thread_id: int,
        user_id: int,
        answer: str,
        *,
        now: datetime | None = None,
    ) -> SubmissionResult:
        """スレッドIDを使って回答を原子的に受付する。"""

        thread_id = _require_positive_integer(thread_id, "thread_id")
        return self._submit_answer(
            lookup_column="thread_id",
            lookup_value=thread_id,
            user_id=user_id,
            answer=answer,
            now=now,
        )

    def submit_answer_by_message(
        self,
        message_id: int,
        user_id: int,
        answer: str,
        *,
        now: datetime | None = None,
    ) -> SubmissionResult:
        """persistent buttonのメッセージIDで回答を原子的に受付する。"""

        message_id = _require_positive_integer(message_id, "message_id")
        return self._submit_answer(
            lookup_column="message_id",
            lookup_value=message_id,
            user_id=user_id,
            answer=answer,
            now=now,
        )

    def _submit_answer(
        self,
        *,
        lookup_column: str,
        lookup_value: int,
        user_id: int,
        answer: str,
        now: datetime | None,
    ) -> SubmissionResult:
        user_id = _require_positive_integer(user_id, "user_id")
        current_time = _datetime_to_text(now or datetime.now(timezone.utc))

        with self._write_connection() as connection:
            row = connection.execute(
                f"SELECT * FROM riddles WHERE {lookup_column} = ?",
                (lookup_value,),
            ).fetchone()
            if row is None:
                return SubmissionResult(SubmissionStatus.NOT_FOUND, None)

            riddle = _riddle_from_row(row)
            attempt_count = self._get_answer_attempt_count_in_connection(
                connection,
                riddle.id,
                user_id,
            )
            if riddle.status != ACTIVE_STATUS:
                result_status = (
                    SubmissionStatus.EXPIRED
                    if riddle.status == EXPIRED_STATUS
                    else SubmissionStatus.CLOSED
                )
                return SubmissionResult(
                    result_status,
                    riddle,
                    self._list_winner_ids_in_connection(connection, riddle.id),
                    attempt_count,
                    riddle.answer_limit,
                )

            if row["deadline_at"] <= current_time:
                connection.execute(
                    "UPDATE riddles SET status = 'expired' WHERE id = ?",
                    (riddle.id,),
                )
                expired_row = connection.execute(
                    "SELECT * FROM riddles WHERE id = ?",
                    (riddle.id,),
                ).fetchone()
                return SubmissionResult(
                    SubmissionStatus.EXPIRED,
                    _riddle_from_row(expired_row),
                    self._list_winner_ids_in_connection(connection, riddle.id),
                    attempt_count,
                    riddle.answer_limit,
                )

            if riddle.answer_limit is not None and attempt_count >= riddle.answer_limit:
                return SubmissionResult(
                    SubmissionStatus.LIMIT_REACHED,
                    riddle,
                    self._list_winner_ids_in_connection(connection, riddle.id),
                    attempt_count,
                    riddle.answer_limit,
                )

            # answer は比較中だけメモリに存在し、DBへは一切書き込まない。
            is_correct = answer_matches(answer, riddle.normalized_answers)
            connection.execute(
                """
                INSERT INTO riddle_attempts (
                    riddle_id, user_id, attempt_count, last_answered_at
                )
                VALUES (?, ?, 1, ?)
                ON CONFLICT(riddle_id, user_id) DO UPDATE SET
                    attempt_count = riddle_attempts.attempt_count + 1,
                    last_answered_at = excluded.last_answered_at
                """,
                (riddle.id, user_id, current_time),
            )
            attempt_count += 1

            if not is_correct:
                return SubmissionResult(
                    SubmissionStatus.WRONG,
                    riddle,
                    self._list_winner_ids_in_connection(connection, riddle.id),
                    attempt_count,
                    riddle.answer_limit,
                )

            if riddle.mode == "briddle":
                connection.execute(
                    """
                    UPDATE riddles
                    SET status = 'solved', winner_id = ?
                    WHERE id = ? AND status = 'active'
                    """,
                    (user_id, riddle.id),
                )
                connection.execute(
                    """
                    INSERT INTO riddle_winners (riddle_id, user_id, answered_at)
                    VALUES (?, ?, ?)
                    """,
                    (riddle.id, user_id, current_time),
                )
                status = SubmissionStatus.CORRECT
            else:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO riddle_winners
                        (riddle_id, user_id, answered_at)
                    VALUES (?, ?, ?)
                    """,
                    (riddle.id, user_id, current_time),
                )
                if cursor.rowcount == 0:
                    status = SubmissionStatus.ALREADY_CORRECT
                else:
                    connection.execute(
                        """
                        UPDATE riddles
                        SET winner_id = COALESCE(winner_id, ?)
                        WHERE id = ? AND status = 'active'
                        """,
                        (user_id, riddle.id),
                    )
                    status = SubmissionStatus.CORRECT

            updated_row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle.id,),
            ).fetchone()
            winner_ids = self._list_winner_ids_in_connection(connection, riddle.id)
        return SubmissionResult(
            status,
            _riddle_from_row(updated_row),
            winner_ids,
            attempt_count,
            riddle.answer_limit,
        )

    @staticmethod
    def _get_answer_attempt_count_in_connection(
        connection: sqlite3.Connection,
        riddle_id: int,
        user_id: int,
    ) -> int:
        row = connection.execute(
            """
            SELECT attempt_count
            FROM riddle_attempts
            WHERE riddle_id = ? AND user_id = ?
            """,
            (riddle_id, user_id),
        ).fetchone()
        return row["attempt_count"] if row is not None else 0

    def get_answer_attempt_count(self, riddle_id: int, user_id: int) -> int:
        """指定ユーザーが問題へ送信した、受付済み回答回数を返す。"""

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        user_id = _require_positive_integer(user_id, "user_id")
        with self._connect() as connection:
            return self._get_answer_attempt_count_in_connection(
                connection,
                riddle_id,
                user_id,
            )

    @staticmethod
    def _list_winner_ids_in_connection(
        connection: sqlite3.Connection,
        riddle_id: int,
    ) -> tuple[int, ...]:
        rows = connection.execute(
            """
            SELECT user_id FROM riddle_winners
            WHERE riddle_id = ?
            ORDER BY answered_at, user_id
            """,
            (riddle_id,),
        ).fetchall()
        return tuple(row["user_id"] for row in rows)

    def list_winner_ids(self, riddle_id: int) -> tuple[int, ...]:
        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        with self._connect() as connection:
            return self._list_winner_ids_in_connection(connection, riddle_id)

    def register_public_answer_message(
        self,
        *,
        message_id: int,
        riddle_id: int,
        user_id: int,
        attempt_count: int,
        posted_at: datetime | None = None,
    ) -> PublicAnswerMessage:
        """公開回答本文を保存せず、Discordメッセージとの対応だけを登録する。"""

        message_id = _require_positive_integer(message_id, "message_id")
        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        user_id = _require_positive_integer(user_id, "user_id")
        attempt_count = _require_positive_integer(attempt_count, "attempt_count")
        posted_text = _datetime_to_text(posted_at or datetime.now(timezone.utc))

        with self._write_connection() as connection:
            riddle_row = connection.execute(
                """
                SELECT * FROM riddles
                WHERE id = ? AND public_answers = 1
                """,
                (riddle_id,),
            ).fetchone()
            if riddle_row is None:
                raise RiddleNotFoundError("公開回答を紐付ける問題が見つかりません。")

            recorded_attempt = self._get_answer_attempt_count_in_connection(
                connection,
                riddle_id,
                user_id,
            )
            if attempt_count > recorded_attempt:
                raise ValueError("未記録の回答回数へメッセージを紐付けられません。")

            try:
                connection.execute(
                    """
                    INSERT INTO riddle_answer_messages (
                        message_id, riddle_id, user_id, attempt_count, posted_at,
                        accepted_at
                    )
                    VALUES (?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        message_id,
                        riddle_id,
                        user_id,
                        attempt_count,
                        posted_text,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DiscordBindingError(
                    "その公開回答メッセージは既に登録されています。"
                ) from exc
            row = connection.execute(
                """
                SELECT * FROM riddle_answer_messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
        return _public_answer_message_from_row(row)

    def get_public_answer_message(
        self,
        message_id: int,
    ) -> PublicAnswerMessage | None:
        message_id = _require_positive_integer(message_id, "message_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM riddle_answer_messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
        return _public_answer_message_from_row(row) if row is not None else None

    def accept_public_answer_message(
        self,
        message_id: int,
        evaluator_id: int,
        *,
        now: datetime | None = None,
    ) -> ManualAcceptanceResult:
        """出題者の評価により、公開回答を正解・採用として原子的に記録する。"""

        message_id = _require_positive_integer(message_id, "message_id")
        evaluator_id = _require_positive_integer(evaluator_id, "evaluator_id")
        accepted_text = _datetime_to_text(now or datetime.now(timezone.utc))

        with self._write_connection() as connection:
            answer_row = connection.execute(
                """
                SELECT * FROM riddle_answer_messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            if answer_row is None:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.NOT_FOUND,
                    None,
                )

            riddle_row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (answer_row["riddle_id"],),
            ).fetchone()
            if riddle_row is None:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.NOT_FOUND,
                    None,
                )
            riddle = _riddle_from_row(riddle_row)
            answer_message = _public_answer_message_from_row(answer_row)

            if evaluator_id != riddle.creator_id:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.FORBIDDEN,
                    riddle,
                    answer_message,
                )
            if riddle.status != ACTIVE_STATUS:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.CLOSED,
                    riddle,
                    answer_message,
                )
            if riddle_row["deadline_at"] <= accepted_text:
                connection.execute(
                    "UPDATE riddles SET status = 'expired' WHERE id = ?",
                    (riddle.id,),
                )
                expired_row = connection.execute(
                    "SELECT * FROM riddles WHERE id = ?",
                    (riddle.id,),
                ).fetchone()
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.EXPIRED,
                    _riddle_from_row(expired_row),
                    answer_message,
                )
            if answer_row["accepted_at"] is not None:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.ALREADY_ACCEPTED,
                    riddle,
                    answer_message,
                )

            connection.execute(
                """
                UPDATE riddle_answer_messages
                SET accepted_at = ?
                WHERE message_id = ? AND accepted_at IS NULL
                """,
                (accepted_text, message_id),
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO riddle_winners
                    (riddle_id, user_id, answered_at)
                VALUES (?, ?, ?)
                """,
                (riddle.id, answer_message.user_id, accepted_text),
            )
            connection.execute(
                """
                UPDATE riddles
                SET winner_id = COALESCE(winner_id, ?)
                WHERE id = ? AND status = 'active'
                """,
                (answer_message.user_id, riddle.id),
            )
            updated_riddle_row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle.id,),
            ).fetchone()
            updated_answer_row = connection.execute(
                """
                SELECT * FROM riddle_answer_messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()

        return ManualAcceptanceResult(
            ManualAcceptanceStatus.ACCEPTED,
            _riddle_from_row(updated_riddle_row),
            _public_answer_message_from_row(updated_answer_row),
        )

    def mark_public_answer_mvp(
        self,
        message_id: int,
        evaluator_id: int,
        *,
        now: datetime | None = None,
    ) -> ManualAcceptanceResult:
        """大喜利の公開回答を、出題者のMVPとして記録する。"""

        message_id = _require_positive_integer(message_id, "message_id")
        evaluator_id = _require_positive_integer(evaluator_id, "evaluator_id")
        marked_text = _datetime_to_text(now or datetime.now(timezone.utc))

        with self._write_connection() as connection:
            answer_row = connection.execute(
                """
                SELECT * FROM riddle_answer_messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()
            if answer_row is None:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.NOT_FOUND,
                    None,
                )

            riddle_row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (answer_row["riddle_id"],),
            ).fetchone()
            if riddle_row is None:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.NOT_FOUND,
                    None,
                )
            riddle = _riddle_from_row(riddle_row)
            answer_message = _public_answer_message_from_row(answer_row)

            if evaluator_id != riddle.creator_id:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.FORBIDDEN,
                    riddle,
                    answer_message,
                )
            if not riddle.manual_judging:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.NOT_OGIRI,
                    riddle,
                    answer_message,
                )
            if riddle.status != ACTIVE_STATUS:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.CLOSED,
                    riddle,
                    answer_message,
                )
            if riddle_row["deadline_at"] <= marked_text:
                connection.execute(
                    "UPDATE riddles SET status = 'expired' WHERE id = ?",
                    (riddle.id,),
                )
                expired_row = connection.execute(
                    "SELECT * FROM riddles WHERE id = ?",
                    (riddle.id,),
                ).fetchone()
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.EXPIRED,
                    _riddle_from_row(expired_row),
                    answer_message,
                )
            if answer_row["mvp_at"] is not None:
                return ManualAcceptanceResult(
                    ManualAcceptanceStatus.ALREADY_MVP,
                    riddle,
                    answer_message,
                )

            connection.execute(
                """
                UPDATE riddle_answer_messages
                SET mvp_at = ?
                WHERE message_id = ? AND mvp_at IS NULL
                """,
                (marked_text, message_id),
            )
            updated_riddle_row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle.id,),
            ).fetchone()
            updated_answer_row = connection.execute(
                """
                SELECT * FROM riddle_answer_messages
                WHERE message_id = ?
                """,
                (message_id,),
            ).fetchone()

        return ManualAcceptanceResult(
            ManualAcceptanceStatus.MVP_MARKED,
            _riddle_from_row(updated_riddle_row),
            _public_answer_message_from_row(updated_answer_row),
        )

    def list_mvp_user_ids(self, riddle_id: int) -> tuple[int, ...]:
        """大喜利でMVPに選ばれたユーザーを、初回選出順で返す。"""

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT user_id, MIN(mvp_at) AS first_mvp_at
                FROM riddle_answer_messages
                WHERE riddle_id = ? AND mvp_at IS NOT NULL
                GROUP BY user_id
                ORDER BY first_mvp_at, user_id
                """,
                (riddle_id,),
            ).fetchall()
        return tuple(row["user_id"] for row in rows)

    def delete_riddle(self, riddle_id: int) -> Riddle | None:
        """状態を問わず問題を物理削除し、削除前の値を返す。"""

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM riddles WHERE id = ?",
                (riddle_id,),
            ).fetchone()
            if row is None:
                return None
            riddle = _riddle_from_row(row)
            connection.execute("DELETE FROM riddles WHERE id = ?", (riddle_id,))
        return riddle

    def delete_active_riddle(
        self,
        riddle_id: int,
        *,
        now: datetime | None = None,
    ) -> Riddle | None:
        """期限前のactive問題だけを原子的に削除し、削除前の値を返す。

        正解受付と同じ ``BEGIN IMMEDIATE`` の書き込みロックを使うため、
        同時にbriddleの正解が届いてもsolvedへ移ったレコードを後から
        取り消さず、期限到達後の問題も削除しない。
        """

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        current_time = _datetime_to_text(now or datetime.now(timezone.utc))
        with self._write_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM riddles
                WHERE id = ? AND status = 'active' AND deadline_at > ?
                """,
                (riddle_id, current_time),
            ).fetchone()
            if row is None:
                return None
            riddle = _riddle_from_row(row)
            cursor = connection.execute(
                """
                DELETE FROM riddles
                WHERE id = ? AND status = 'active' AND deadline_at > ?
                """,
                (riddle_id, current_time),
            )
            if cursor.rowcount != 1:
                return None
        return riddle

    def delete_finished_riddle(self, riddle_id: int) -> Riddle | None:
        """solved/expired問題だけを物理削除し、削除前の値を返す。"""

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        with self._write_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM riddles
                WHERE id = ? AND status IN ('solved', 'expired')
                """,
                (riddle_id,),
            ).fetchone()
            if row is None:
                return None
            riddle = _riddle_from_row(row)
            connection.execute("DELETE FROM riddles WHERE id = ?", (riddle_id,))
        return riddle

    def get_guild_settings(self, guild_id: int) -> GuildSettings:
        guild_id = _require_positive_integer(guild_id, "guild_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        if row is None:
            return GuildSettings(guild_id=guild_id)
        return _settings_from_row(row)

    def update_guild_settings(
        self,
        guild_id: int,
        *,
        allowed_channel_id: int | None | object = _UNSET,
        max_active: int | object = _UNSET,
        mention_winners: bool | object = _UNSET,
        answer_accept_emoji: str | object = _UNSET,
        ogiri_mvp_emoji: str | object = _UNSET,
        good_question_emoji: str | object = _UNSET,
    ) -> GuildSettings:
        """指定された設定項目だけ変更する。

        ``allowed_channel_id=None`` を明示するとチャンネル制限を解除する。
        """

        guild_id = _require_positive_integer(guild_id, "guild_id")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
            current = (
                _settings_from_row(row)
                if row is not None
                else GuildSettings(guild_id=guild_id)
            )

            next_channel = current.allowed_channel_id
            if allowed_channel_id is not _UNSET:
                if allowed_channel_id is not None:
                    allowed_channel_id = _require_positive_integer(
                        allowed_channel_id,
                        "allowed_channel_id",
                    )
                next_channel = allowed_channel_id

            next_max = current.max_active
            if max_active is not _UNSET:
                if (
                    isinstance(max_active, bool)
                    or not isinstance(max_active, int)
                    or max_active < 1
                ):
                    raise ValueError("max_active は1以上の整数で指定してください。")
                next_max = max_active

            next_mention = current.mention_winners
            if mention_winners is not _UNSET:
                if not isinstance(mention_winners, bool):
                    raise ValueError("mention_winners は真偽値で指定してください。")
                next_mention = mention_winners

            next_answer_accept_emoji = current.answer_accept_emoji
            if answer_accept_emoji is not _UNSET:
                next_answer_accept_emoji = _validate_emoji_setting(
                    answer_accept_emoji,
                    "answer_accept_emoji",
                )

            next_ogiri_mvp_emoji = current.ogiri_mvp_emoji
            if ogiri_mvp_emoji is not _UNSET:
                next_ogiri_mvp_emoji = _validate_emoji_setting(
                    ogiri_mvp_emoji,
                    "ogiri_mvp_emoji",
                )

            next_good_question_emoji = current.good_question_emoji
            if good_question_emoji is not _UNSET:
                next_good_question_emoji = _validate_emoji_setting(
                    good_question_emoji,
                    "good_question_emoji",
                )

            _require_distinct_evaluation_emojis(
                next_answer_accept_emoji,
                next_ogiri_mvp_emoji,
                next_good_question_emoji,
            )

            connection.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, allowed_channel_id, max_active, mention_winners,
                    answer_accept_emoji, ogiri_mvp_emoji, good_question_emoji
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    allowed_channel_id = excluded.allowed_channel_id,
                    max_active = excluded.max_active,
                    mention_winners = excluded.mention_winners,
                    answer_accept_emoji = excluded.answer_accept_emoji,
                    ogiri_mvp_emoji = excluded.ogiri_mvp_emoji,
                    good_question_emoji = excluded.good_question_emoji
                """,
                (
                    guild_id,
                    next_channel,
                    next_max,
                    int(next_mention),
                    next_answer_accept_emoji,
                    next_ogiri_mvp_emoji,
                    next_good_question_emoji,
                ),
            )
            updated = connection.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return _settings_from_row(updated)

    def get_user_preferences(
        self,
        guild_id: int,
        user_id: int,
    ) -> UserPreferences:
        """問題作成者の個人既定値を返す。未保存なら初期値を返す。"""

        guild_id = _require_positive_integer(guild_id, "guild_id")
        user_id = _require_positive_integer(user_id, "user_id")
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_preferences
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
        if row is None:
            return UserPreferences(guild_id=guild_id, user_id=user_id)
        return _user_preferences_from_row(row)

    def update_user_preferences(
        self,
        guild_id: int,
        user_id: int,
        *,
        public_answers: bool | object = _UNSET,
        answer_limit: int | None | object = _UNSET,
    ) -> UserPreferences:
        """指定された個人既定値だけを更新する。

        ``answer_limit=None`` を明示すると回答回数を無制限へ戻す。
        """

        guild_id = _require_positive_integer(guild_id, "guild_id")
        user_id = _require_positive_integer(user_id, "user_id")
        with self._write_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM user_preferences
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            current = (
                _user_preferences_from_row(row)
                if row is not None
                else UserPreferences(guild_id=guild_id, user_id=user_id)
            )

            next_public_answers = current.public_answers
            if public_answers is not _UNSET:
                if not isinstance(public_answers, bool):
                    raise TypeError("public_answers は真偽値で指定してください。")
                next_public_answers = public_answers

            next_answer_limit = current.answer_limit
            if answer_limit is not _UNSET:
                next_answer_limit = validate_answer_limit(answer_limit)

            connection.execute(
                """
                INSERT INTO user_preferences (
                    guild_id, user_id, public_answers, answer_limit
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id, user_id) DO UPDATE SET
                    public_answers = excluded.public_answers,
                    answer_limit = excluded.answer_limit
                """,
                (
                    guild_id,
                    user_id,
                    int(next_public_answers),
                    next_answer_limit,
                ),
            )
            updated = connection.execute(
                """
                SELECT * FROM user_preferences
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
        return _user_preferences_from_row(updated)

    def get_user_data_summary(self, guild_id: int, user_id: int) -> UserDataSummary:
        guild_id = _require_positive_integer(guild_id, "guild_id")
        user_id = _require_positive_integer(user_id, "user_id")
        with self._connect() as connection:
            created = connection.execute(
                """
                SELECT
                    COUNT(*) AS total,
                    COALESCE(SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END), 0)
                        AS active
                FROM riddles
                WHERE guild_id = ? AND creator_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()
            correct = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddle_winners AS winners
                JOIN riddles ON riddles.id = winners.riddle_id
                WHERE riddles.guild_id = ? AND winners.user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            first_wins = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddles
                WHERE guild_id = ? AND winner_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            answer_attempts = connection.execute(
                """
                SELECT COALESCE(SUM(attempts.attempt_count), 0) AS count
                FROM riddle_attempts AS attempts
                JOIN riddles ON riddles.id = attempts.riddle_id
                WHERE riddles.guild_id = ? AND attempts.user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            public_answer_messages = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddle_answer_messages AS messages
                JOIN riddles ON riddles.id = messages.riddle_id
                WHERE riddles.guild_id = ? AND messages.user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            has_preferences = (
                connection.execute(
                    """
                    SELECT 1 FROM user_preferences
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (guild_id, user_id),
                ).fetchone()
                is not None
            )
        return UserDataSummary(
            guild_id=guild_id,
            user_id=user_id,
            created_riddles=created["total"],
            active_created_riddles=created["active"],
            correct_riddles=correct,
            first_wins=first_wins,
            answer_attempts=answer_attempts,
            public_answer_messages=public_answer_messages,
            has_preferences=has_preferences,
        )

    def delete_user_data(self, guild_id: int, user_id: int) -> UserDataDeletion:
        """ユーザー作成問題・回答記録・個人設定を一括削除する。"""

        guild_id = _require_positive_integer(guild_id, "guild_id")
        user_id = _require_positive_integer(user_id, "user_id")
        with self._write_connection() as connection:
            deleted_riddles = connection.execute(
                """
                SELECT COUNT(*) AS count FROM riddles
                WHERE guild_id = ? AND creator_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            deleted_winner_entries = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddle_winners AS winners
                JOIN riddles ON riddles.id = winners.riddle_id
                WHERE riddles.guild_id = ? AND winners.user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            deleted_answer_attempts = connection.execute(
                """
                SELECT COALESCE(SUM(attempts.attempt_count), 0) AS count
                FROM riddle_attempts AS attempts
                JOIN riddles ON riddles.id = attempts.riddle_id
                WHERE riddles.guild_id = ? AND attempts.user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            deleted_public_answer_messages = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddle_answer_messages AS messages
                JOIN riddles ON riddles.id = messages.riddle_id
                WHERE riddles.guild_id = ? AND messages.user_id = ?
                """,
                (guild_id, user_id),
            ).fetchone()["count"]
            had_preferences = (
                connection.execute(
                    """
                    SELECT 1 FROM user_preferences
                    WHERE guild_id = ? AND user_id = ?
                    """,
                    (guild_id, user_id),
                ).fetchone()
                is not None
            )

            connection.execute(
                "DELETE FROM riddles WHERE guild_id = ? AND creator_id = ?",
                (guild_id, user_id),
            )
            connection.execute(
                """
                DELETE FROM riddle_winners
                WHERE user_id = ?
                  AND riddle_id IN (
                      SELECT id FROM riddles WHERE guild_id = ?
                  )
                """,
                (user_id, guild_id),
            )
            connection.execute(
                """
                DELETE FROM riddle_attempts
                WHERE user_id = ?
                  AND riddle_id IN (
                      SELECT id FROM riddles WHERE guild_id = ?
                  )
                """,
                (user_id, guild_id),
            )
            connection.execute(
                """
                DELETE FROM riddle_answer_messages
                WHERE user_id = ?
                  AND riddle_id IN (
                      SELECT id FROM riddles WHERE guild_id = ?
                  )
                """,
                (user_id, guild_id),
            )
            connection.execute(
                """
                DELETE FROM user_preferences
                WHERE guild_id = ? AND user_id = ?
                """,
                (guild_id, user_id),
            )
            cleared = connection.execute(
                """
                UPDATE riddles SET winner_id = NULL
                WHERE guild_id = ? AND winner_id = ?
                """,
                (guild_id, user_id),
            ).rowcount

        return UserDataDeletion(
            guild_id=guild_id,
            user_id=user_id,
            deleted_riddles=deleted_riddles,
            deleted_winner_entries=deleted_winner_entries,
            cleared_winner_references=cleared,
            deleted_answer_attempts=deleted_answer_attempts,
            deleted_public_answer_messages=deleted_public_answer_messages,
            deleted_preferences=had_preferences,
        )

    def delete_guild_data(self, guild_id: int) -> GuildDataDeletion:
        """Bot退出時にサーバー内の全問題・回答記録・設定を即時削除する。"""

        guild_id = _require_positive_integer(guild_id, "guild_id")
        with self._write_connection() as connection:
            deleted_riddles = connection.execute(
                "SELECT COUNT(*) AS count FROM riddles WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()["count"]
            deleted_winner_entries = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddle_winners
                WHERE riddle_id IN (
                    SELECT id FROM riddles WHERE guild_id = ?
                )
                """,
                (guild_id,),
            ).fetchone()["count"]
            deleted_answer_attempts = connection.execute(
                """
                SELECT COALESCE(SUM(attempts.attempt_count), 0) AS count
                FROM riddle_attempts AS attempts
                JOIN riddles ON riddles.id = attempts.riddle_id
                WHERE riddles.guild_id = ?
                """,
                (guild_id,),
            ).fetchone()["count"]
            deleted_public_answer_messages = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM riddle_answer_messages AS messages
                JOIN riddles ON riddles.id = messages.riddle_id
                WHERE riddles.guild_id = ?
                """,
                (guild_id,),
            ).fetchone()["count"]
            deleted_user_preferences = connection.execute(
                """
                SELECT COUNT(*) AS count FROM user_preferences
                WHERE guild_id = ?
                """,
                (guild_id,),
            ).fetchone()["count"]
            connection.execute("DELETE FROM riddles WHERE guild_id = ?", (guild_id,))
            settings_cursor = connection.execute(
                "DELETE FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
            connection.execute(
                "DELETE FROM user_preferences WHERE guild_id = ?",
                (guild_id,),
            )
        return GuildDataDeletion(
            guild_id=guild_id,
            deleted_riddles=deleted_riddles,
            deleted_winner_entries=deleted_winner_entries,
            deleted_settings=settings_cursor.rowcount > 0,
            deleted_answer_attempts=deleted_answer_attempts,
            deleted_public_answer_messages=deleted_public_answer_messages,
            deleted_user_preferences=deleted_user_preferences,
        )


# 短い名前を好む呼び出し側向けの公開別名。
Database = RiddleDatabase
