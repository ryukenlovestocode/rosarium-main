"""
Pure time helpers for Roselle — no Discord, no database, no I/O.

Rules this module follows:
- Only timezone-aware datetimes cross function boundaries.
- Timezones are IANA names resolved through the standard library's zoneinfo.
  Offsets and abbreviations are always *derived* for a specific moment, never
  stored or hard-coded, so DST is handled by the tz database itself.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError, available_timezones

UTC = dt_timezone.utc

# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class TimeParseError(ValueError):
    """Raised when free-form date/time text can't be understood."""


class NonexistentTimeError(ValueError):
    """The wall-clock time doesn't exist (skipped by a DST 'spring forward')."""


# --------------------------------------------------------------------------
# Timezone validation
# --------------------------------------------------------------------------

_CANONICAL: dict[str, str] | None = None
_EXCLUDED_PREFIXES = ("Etc/", "SystemV/", "posix/", "right/")


def _canonical_map() -> dict[str, str]:
    """lower-case name -> canonical IANA name.

    Bare abbreviations/legacy names (EST, EST5EDT, MST7MDT...) have no '/' and
    are deliberately excluded: they're ambiguous and not valid storage keys.
    """
    global _CANONICAL
    if _CANONICAL is None:
        _CANONICAL = {
            name.lower(): name
            for name in available_timezones()
            if (("/" in name and not name.startswith(_EXCLUDED_PREFIXES)) or name == "UTC")
        }
    return _CANONICAL


def canonical_timezone_name(name: str) -> str | None:
    """Return the canonical IANA spelling of ``name``, or None if invalid."""
    if not isinstance(name, str):
        return None
    key = name.strip().replace(" ", "_").lower()
    return _canonical_map().get(key)


def is_valid_timezone(name: str) -> bool:
    return canonical_timezone_name(name) is not None


def get_zone(name: str) -> ZoneInfo | None:
    canonical = canonical_timezone_name(name)
    if canonical is None:
        return None
    try:
        return ZoneInfo(canonical)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return None


def require_zone(name: str) -> ZoneInfo:
    zone = get_zone(name)
    if zone is None:
        raise ValueError(f"Unknown timezone: {name!r}")
    return zone


# --------------------------------------------------------------------------
# Current time / conversion
# --------------------------------------------------------------------------


def get_current_time(timezone: str | ZoneInfo) -> datetime:
    """Current moment, as a timezone-aware datetime in ``timezone``."""
    zone = require_zone(timezone) if isinstance(timezone, str) else timezone
    return datetime.now(UTC).astimezone(zone)


def resolve_local(naive: datetime, zone: ZoneInfo) -> tuple[datetime, bool]:
    """Attach ``zone`` to a naive wall-clock time, DST-safely.

    Returns ``(aware_datetime, was_ambiguous)``.

    - Nonexistent time (clock jumped forward over it): raises NonexistentTimeError.
    - Ambiguous time (clock fell back, so it happened twice): returns the
      *first* occurrence and ``was_ambiguous=True``.
    """
    if naive.tzinfo is not None:
        raise ValueError("resolve_local expects a naive datetime")
    first = naive.replace(tzinfo=zone, fold=0)
    second = naive.replace(tzinfo=zone, fold=1)
    if first.utcoffset() == second.utcoffset():
        return first, False
    # Offsets differ for the two folds: it's either a gap or an overlap.
    round_trip = first.astimezone(UTC).astimezone(zone).replace(tzinfo=None)
    if round_trip != naive:
        raise NonexistentTimeError(naive.isoformat())
    return first, True


def convert_time(
    moment: datetime,
    from_timezone: str | ZoneInfo,
    to_timezone: str | ZoneInfo,
) -> datetime:
    """Convert ``moment`` to ``to_timezone``.

    A naive ``moment`` is interpreted as wall-clock time in ``from_timezone``;
    an aware one is converted as-is (``from_timezone`` is then only a fallback
    that is ignored).
    """
    src = require_zone(from_timezone) if isinstance(from_timezone, str) else from_timezone
    dst = require_zone(to_timezone) if isinstance(to_timezone, str) else to_timezone
    if moment.tzinfo is None:
        moment, _ = resolve_local(moment, src)
    return moment.astimezone(dst)


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------

_STYLE_CODES = {
    "short_time": "t",
    "long_time": "T",
    "short_date": "d",
    "long_date": "D",
    "short_datetime": "f",
    "full": "F",
    "relative": "R",
}


def format_discord_timestamp(moment: datetime, style: str = "full") -> str:
    """Return Discord's native ``<t:UNIX:STYLE>`` markup.

    Discord renders it in each *viewer's* own timezone, so one message is
    correct for everybody. ``style`` may be a friendly name (see _STYLE_CODES)
    or a raw Discord code (t, T, d, D, f, F, R).
    """
    if moment.tzinfo is None:
        raise ValueError("format_discord_timestamp requires a timezone-aware datetime")
    code = _STYLE_CODES.get(style, style)
    if code not in set("tTdDfFR"):
        raise ValueError(f"Unknown Discord timestamp style: {style!r}")
    return f"<t:{int(moment.timestamp())}:{code}>"


def format_clock(moment: datetime) -> str:
    """'9:31 PM' (no leading zero, portable across platforms)."""
    return moment.strftime("%I:%M %p").lstrip("0")


def format_long_date(moment: datetime) -> str:
    """'Monday, October 26'."""
    return f"{moment:%A}, {moment:%B} {moment.day}"


