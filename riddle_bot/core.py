"""Discord に依存しない、なぞなぞの入力検証と正解判定。"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone

DEFAULT_TERM = timedelta(hours=24)
MIN_TERM = timedelta(minutes=1)
MAX_TERM = timedelta(days=30)
MAX_ANSWER_LIMIT = 100
VALID_MODES = frozenset({"briddle", "riddle"})

_TERM_PATTERN = re.compile(r"(?P<amount>\d+)(?P<unit>[mhd])", re.IGNORECASE)
_TERM_SECONDS = {
    "m": 60,
    "h": 60 * 60,
    "d": 24 * 60 * 60,
}


class ValidationError(ValueError):
    """利用者から受け取った値が仕様を満たさない場合の基底エラー。"""


class TermValidationError(ValidationError):
    """期限の指定が不正な場合のエラー。"""


class AnswerValidationError(ValidationError):
    """正解候補が不正な場合のエラー。"""


class ModeValidationError(ValidationError):
    """問題モードが不正な場合のエラー。"""


class AnswerLimitValidationError(ValidationError):
    """1ユーザーあたりの回答回数上限が不正な場合のエラー。"""


def parse_term(value: str | None) -> timedelta:
    """``30m``、``2h``、``1d`` 形式の期間を返す。

    ``None`` または空白だけの値は24時間として扱う。単位は大文字でも
    よく、指定可能な範囲は1分以上30日以下。
    """

    if value is None:
        return DEFAULT_TERM
    if not isinstance(value, str):
        raise TermValidationError("期限は 30m、2h、1d のように指定してください。")

    cleaned = value.strip()
    if not cleaned:
        return DEFAULT_TERM

    match = _TERM_PATTERN.fullmatch(cleaned)
    if match is None:
        raise TermValidationError("期限は整数と m/h/d の組み合わせで指定してください。")

    amount = int(match.group("amount"))
    seconds = amount * _TERM_SECONDS[match.group("unit").lower()]
    if not MIN_TERM.total_seconds() <= seconds <= MAX_TERM.total_seconds():
        raise TermValidationError("期限は1分以上30日以下で指定してください。")
    return timedelta(seconds=seconds)


def as_utc(value: datetime) -> datetime:
    """日時をUTCのaware datetimeへ変換する。

    SQLiteへの保存値を一意にするため、naive datetimeはUTCとして扱う。
    """

    if not isinstance(value, datetime):
        raise TypeError("日時には datetime を指定してください。")
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def deadline_from_term(
    term: str | None,
    *,
    now: datetime | None = None,
) -> datetime:
    """基準時刻に期限指定を加えたUTC日時を返す。"""

    base = as_utc(now) if now is not None else datetime.now(timezone.utc)
    return base + parse_term(term)


def normalize_answer(value: str) -> str:
    """回答をNFKC化し、全空白を除去してcasefoldする。

    この関数の戻り値だけを比較に使い、利用者が送った回答本文そのものは
    永続化しない。
    """

    if not isinstance(value, str):
        raise AnswerValidationError("回答は文字列で指定してください。")
    normalized = unicodedata.normalize("NFKC", value)
    without_whitespace = "".join(
        character for character in normalized if not character.isspace()
    )
    return without_whitespace.casefold()


def prepare_answers(answer_display: str) -> tuple[str, ...]:
    """表示用の正解を ``|`` で分け、照合用候補へ変換する。

    空の候補は入力ミスとして拒否する。同じ表記へ正規化される候補は、
    入力順を保ったまま一つにまとめる。
    """

    if not isinstance(answer_display, str) or not answer_display.strip():
        raise AnswerValidationError("正解を1件以上指定してください。")

    normalized_answers: list[str] = []
    seen: set[str] = set()
    for candidate in answer_display.split("|"):
        normalized = normalize_answer(candidate)
        if not normalized:
            raise AnswerValidationError("正解候補を空にはできません。")
        if normalized not in seen:
            seen.add(normalized)
            normalized_answers.append(normalized)
    return tuple(normalized_answers)


def answer_matches(submission: str, normalized_answers: Iterable[str]) -> bool:
    """回答が正規化済み候補のいずれかと完全一致するか返す。"""

    normalized_submission = normalize_answer(submission)
    return any(
        normalized_submission == normalized_candidate
        for normalized_candidate in normalized_answers
    )


def validate_mode(mode: str) -> str:
    """問題モードを検証し、小文字の値を返す。"""

    if not isinstance(mode, str) or mode.casefold() not in VALID_MODES:
        raise ModeValidationError("モードは briddle または riddle を指定してください。")
    return mode.casefold()


def validate_answer_limit(value: int | None) -> int | None:
    """保存用の回答上限を検証する。

    ``None`` は無制限を表す。Discordコマンドなど外部入力で ``0`` を
    無制限として扱う変換は、保存層へ渡す前に呼び出し側で行う。
    """

    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise AnswerLimitValidationError(
            f"回答上限は1〜{MAX_ANSWER_LIMIT}の整数で指定してください。"
        )
    if not 1 <= value <= MAX_ANSWER_LIMIT:
        raise AnswerLimitValidationError(
            f"回答上限は1〜{MAX_ANSWER_LIMIT}、または無制限で指定してください。"
        )
    return value
