"""时间解析、比较与可注入时钟。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import ValidationError


def parse_instant(value: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("时间不能为空")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValidationError("时间必须使用 ISO 8601 格式") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValidationError("时间必须包含时区")
    return result.astimezone(timezone.utc)


def canonical_instant(value: str) -> str:
    return parse_instant(value).isoformat().replace("+00:00", "Z")


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_date(value: str) -> str:
    """把 YYYY-MM-DD 或带时区的时刻归一化为 UTC 日期字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("日期不能为空")
    text = value.strip()
    if len(text) == 10:
        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError as exc:
            raise ValidationError("日期必须使用 YYYY-MM-DD 格式") from exc
        return text
    return parse_instant(text).date().isoformat()


@dataclass(frozen=True)
class Clock:
    fixed: str | None = None

    def now(self) -> str:
        return canonical_instant(self.fixed) if self.fixed else now_utc()

    def is_due(self, due_at: str) -> bool:
        return parse_instant(due_at) <= parse_instant(self.now())