def format_offset(moment: datetime) -> str:
    """'UTC+5:30', 'UTC-4', 'UTC+0' — derived for this exact moment."""
    offset = moment.utcoffset() or timedelta(0)
    total = int(offset.total_seconds() // 60)
    sign = "+" if total >= 0 else "-"
    hours, minutes = divmod(abs(total), 60)
    return f"UTC{sign}{hours}" + (f":{minutes:02d}" if minutes else "")


def day_shift_label(moment: datetime, reference: datetime) -> str:
    """'' / 'tomorrow' / 'yesterday' / '+2 days' of ``moment`` vs ``reference``'s local date."""
    diff = (moment.date() - reference.date()).days
    if diff == 0:
        return ""
    if diff == 1:
        return "tomorrow"
    if diff == -1:
        return "yesterday"
    return f"{diff:+d} days"


def event_announcement(title: str, moment: datetime, *, emoji: str = "🎬") -> str:
    """Reusable announcement text for any future event system.

    Every viewer sees the time in their own timezone — no per-zone maths.
    """
    return (
        f"{emoji} **{title}**\n\n"
        f"🗓️ {format_discord_timestamp(moment, 'full')}\n"
        f"🕐 {format_discord_timestamp(moment, 'relative')}"
    )


# --------------------------------------------------------------------------
# Free-form parsing (deliberately small and predictable — no NLP dependency)
# --------------------------------------------------------------------------

_MERIDIEM = r"(a\.?m\.?|p\.?m\.?)"
_TIME_COLON = re.compile(rf"(?<![\d:])(\d{{1,2}}):(\d{{2}})\s*{_MERIDIEM}?(?![\w:])", re.I)
_TIME_AMPM = re.compile(rf"(?<![\d:])(\d{{1,2}})\s*{_MERIDIEM}(?!\w)", re.I)
_TIME_WORD = re.compile(r"\b(noon|midnight)\b", re.I)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_REL_TOMORROW = re.compile(r"\b(tomorrow|tmrw|tmr)\b", re.I)
_REL_TODAY = re.compile(r"\b(today|tonight)\b", re.I)
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_REL_WEEKDAY = re.compile(r"\b(?:(next|this)\s+)?(" + "|".join(_WEEKDAYS) + r")\b", re.I)
_LEADING_AT = re.compile(r"(?:\bat|@)\s*$", re.I)


@dataclass(frozen=True)
class ParsedWhen:
    hour: int
    minute: int
    day: date
    explicit_day: bool
    title: str

    def as_naive(self) -> datetime:
        return datetime.combine(self.day, dtime(self.hour, self.minute))


def _to_24h(hour: int, minute: int, meridiem: str | None) -> tuple[int, int]:
    if not 0 <= minute <= 59:
        raise TimeParseError("Minutes must be between 00 and 59.")
    if meridiem:
        if not 1 <= hour <= 12:
            raise TimeParseError("With AM/PM the hour must be between 1 and 12.")
        hour = hour % 12 + (12 if meridiem.lower().startswith("p") else 0)
    elif not 0 <= hour <= 23:
        raise TimeParseError("Hours must be between 0 and 23.")
    return hour, minute


def parse_when(text: str, today: date) -> ParsedWhen:
    """Parse things like ``20:00``, ``8pm``, ``Meeting tomorrow at 8:30 pm``,
    ``friday 18:00``, ``2026-11-01 7pm``.

    ``today`` must be today's date *in the timezone the text is meant in*.
    Raises TimeParseError with a user-presentable message.
    """
    s = (text or "").strip()
    if not s:
        raise TimeParseError("I need a time, for example `8pm` or `20:00`.")

    spans: list[tuple[int, int]] = []

    # --- time of day ---
    m = _TIME_COLON.search(s)
    if m:
        hour, minute = _to_24h(int(m.group(1)), int(m.group(2)), m.group(3))
    else:
        m = _TIME_AMPM.search(s)
        if m:
            hour, minute = _to_24h(int(m.group(1)), 0, m.group(2))
        else:
            m = _TIME_WORD.search(s)
            if not m:
                raise TimeParseError(
                    "I couldn't find a time in that. Try something like `8pm`, `20:00` or `tomorrow 9:30am`."
                )
            hour, minute = (12, 0) if m.group(1).lower() == "noon" else (0, 0)
    start, end = m.span()
    prefix = _LEADING_AT.search(s[:start])
    if prefix:
        start = prefix.start()
    spans.append((start, end))

    # --- day ---
    day = today
    explicit = False
    iso = _DATE_ISO.search(s)
    if iso:
        try:
            day = date(int(iso.group(1)), int(iso.group(2)), int(iso.group(3)))
        except ValueError:
            raise TimeParseError("That date doesn't exist. Use the format `YYYY-MM-DD`.") from None
        explicit = True
        spans.append(iso.span())
    elif _REL_TOMORROW.search(s):
        day, explicit = today + timedelta(days=1), True
    elif _REL_TODAY.search(s):
        day, explicit = today, True
    else:
        wd = _REL_WEEKDAY.search(s)
        if wd:
            target = _WEEKDAYS.index(wd.group(2).lower())
            delta = (target - today.weekday()) % 7
            if delta == 0 and (wd.group(1) or "").lower() == "next":
                delta = 7
            day, explicit = today + timedelta(days=delta), True

    # --- title: the original text minus the time (and a leading "at") ---
    title = s
    for a, b in sorted(spans, reverse=True):
        title = title[:a] + " " + title[b:]
    title = re.sub(r"\s+", " ", title).strip(" -–:,")
    return ParsedWhen(hour, minute, day, explicit, title)