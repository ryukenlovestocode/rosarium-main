"""
Timezone service — everything between the Discord layer and the data.

- Storage: a single JSON file, written atomically, guarded by an asyncio lock.
- Only IANA identifiers are stored (never abbreviations or fixed offsets).
- Search/autocomplete uses zone.tab / iso3166.tab from the system or the
  `tzdata` package when present, plus a small curated alias table, so there is
  no extra dependency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from pathlib import Path
from typing import Iterable
from zoneinfo import TZPATH

from utils.time import (
    canonical_timezone_name,
    format_offset,
    get_current_time,
    require_zone,
)

log = logging.getLogger("rosarium.timezone")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class TimezoneError(Exception):
    """Base class for errors the cog turns into friendly messages."""


class InvalidTimezone(TimezoneError):
    pass


class StorageError(TimezoneError):
    pass


# --------------------------------------------------------------------------
# Metadata for friendly search / display
# --------------------------------------------------------------------------

# zone -> (ISO country code | None, friendly place name)
ZONE_META: dict[str, tuple[str | None, str]] = {
    "Asia/Kolkata": ("IN", "India"),
    "Asia/Kathmandu": ("NP", "Nepal"),
    "Asia/Colombo": ("LK", "Sri Lanka"),
    "Asia/Dhaka": ("BD", "Bangladesh"),
    "Asia/Karachi": ("PK", "Pakistan"),
    "Asia/Dubai": ("AE", "UAE"),
    "Asia/Bangkok": ("TH", "Thailand"),
    "Asia/Jakarta": ("ID", "Indonesia"),
    "Asia/Singapore": ("SG", "Singapore"),
    "Asia/Manila": ("PH", "Philippines"),
    "Asia/Shanghai": ("CN", "China"),
    "Asia/Hong_Kong": ("HK", "Hong Kong"),
    "Asia/Seoul": ("KR", "South Korea"),
    "Asia/Tokyo": ("JP", "Japan"),
    "Europe/London": ("GB", "United Kingdom"),
    "Europe/Berlin": ("DE", "Germany"),
    "Europe/Paris": ("FR", "France"),
    "Europe/Madrid": ("ES", "Spain"),
    "Europe/Rome": ("IT", "Italy"),
    "Europe/Moscow": ("RU", "Moscow"),
    "Europe/Istanbul": ("TR", "Turkey"),
    "America/New_York": ("US", "New York"),
    "America/Chicago": ("US", "Chicago"),
    "America/Denver": ("US", "Denver"),
    "America/Los_Angeles": ("US", "Los Angeles"),
    "America/Toronto": ("CA", "Toronto"),
    "America/Sao_Paulo": ("BR", "São Paulo"),
    "Australia/Sydney": ("AU", "Sydney"),
    "Australia/Perth": ("AU", "Perth"),
    "Pacific/Auckland": ("NZ", "New Zealand"),
    "Africa/Lagos": ("NG", "Nigeria"),
    "Africa/Cairo": ("EG", "Egypt"),
    "Africa/Johannesburg": ("ZA", "South Africa"),
    "UTC": (None, "UTC"),
}

# Search-only aliases (never stored). Abbreviations are fine as *search terms*
# because the user then picks a concrete IANA zone.
ALIASES: dict[str, tuple[str, ...]] = {
    "ist": ("Asia/Kolkata",),
    "india": ("Asia/Kolkata",),
    "uk": ("Europe/London",),
    "gb": ("Europe/London",),
    "britain": ("Europe/London",),
    "england": ("Europe/London",),
    "bst": ("Europe/London",),
    "gmt": ("Europe/London", "UTC"),
    "utc": ("UTC",),
    "est": ("America/New_York",),
    "edt": ("America/New_York",),
    "eastern": ("America/New_York",),
    "cst": ("America/Chicago",),
    "central": ("America/Chicago",),
    "mst": ("America/Denver",),
    "mountain": ("America/Denver",),
    "pst": ("America/Los_Angeles",),
    "pdt": ("America/Los_Angeles",),
    "pacific": ("America/Los_Angeles",),
    "cet": ("Europe/Berlin",),
    "cest": ("Europe/Berlin",),
    "jst": ("Asia/Tokyo",),
    "kst": ("Asia/Seoul",),
    "pkt": ("Asia/Karachi",),
    "aest": ("Australia/Sydney",),
    "nz": ("Pacific/Auckland",),
    "uae": ("Asia/Dubai",),
    "usa": ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"),
    "us": ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles"),
}

POPULAR: tuple[str, ...] = (
    "Asia/Kolkata", "America/New_York", "Europe/London", "America/Los_Angeles",
    "Europe/Berlin", "Asia/Tokyo", "Australia/Sydney", "Asia/Dubai",
    "America/Chicago", "Asia/Singapore", "Asia/Karachi", "UTC",
)


def flag_emoji(country_code: str | None) -> str:
    if not country_code or len(country_code) != 2 or not country_code.isalpha():
        return "🌐"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in country_code.upper())


def _read_tz_table(filename: str) -> str | None:
    for base in TZPATH:
        path = Path(base) / filename
        if path.is_file():
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                continue
    try:  # pip-installed tzdata fallback (Windows / slim containers)
        from importlib import resources

        return (resources.files("tzdata.zoneinfo") / filename).read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001 - optional data, any failure just means "no table"
        return None


@dataclass(frozen=True)
class ZoneEntry:
    zone: str
    city: str
    place: str
    country: str
    cc: str | None

    @property
    def label(self) -> str:
        text = f"{flag_emoji(self.cc)} {self.place} — {self.zone}" if self.place != self.zone else f"🌐 {self.zone}"
        return text[:100]


def _city(zone: str) -> str:
    return zone.split("/")[-1].replace("_", " ")


_INDEX: dict[str, ZoneEntry] | None = None


def _build_index() -> dict[str, ZoneEntry]:
    country_names: dict[str, str] = {}
    table = _read_tz_table("iso3166.tab")
    if table:
        for line in table.splitlines():
            if line and not line.startswith("#"):
                parts = line.split("\t")
                if len(parts) >= 2:
                    country_names[parts[0]] = parts[1]

    zone_country: dict[str, str] = {}
    table = _read_tz_table("zone.tab")
    if table:
        for line in table.splitlines():
            if line and not line.startswith("#"):
                parts = line.split("\t")
                if len(parts) >= 3:
                    zone_country[parts[2]] = parts[0]

    # zone.tab lists canonical zones only, which keeps legacy aliases
    # (Asia/Calcutta, US/Eastern...) out of search. Without it, use every
    # valid zone.
    candidates: set[str] = set(zone_country) | set(ZONE_META)
    if not zone_country:
        from utils.time import _canonical_map  # same package, internal helper

        candidates |= set(_canonical_map().values())

    index: dict[str, ZoneEntry] = {}
    for zone in candidates:
        canonical = canonical_timezone_name(zone)
        if canonical is None:
            continue
        meta = ZONE_META.get(canonical)
        cc = meta[0] if meta else zone_country.get(canonical)
        city = _city(canonical)
        place = meta[1] if meta else city
        country = country_names.get(cc or "", meta[1] if meta else "")
        index[canonical] = ZoneEntry(canonical, city, place, country, cc)
    return index


def zone_index() -> dict[str, ZoneEntry]:
    global _INDEX
    if _INDEX is None:
        _INDEX = _build_index()
    return _INDEX


def search_timezones(query: str, limit: int = 25) -> list[ZoneEntry]:
    """Human-friendly search: 'india', 'new york', 'tokyo', 'Asia/Kol', 'pst'."""
    index = zone_index()
    q = (query or "").strip().lower().replace("_", " ")

    if not q:
        return [index[z] for z in POPULAR if z in index][:limit]

    scored: dict[str, tuple[int, int, str]] = {}

    def offer(zone: str, score: int) -> None:
        if zone not in index:
            return
        current = scored.get(zone)
        curated = 0 if zone in ZONE_META else 1
        if current is None or score < current[0]:
            scored[zone] = (score, curated, zone)

    for zone in ALIASES.get(q, ()):
        offer(zone, 0)

    for zone, entry in index.items():
        fields = (
            zone.lower().replace("_", " "),
            entry.city.lower(),
            entry.place.lower(),
            entry.country.lower(),
        )
        if q in (fields[0], fields[1], fields[2]):
            offer(zone, 1)
        elif q == fields[3] or any(f.startswith(q) for f in fields) or any(
            part.startswith(q) for f in fields for part in f.split()
        ):
            offer(zone, 2)
        elif any(q in f for f in fields):
            offer(zone, 3)

    return [index[z] for _, _, z in sorted(scored.values())[:limit]]


def resolve_timezone_input(text: str) -> tuple[str | None, list[ZoneEntry]]:
    """Turn user input into one IANA zone if that's unambiguous.

    Returns ``(zone, suggestions)``: ``zone`` is set when the input is a valid
    IANA name or a search that has exactly one sensible answer; otherwise it is
    None and ``suggestions`` holds the best candidates.
    """
    canonical = canonical_timezone_name(text)
    if canonical:
        return canonical, []
    matches = search_timezones(text, limit=8)
    q = (text or "").strip().lower().replace("_", " ")
    if len(matches) == 1:
        return matches[0].zone, []
    if q in ALIASES and len(ALIASES[q]) == 1 and matches:
        return matches[0].zone, []
    exact = [m for m in matches if q in (m.city.lower(), m.place.lower())]
    if len(exact) == 1:
        return exact[0].zone, []
    return None, matches[:5]


_ZONE_CLAUSE = re.compile(r"(?:^|\s)in\s+(.+?)\s*$", re.IGNORECASE)


def split_zone_clause(text: str | None) -> tuple[str, str | None]:
    """Split ``"8pm in Tokyo"`` into ``("8pm", "Tokyo")``.

    Lets one free-text argument carry both a time and the zone it is meant in,
    which works the same for slash and prefix commands.
    """
    text = (text or "").strip()
    match = _ZONE_CLAUSE.search(text)
    if not match:
        return text, None
    return text[: match.start()].strip(), match.group(1).strip()


# --------------------------------------------------------------------------
# Formatting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class FormattedZone:
    zone: str
    flag: str
    place: str
    abbr: str
    offset: str

    @property
    def label(self) -> str:
        """'🇮🇳 Asia/Kolkata'"""
        return f"{self.flag} {self.zone}"

    @property
    def friendly(self) -> str:
        """'🇮🇳 India'"""
        return f"{self.flag} {self.place}"

    @property
    def abbr_offset(self) -> str:
        """'IST · UTC+5:30' (abbreviation dropped if it's just a numeric offset)."""
        if self.abbr and not self.abbr[0] in "+-":
            return f"{self.abbr} · {self.offset}"
        return self.offset


def format_timezone(timezone: str, at: datetime | None = None) -> FormattedZone:
    """Display info for ``timezone`` at moment ``at`` (default: now).

    Abbreviation and offset are derived from the tz database for that moment,
    so New York reads EST in winter and EDT in summer automatically.
    """
    zone = require_zone(timezone)
    moment = at.astimezone(zone) if at else get_current_time(zone)
    entry = zone_index().get(zone.key) if zone.key else None
    meta = ZONE_META.get(zone.key or "")
    cc = (entry.cc if entry else None) or (meta[0] if meta else None)
    place = entry.place if entry else _city(zone.key or timezone)
    return FormattedZone(zone.key or timezone, flag_emoji(cc), place, moment.tzname() or "", format_offset(moment))


# --------------------------------------------------------------------------
# Grouping / pagination helpers (pure, so they're unit-testable)
# --------------------------------------------------------------------------


def group_by_timezone(mapping: dict[int, str]) -> dict[str, list[int]]:
    groups: dict[str, list[int]] = defaultdict(list)
    for user_id, zone in mapping.items():
        groups[zone].append(user_id)
    return dict(groups)


def paginate_blocks(blocks: Iterable[str], max_chars: int = 3500, max_blocks: int = 8) -> list[str]:
    """Pack text blocks into pages that fit Discord's embed limits."""
    pages: list[str] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        block = block[: max_chars - 10]
        extra = len(block) + (2 if current else 0)
        if current and (size + extra > max_chars or len(current) >= max_blocks):
            pages.append("\n\n".join(current))
            current, size, extra = [], 0, len(block)
        current.append(block)
        size += extra
    if current:
        pages.append("\n\n".join(current))
    return pages


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class TimezoneService:
    """JSON-file-backed user timezone store.

    File shape::

        {"<discord user id>": {"timezone": "Asia/Kolkata",
                               "created_at": "...Z", "updated_at": "...Z"}}
    """

    def __init__(self, path: str | os.PathLike[str]):
        self._path = Path(path)
        self._lock = asyncio.Lock()
        self._data: dict[str, dict[str, str]] | None = None

    # -- internal ----------------------------------------------------------

    @staticmethod
    def _now() -> str:
        return datetime.now(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _load(self) -> dict[str, dict[str, str]]:
        if self._data is not None:
            return self._data
        if not self._path.exists():
            self._data = {}
            return self._data
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("top level is not an object")
            self._data = raw
        except (OSError, ValueError):
            # Never silently discard user data: park the bad file and start clean.
            backup = self._path.with_suffix(f".corrupt-{int(datetime.now().timestamp())}")
            log.exception("Timezone file unreadable; moving it to %s", backup)
            try:
                os.replace(self._path, backup)
            except OSError:
                log.exception("Could not back up corrupt timezone file")
            self._data = {}
        return self._data

    def _save(self) -> None:
        assert self._data is not None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self._path)  # atomic: no half-written file on crash
        except OSError as exc:
            log.exception("Failed to write timezone file")
            raise StorageError("Could not save timezone data") from exc

    # -- public API --------------------------------------------------------

    async def get_user_timezone(self, user_id: int) -> str | None:
        async with self._lock:
            record = self._load().get(str(user_id))
            return record["timezone"] if record else None

    async def set_user_timezone(self, user_id: int, timezone: str) -> str:
        """Validate, canonicalise and store. Returns the canonical IANA name."""
        canonical = canonical_timezone_name(timezone)
        if canonical is None:
            raise InvalidTimezone(timezone)
        async with self._lock:
            data = self._load()
            key = str(user_id)
            previous = data.get(key)
            now = self._now()
            data[key] = {
                "timezone": canonical,
                "created_at": previous["created_at"] if previous else now,
                "updated_at": now,
            }
            try:
                self._save()
            except StorageError:
                if previous is None:
                    data.pop(key, None)
                else:
                    data[key] = previous
                raise
        return canonical

    async def remove_user_timezone(self, user_id: int) -> bool:
        async with self._lock:
            data = self._load()
            previous = data.pop(str(user_id), None)
            if previous is None:
                return False
            try:
                self._save()
            except StorageError:
                data[str(user_id)] = previous
                raise
            return True

    async def all_user_timezones(self) -> dict[int, str]:
        async with self._lock:
            out: dict[int, str] = {}
            for key, record in self._load().items():
                try:
                    out[int(key)] = record["timezone"]
                except (ValueError, KeyError, TypeError):
                    continue
            return out