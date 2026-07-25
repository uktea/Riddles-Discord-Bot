"""なぞなぞとサーバー設定を保存するSQLiteリポジトリ。"""

from __future__ import annotations

import json
import os
import sqlite3
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
    validate_mode,
)

DEFAULT_MAX_ACTIVE = 10
DEFAULT_MENTION_WINNERS = True
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


class SubmissionStatus(str, Enum):
    """回答受付の結果。"""

    CORRECT = "correct"
    WRONG = "wrong"
    ALREADY_CORRECT = "already_correct"
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


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    status: SubmissionStatus
    riddle: Riddle | None
    winner_ids: tuple[int, ...] = ()

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


@dataclass(frozen=True, slots=True)
class UserDataSummary:
    guild_id: int
    user_id: int
    created_riddles: int
    active_created_riddles: int
    correct_riddles: int
    first_wins: int


@dataclass(frozen=True, slots=True)
class UserDataDeletion:
    guild_id: int
    user_id: int
    deleted_riddles: int
    deleted_winner_entries: int
    cleared_winner_references: int


@dataclass(frozen=True, slots=True)
class GuildDataDeletion:
    guild_id: int
    deleted_riddles: int
    deleted_winner_entries: int
    deleted_settings: bool


def _require_positive_integer(value: int, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} は正の整数で指定してください。")
    return value


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
    )


def _settings_from_row(row: sqlite3.Row) -> GuildSettings:
    return GuildSettings(
        guild_id=row["guild_id"],
        allowed_channel_id=row["allowed_channel_id"],
        max_active=row["max_active"],
        mention_winners=bool(row["mention_winners"]),
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
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_settings (
                    guild_id INTEGER PRIMARY KEY,
                    allowed_channel_id INTEGER,
                    max_active INTEGER NOT NULL DEFAULT 10
                        CHECK (max_active >= 1),
                    mention_winners INTEGER NOT NULL DEFAULT 1
                        CHECK (mention_winners IN (0, 1))
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
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS riddle_winners (
                    riddle_id INTEGER NOT NULL
                        REFERENCES riddles(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    answered_at TEXT NOT NULL,
                    PRIMARY KEY (riddle_id, user_id)
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
                """
            )

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
    ) -> Riddle:
        """問題を作成し、作成者単位の未終了問題数上限を原子的に守る。"""

        guild_id = _require_positive_integer(guild_id, "guild_id")
        channel_id = _require_positive_integer(channel_id, "channel_id")
        creator_id = _require_positive_integer(creator_id, "creator_id")
        if thread_id is not None:
            thread_id = _require_positive_integer(thread_id, "thread_id")
        if message_id is not None:
            message_id = _require_positive_integer(message_id, "message_id")
        normalized_mode = validate_mode(mode)
        if not isinstance(question, str) or not question.strip():
            raise ValueError("問題文を空にはできません。")
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
                        status, winner_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', NULL, ?)
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
        """設定または問題データが存在する全guild IDを返す。"""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT guild_id FROM guild_settings
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
                )

            # answer は比較中だけメモリに存在し、DBへは一切書き込まない。
            if not answer_matches(answer, riddle.normalized_answers):
                return SubmissionResult(
                    SubmissionStatus.WRONG,
                    riddle,
                    self._list_winner_ids_in_connection(connection, riddle.id),
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
        return SubmissionResult(status, _riddle_from_row(updated_row), winner_ids)

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

    def delete_active_riddle(self, riddle_id: int) -> Riddle | None:
        """active問題だけを原子的に物理削除し、削除前の値を返す。

        正解受付と同じ ``BEGIN IMMEDIATE`` の書き込みロックを使うため、
        同時にbriddleの正解が届いてもsolvedへ移ったレコードを後から
        取り消すことはない。
        """

        riddle_id = _require_positive_integer(riddle_id, "riddle_id")
        with self._write_connection() as connection:
            row = connection.execute(
                "SELECT * FROM riddles WHERE id = ? AND status = 'active'",
                (riddle_id,),
            ).fetchone()
            if row is None:
                return None
            riddle = _riddle_from_row(row)
            cursor = connection.execute(
                "DELETE FROM riddles WHERE id = ? AND status = 'active'",
                (riddle_id,),
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

            connection.execute(
                """
                INSERT INTO guild_settings (
                    guild_id, allowed_channel_id, max_active, mention_winners
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id) DO UPDATE SET
                    allowed_channel_id = excluded.allowed_channel_id,
                    max_active = excluded.max_active,
                    mention_winners = excluded.mention_winners
                """,
                (guild_id, next_channel, next_max, int(next_mention)),
            )
            updated = connection.execute(
                "SELECT * FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            ).fetchone()
        return _settings_from_row(updated)

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
        return UserDataSummary(
            guild_id=guild_id,
            user_id=user_id,
            created_riddles=created["total"],
            active_created_riddles=created["active"],
            correct_riddles=correct,
            first_wins=first_wins,
        )

    def delete_user_data(self, guild_id: int, user_id: int) -> UserDataDeletion:
        """ユーザー作成問題・正解者記録・winner参照を一括削除する。"""

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
        )

    def delete_guild_data(self, guild_id: int) -> GuildDataDeletion:
        """Bot退出時にサーバー内の全問題・正解者・設定を即時削除する。"""

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
            connection.execute("DELETE FROM riddles WHERE guild_id = ?", (guild_id,))
            settings_cursor = connection.execute(
                "DELETE FROM guild_settings WHERE guild_id = ?",
                (guild_id,),
            )
        return GuildDataDeletion(
            guild_id=guild_id,
            deleted_riddles=deleted_riddles,
            deleted_winner_entries=deleted_winner_entries,
            deleted_settings=settings_cursor.rowcount > 0,
        )


# 短い名前を好む呼び出し側向けの公開別名。
Database = RiddleDatabase
