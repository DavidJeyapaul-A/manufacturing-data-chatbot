"""Entity extraction for the deterministic template path.

Pulls the line, the date range, top-N, category and severity out of a question.
Everything here returns plain Python values that become BOUND PARAMETERS — no
extracted text is ever concatenated into SQL.

`now` is injectable throughout so the behaviour is testable without freezing
the clock.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta

# --------------------------------------------------------------------------- #
# Date ranges
# --------------------------------------------------------------------------- #

DEFAULT_LOOKBACK_DAYS = 30


@dataclass(frozen=True)
class DateRange:
    """Half-open interval [start, end). Exclusive end avoids the classic
    'lost the last day' bug with BETWEEN on timestamps."""

    start: datetime
    end: datetime
    label: str
    explicit: bool = True

    def as_params(self) -> list[datetime]:
        return [self.start, self.end]


_MONTHS = {
    name.lower(): num
    for num, name in enumerate(calendar.month_name)
    if name
} | {
    name.lower(): num
    for num, name in enumerate(calendar.month_abbr)
    if name
}

_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
    "fifteen": 15, "twenty": 20, "thirty": 30, "sixty": 60, "ninety": 90,
}

_DMY = "%d %b %Y"


def _midnight(d: date) -> datetime:
    return datetime(d.year, d.month, d.day)


def _fmt(start: datetime, end: datetime) -> str:
    """Label a half-open range using its inclusive last day."""
    last = (end - timedelta(seconds=1)).date()
    if start.date() == last:
        return start.strftime(_DMY)
    return f"{start.strftime(_DMY)} to {last.strftime(_DMY)}"


def _month_range(year: int, month: int) -> tuple[datetime, datetime]:
    start = datetime(year, month, 1)
    last_day = calendar.monthrange(year, month)[1]
    return start, datetime(year, month, last_day) + timedelta(days=1)


def _parse_count(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _parse_date_token(token: str) -> date | None:
    """Accept 2026-07-14, 14/07/2026, 14 July 2026, July 14 2026."""
    token = token.strip().strip(".,")
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %B %Y", "%d %b %Y",
                "%B %d %Y", "%b %d %Y", "%d %B", "%d %b"):
        try:
            parsed = datetime.strptime(token, fmt)
        except ValueError:
            continue
        if "%Y" not in fmt:  # bare "14 July" — assume the current year
            parsed = parsed.replace(year=date.today().year)
        return parsed.date()
    return None


def extract_date_range(question: str, now: datetime | None = None) -> DateRange:
    """Best-effort natural-language date range. Falls back to the last 30 days."""
    now = now or datetime.now()
    text = " ".join((question or "").lower().split())
    today = _midnight(now.date())
    tomorrow = today + timedelta(days=1)

    # -- explicit two-sided range ------------------------------------------
    span = re.search(
        r"(?:between|from)\s+(.+?)\s+(?:and|to|until|through|-)\s+"
        r"([0-9]{1,4}[-/ ][a-z0-9]+(?:[-/ ][0-9]{2,4})?)",
        text,
    )
    if span:
        first = _parse_date_token(span.group(1))
        second = _parse_date_token(span.group(2))
        if first and second:
            lo, hi = sorted((first, second))
            return DateRange(_midnight(lo), _midnight(hi) + timedelta(days=1),
                             _fmt(_midnight(lo), _midnight(hi) + timedelta(days=1)))

    # -- a single named day -------------------------------------------------
    single = re.search(
        r"\bon\s+([0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{1,2}[/-][0-9]{1,2}[/-][0-9]{2,4}"
        r"|[0-9]{1,2}\s+[a-z]+(?:\s+[0-9]{4})?)",
        text,
    )
    if single:
        day = _parse_date_token(single.group(1))
        if day:
            return DateRange(_midnight(day), _midnight(day) + timedelta(days=1),
                             _midnight(day).strftime(_DMY))

    bare_iso = re.search(r"\b([0-9]{4}-[0-9]{2}-[0-9]{2})\b", text)
    if bare_iso:
        day = _parse_date_token(bare_iso.group(1))
        if day:
            return DateRange(_midnight(day), _midnight(day) + timedelta(days=1),
                             _midnight(day).strftime(_DMY))

    # -- relative keywords --------------------------------------------------
    if re.search(r"\byesterday\b", text):
        start = today - timedelta(days=1)
        return DateRange(start, today, "yesterday (" + start.strftime(_DMY) + ")")

    if re.search(r"\b(today|so far today)\b", text):
        return DateRange(today, tomorrow, "today (" + today.strftime(_DMY) + ")")

    rolling = re.search(
        r"\b(?:last|past|previous|latest)\s+([0-9]+|[a-z]+)\s+(hour|day|week|month)s?\b",
        text,
    )
    if rolling:
        count = _parse_count(rolling.group(1))
        unit = rolling.group(2)
        if count:
            if unit == "hour":
                start = now - timedelta(hours=count)
                return DateRange(start, now, f"the last {count} hours")
            days = {"day": 1, "week": 7, "month": 30}[unit] * count
            start = today - timedelta(days=days - 1 if unit == "day" else days)
            return DateRange(start, tomorrow, f"the last {count} {unit}{'s' if count > 1 else ''}")

    if re.search(r"\b(last|previous)\s+week\b", text):
        this_week = today - timedelta(days=today.weekday())
        start = this_week - timedelta(days=7)
        return DateRange(start, this_week, "last week (" + _fmt(start, this_week) + ")")

    if re.search(r"\bthis\s+week\b", text):
        start = today - timedelta(days=today.weekday())
        return DateRange(start, tomorrow, "this week (" + _fmt(start, tomorrow) + ")")

    if re.search(r"\b(last|previous)\s+month\b", text):
        first_this = today.replace(day=1)
        prev_end = first_this
        prev_start = (first_this - timedelta(days=1)).replace(day=1)
        return DateRange(prev_start, prev_end,
                         prev_start.strftime("%B %Y"))

    if re.search(r"\bthis\s+month\b", text):
        start = today.replace(day=1)
        return DateRange(start, tomorrow, "this month (" + start.strftime("%B %Y") + " so far)")

    if re.search(r"\b(this year|year to date|ytd)\b", text):
        start = datetime(today.year, 1, 1)
        return DateRange(start, tomorrow, f"{today.year} so far")

    if re.search(r"\b(all time|overall|ever|in total|to date|altogether)\b", text):
        start = datetime(today.year - 5, 1, 1)
        return DateRange(start, tomorrow, "all recorded history")

    # -- "in July" / "in July 2026" ----------------------------------------
    named = re.search(r"\b(?:in|during|for)\s+([a-z]+)(?:\s+([0-9]{4}))?\b", text)
    if named and named.group(1) in _MONTHS:
        month = _MONTHS[named.group(1)]
        year = int(named.group(2)) if named.group(2) else today.year
        # "in December" asked in February means last December, not next.
        if not named.group(2) and month > today.month:
            year -= 1
        start, end = _month_range(year, month)
        return DateRange(start, end, start.strftime("%B %Y"))

    # -- nothing said: sensible default ------------------------------------
    start = today - timedelta(days=DEFAULT_LOOKBACK_DAYS)
    return DateRange(start, tomorrow, f"the last {DEFAULT_LOOKBACK_DAYS} days", explicit=False)


# --------------------------------------------------------------------------- #
# Other entities
# --------------------------------------------------------------------------- #

_LINE_RE = re.compile(r"\bline[\s_-]*([abc])\b", re.IGNORECASE)
_PER_LINE_RE = re.compile(r"\b(per|each|by|every|across)\s+lines?\b|\ball\s+lines\b|\blines\b",
                          re.IGNORECASE)
_TOPN_RE = re.compile(
    r"\b(?:top|worst|first|best|biggest|highest|most\s+frequent)\s+([0-9]+|[a-z]+)\b",
    re.IGNORECASE,
)
_PRODUCT_RE = re.compile(r"\b(prd[\s-]?10[0-5])\b", re.IGNORECASE)

SEVERITIES = ("critical", "warning", "info")

REJECT_CATEGORIES = {
    "dimensional": "reject_dimensional",
    "dimension": "reject_dimensional",
    "size": "reject_dimensional",
    "visual": "reject_visual",
    "cosmetic": "reject_visual",
    "appearance": "reject_visual",
    "other": "reject_other",
}


def extract_line(question: str) -> str | None:
    """'on line b' -> 'Line B'. None means 'no specific line'."""
    match = _LINE_RE.search(question or "")
    return f"Line {match.group(1).upper()}" if match else None


def wants_per_line(question: str) -> bool:
    """True when the question asks for a breakdown across lines."""
    if extract_line(question):
        return False
    return bool(_PER_LINE_RE.search(question or ""))


def extract_top_n(question: str, default: int = 5, maximum: int = 50) -> int:
    match = _TOPN_RE.search(question or "")
    if match:
        count = _parse_count(match.group(1))
        if count:
            return max(1, min(count, maximum))
    return default


def extract_severity(question: str) -> str | None:
    text = (question or "").lower()
    for severity in SEVERITIES:
        if re.search(rf"\b{severity}\b", text):
            return severity
    if re.search(r"\binformational\b", text):
        return "info"
    return None


def extract_reject_category(question: str) -> str | None:
    text = (question or "").lower()
    for word, category in REJECT_CATEGORIES.items():
        if re.search(rf"\b{word}\b", text):
            return category
    return None


def extract_product(question: str) -> str | None:
    match = _PRODUCT_RE.search(question or "")
    if not match:
        return None
    return "PRD-" + re.sub(r"\D", "", match.group(1))


__all__ = [
    "DateRange",
    "extract_date_range",
    "extract_line",
    "extract_product",
    "extract_reject_category",
    "extract_severity",
    "extract_top_n",
    "wants_per_line",
]
