"""
Moderation cog — kick/ban/timeout, warnings, bulk-delete, channel
lock/slowmode, role management, and a mod-only snipe/editsnipe pair.

Ported from another bot into Rosarium's house style:
  - Uses config.EMBED_COLOR / config.EMBED_COLOR_DARK instead of a grab
    bag of discord.Color values, and config.FOOTER_TEXT for the footer,
    so these embeds read as part of the same bot as general.py/utility.py.
  - Role gating is config-driven instead of hardcoded IDs from the other
    server: set MOD_ROLE_ID and TRIAL_MOD_ROLE_ID in .env. Until those are
    set, only bot owners (config.OWNER_IDS, wired in bot.py) can use
    anything in this cog.
  - Commands are hybrid (slash + prefix) to match the rest of the bot,
    except `clear`/`purge`, which stays prefix-only because its argument
    is a free-form mini-syntax ("bots" / "user @x" / "contains word" / a
    number) that doesn't map cleanly onto a single slash option, and
    `massrole`, which takes a variable number of roles.

Note on snipe/editsnipe: this cog owns both commands now. They used to
also exist in utility.py (a rolling per-channel cache, open to everyone);
that version has been removed so there's a single snipe implementation.
This one is a staff-facing investigation tool, not a public toy, and it
is built so a spammer can't bury evidence by deleting a pile of messages:
  - Every channel keeps a rolling history (SNIPE_DEPTH entries for
    SNIPE_TTL) of deleted messages and, separately, edited ones — not just
    the single most recent.
  - Messages removed by a bulk delete (`clear`/`purge`) are kept too,
    tagged as purged. Attachments, stickers and reply targets are kept.
  - `snipe` / `editsnipe` take a small mini-syntax in one argument:
        $s                       the latest
        $s 3 | $s 2-6            the 3rd most recent | a range (as a list)
        $s 15m                   only the last 15 minutes (s/m/h/d)
        $s contains <word>       text/filename search (always goes last)
        $s user <@user|id>       everything one person deleted
        $s files | $s purged     only attachments | only purged messages
        $s in <#channel> | all   another channel | the whole server
        $s list | top | export | clear | help
    Filters stack (`$s user @x files last 1h`). Results open in a browser
    with Newer/Older/First/Last buttons and a List/Detail toggle. "#N" in
    any result is its serial number in the cache, i.e. `$s N` reopens it.
  - Staff only ever see channels they could view themselves, so snipe
    can't be used to read a private channel.
  - Command invocations in SNIPE_IGNORED_COMMANDS are never recorded. This
    matters for `confess` in fun.py: it deletes the invoking message right
    away, and without this the snipe cache would unmask the confessor.
  - In-memory only, same as AFK in utility.py: it doesn't need to survive
    a restart. Only messages Discord's message cache still holds (i.e. the
    bot saw them arrive) can be recorded.

Note on warnings: also in-memory. If you want warnings to survive a
restart, swap `mod_warnings` for a storage.JSONStore instance (see
storage.py) — the interface is small enough that nothing else here
would need to change.
"""

import asyncio
import io
import math
import re
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

# ---------- SNIPE SETTINGS ----------

SNIPE_DEPTH = 100                    # entries remembered per channel (deleted and edited, separately)
SNIPE_TTL = timedelta(hours=24)      # entries older than this are forgotten
SNIPE_PER_PAGE = 8                   # rows per page in list mode
SNIPE_VIEW_TIMEOUT = 120.0           # seconds the browser buttons stay live
SNIPE_BURST_COUNT = 5                # this many deletions/edits ...
SNIPE_BURST_WINDOW = 120             # ... within this many seconds = flagged in `top`
# Commands whose invoking message must never be recorded. `confess` deletes
# the user's message immediately to stay anonymous; `clear` deletes the
# moderator's own command. (Aliases resolve to these names automatically.)
SNIPE_IGNORED_COMMANDS = {"confess", "clear"}

# ---------- PERMISSION CHECKS ----------


def _member_role_ids(ctx: commands.Context) -> set[int]:
    if isinstance(ctx.author, discord.Member):
        return {r.id for r in ctx.author.roles}
    return set()


def has_any_mod_role():
    """Trial Mod, Mod, or bot owner."""

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        allowed = {rid for rid in (config.MOD_ROLE_ID, config.TRIAL_MOD_ROLE_ID) if rid}
        if allowed & _member_role_ids(ctx):
            return True
        raise commands.CheckFailure("You don't have the required role to use this command.")

    return commands.check(predicate)


def has_mod_role():
    """Mod or bot owner only — the harsher tier of commands."""

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        if config.MOD_ROLE_ID and config.MOD_ROLE_ID in _member_role_ids(ctx):
            return True
        raise commands.CheckFailure("This command requires the **Mod** role or higher.")

    return commands.check(predicate)


# ---------- SNIPE DATA ----------


@dataclass
class SnipeEntry:
    """One remembered deleted or edited message."""

    kind: str                       # "delete" | "edit"
    message_id: int
    channel_id: int
    guild_id: int
    author_id: int
    author_name: str
    avatar_url: str
    content: str                    # the deleted text — or, for an edit, the NEW text
    before: str = ""                # edits only: the old text
    attachments: list = field(default_factory=list)   # [(filename, url, content_type)]
    stickers: list = field(default_factory=list)      # [sticker name]
    reply_to: Optional[str] = None
    jump_url: str = ""
    sent_at: datetime = field(default_factory=discord.utils.utcnow)   # when the message was posted
    time: datetime = field(default_factory=discord.utils.utcnow)      # when it was deleted / edited
    purged: bool = False            # removed by a bulk delete rather than individually


# Newest entry first: index 0 is "#1". appendleft + maxlen drops the oldest.
sniped_messages: dict[int, deque] = defaultdict(lambda: deque(maxlen=SNIPE_DEPTH))
edited_messages: dict[int, deque] = defaultdict(lambda: deque(maxlen=SNIPE_DEPTH))


def _prune(store: dict) -> None:
    """Forget entries older than SNIPE_TTL (and empty channels)."""
    cutoff = discord.utils.utcnow() - SNIPE_TTL
    for cid in list(store):
        dq = store[cid]
        while dq and dq[-1].time < cutoff:
            dq.pop()
        if not dq:
            del store[cid]


def _snapshot_delete(message: discord.Message, *, purged: bool = False) -> SnipeEntry:
    reply_to = None
    ref = message.reference
    if ref is not None:
        resolved = ref.resolved
        if isinstance(resolved, discord.Message):
            reply_to = f"{resolved.author.mention} — [jump]({resolved.jump_url})"
        elif ref.message_id:
            reply_to = f"a message (`{ref.message_id}`)"

    return SnipeEntry(
        kind="delete",
        message_id=message.id,
        channel_id=message.channel.id,
        guild_id=message.guild.id,
        author_id=message.author.id,
        author_name=str(message.author),
        avatar_url=message.author.display_avatar.url,
        content=message.content or "",
        attachments=[(a.filename, a.url, a.content_type or "") for a in message.attachments],
        stickers=[s.name for s in message.stickers],
        reply_to=reply_to,
        jump_url=message.jump_url,
        sent_at=message.created_at,
        purged=purged,
    )


def _snapshot_edit(before: discord.Message, after: discord.Message) -> SnipeEntry:
    return SnipeEntry(
        kind="edit",
        message_id=after.id,
        channel_id=after.channel.id,
        guild_id=after.guild.id,
        author_id=before.author.id,
        author_name=str(before.author),
        avatar_url=before.author.display_avatar.url,
        content=after.content or "",
        before=before.content or "",
        jump_url=after.jump_url,
        sent_at=before.created_at,
    )


# ---------- SNIPE QUERY ----------


class SnipeQueryError(Exception):
    """A problem with what the moderator typed — shown to them, not logged."""


@dataclass
class SnipeQuery:
    index: Optional[int] = None               # `$s 3`
    span: Optional[tuple] = None              # `$s 2-6`
    keyword: Optional[str] = None             # `$s contains word` (stored lowercase)
    user_id: Optional[int] = None             # `$s user @x`
    channel: Optional[discord.abc.GuildChannel] = None   # `$s in #chan`
    everywhere: bool = False                  # `$s all`
    files_only: bool = False                  # `$s files`
    purged_only: bool = False                 # `$s purged`
    max_age: Optional[timedelta] = None       # `$s 15m`
    mode: str = "detail"                      # "detail" | "list"
    action: str = "view"                      # "view" | "stats" | "export" | "clear" | "help"


_KW_CONTAINS = {"contains", "contain", "has", "with", "match", "find", "search", "c"}
_KW_USER = {"user", "from", "by", "u"}
_KW_CHANNEL = {"in", "channel", "ch"}
_KW_ALL = {"all", "server", "global", "guild", "everywhere"}
_KW_FILES = {"files", "file", "attachments", "attachment", "media", "images", "image", "pics"}
_KW_PURGED = {"purged", "purge", "bulk", "cleared"}
_KW_AGE = {"last", "within", "since", "ago"}
_KW_LIST = {"list", "ls", "log", "history", "l"}
_KW_STATS = {"top", "stats", "stat", "leaderboard", "lb"}
_KW_EXPORT = {"export", "dump", "save"}
_KW_CLEAR = {"clear", "wipe", "reset"}

_ID_RE = re.compile(r"^(?:<@!?(\d{15,25})>|(\d{15,25}))$")
_CHANNEL_MENTION_RE = re.compile(r"^<#(\d{15,25})>$")
_RANGE_RE = re.compile(r"^(\d{1,3})(?:-|\.\.)(\d{1,3})$")
_DUR_RE = re.compile(r"^(\d{1,4})([smhd])$")
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(token: str) -> timedelta:
    m = _DUR_RE.match(token.lower())
    if not m or int(m.group(1)) == 0:
        raise SnipeQueryError(f"`{token}` isn't a duration — use something like `30s`, `15m`, `2h` or `1d`.")
    return timedelta(seconds=int(m.group(1)) * _UNIT_SECONDS[m.group(2)])


def _fmt_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if secs >= size and secs % size == 0:
            return f"{secs // size}{unit}"
    return f"{secs}s"


def _snipe_matches(entry: SnipeEntry, q: SnipeQuery, now: datetime) -> bool:
    if q.user_id is not None and entry.author_id != q.user_id:
        return False
    if q.files_only and not entry.attachments:
        return False
    if q.purged_only and not entry.purged:
        return False
    if q.max_age is not None and now - entry.time > q.max_age:
        return False
    if q.keyword:
        haystack = "\n".join([entry.before, entry.content, *(fn for fn, _, _ in entry.attachments)]).lower()
        if q.keyword not in haystack:
            return False
    return True


def _describe_query(q: SnipeQuery) -> str:
    bits = []
    if q.keyword:
        bits.append(f'containing "{discord.utils.escape_mentions(q.keyword)}"')
    if q.user_id is not None:
        bits.append(f"from user `{q.user_id}`")
    if q.files_only:
        bits.append("with attachments")
    if q.purged_only:
        bits.append("that were purged")
    if q.max_age is not None:
        bits.append(f"in the last {_fmt_duration(q.max_age)}")
    return ", ".join(bits) or "no filters"


def _guild_channel(guild: discord.Guild, channel_id: int):
    getter = getattr(guild, "get_channel_or_thread", guild.get_channel)
    return getter(channel_id)


def _can_view(channel, member: discord.Member) -> bool:
    perms = channel.permissions_for(member)
    return bool(perms.view_channel and perms.read_message_history)


# ---------- SNIPE EMBEDS ----------


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _one_line(text: str, limit: int) -> str:
    flat = _clip(" ".join((text or "").split()), limit)
    return discord.utils.escape_markdown(discord.utils.escape_mentions(flat))


def _snipe_detail_embed(
    entry: SnipeEntry, rank: int, total: int, pos: int, count: int, requester: str
) -> discord.Embed:
    is_edit = entry.kind == "edit"
    if is_edit:
        title = "Edit Sniped"
    else:
        title = "Sniped Message · Purged" if entry.purged else "Sniped Message"

    embed = discord.Embed(title=title, color=config.EMBED_COLOR_DARK, timestamp=entry.time)
    embed.set_author(name=entry.author_name, icon_url=entry.avatar_url)

    if is_edit:
        embed.add_field(name="Before", value=_clip(entry.before, 1000) or "*[No text]*", inline=False)
        embed.add_field(name="After", value=_clip(entry.content, 1000) or "*[No text]*", inline=False)
    else:
        if entry.content:
            embed.description = _clip(entry.content, 4000)
        elif entry.attachments or entry.stickers:
            embed.description = "*[No text — attachment only]*"
        else:
            embed.description = "*[No text content]*"

    verb = "Edited" if is_edit else "Deleted"
    embed.add_field(name="Author", value=f"<@{entry.author_id}>\n`{entry.author_id}`", inline=True)
    embed.add_field(name="Channel", value=f"<#{entry.channel_id}>", inline=True)
    embed.add_field(
        name="Timing",
        value=(
            f"Sent {discord.utils.format_dt(entry.sent_at, 'R')}\n"
            f"{verb} {discord.utils.format_dt(entry.time, 'R')}"
        ),
        inline=True,
    )
    embed.add_field(name="Message ID", value=f"`{entry.message_id}`", inline=True)
    if is_edit and entry.jump_url:
        embed.add_field(name="Message", value=f"[Jump to message]({entry.jump_url})", inline=True)

    if entry.attachments:
        lines, used = [], 0
        for idx, (filename, url, _ctype) in enumerate(entry.attachments):
            name = _clip(discord.utils.escape_markdown(filename), 40)
            line = f"📎 [{name}]({url})"
            if used + len(line) + 1 > 950:
                lines.append(f"…and {len(entry.attachments) - idx} more")
                break
            lines.append(line)
            used += len(line) + 1
        embed.add_field(name=f"Attachments ({len(entry.attachments)})", value="\n".join(lines), inline=False)
        for _filename, url, ctype in entry.attachments:
            if ctype.startswith("image/"):
                embed.set_image(url=url)
                break

    if entry.stickers:
        embed.add_field(name="Stickers", value=_clip(", ".join(entry.stickers), 1000), inline=True)
    if entry.reply_to:
        embed.add_field(name="Replying to", value=_clip(entry.reply_to, 1000), inline=True)

    parts = [config.FOOTER_TEXT, "edited message" if is_edit else "deleted message", f"#{rank} of {total}"]
    if count != total:
        parts.append(f"match {pos + 1}/{count}")
    parts.append(f"asked by {requester}")
    embed.set_footer(text=" • ".join(parts))
    return embed


def _snipe_list_embed(
    results: list, page: int, kind: str, total: int, scope: str, requester: str
) -> discord.Embed:
    is_edit = kind == "edit"
    short = f"{config.COMMAND_PREFIX}{'es' if is_edit else 's'}"
    pages = max(1, math.ceil(len(results) / SNIPE_PER_PAGE))
    chunk = results[page * SNIPE_PER_PAGE:(page + 1) * SNIPE_PER_PAGE]

    lines = []
    for rank, e in chunk:
        flags = ""
        if e.attachments:
            flags += f" 📎{len(e.attachments)}"
        if e.purged:
            flags += " 🧹"
        if is_edit:
            text = f"{_one_line(e.before, 45) or '∅'} → {_one_line(e.content, 45) or '∅'}"
        else:
            text = _one_line(e.content, 90) or (
                "*[attachment only]*" if e.attachments or e.stickers else "*[no text]*"
            )
        lines.append(
            f"`#{rank:<3}` {discord.utils.format_dt(e.time, 'R')} · <@{e.author_id}> · <#{e.channel_id}>{flags}\n> {text}"
        )

    header = f"{scope} — newest first. Open one with `{short} <#>`.\n\n"
    embed = discord.Embed(
        title="Edit Snipe Log" if is_edit else "Snipe Log",
        description=header + ("\n".join(lines) or "*Nothing on this page.*"),
        color=config.EMBED_COLOR_DARK,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(
        text=f"{config.FOOTER_TEXT} • page {page + 1}/{pages} • {len(results)} of {total} shown • asked by {requester}"
    )
    return embed


def _peak_burst(times: list) -> int:
    """Most events inside any SNIPE_BURST_WINDOW-second window (times ascending)."""
    best = lo = 0
    for hi, t in enumerate(times):
        while (t - times[lo]).total_seconds() > SNIPE_BURST_WINDOW:
            lo += 1
        best = max(best, hi - lo + 1)
    return best


def _snipe_stats_embed(entries: list, kind: str, scope: str, requester: str) -> discord.Embed:
    is_edit = kind == "edit"
    noun = "edits" if is_edit else "deletions"
    who = "editors" if is_edit else "deleters"

    embed = discord.Embed(
        title=f"Snipe Stats — {'Edits' if is_edit else 'Deletions'}",
        color=config.EMBED_COLOR_DARK,
        timestamp=discord.utils.utcnow(),
    )

    overview = [
        f"**{len(entries)}** {noun} cached — {scope}",
        f"Newest {discord.utils.format_dt(entries[0].time, 'R')} · oldest {discord.utils.format_dt(entries[-1].time, 'R')}",
    ]
    with_files = sum(1 for e in entries if e.attachments)
    purged = sum(1 for e in entries if e.purged)
    if with_files:
        overview.append(f"📎 {with_files} had attachments")
    if purged:
        overview.append(f"🧹 {purged} removed by a purge")
    embed.add_field(name="Overview", value="\n".join(overview), inline=False)

    by_author = Counter(e.author_id for e in entries)
    top = []
    for pos, (uid, count) in enumerate(by_author.most_common(5), 1):
        extras = []
        author_purged = sum(1 for e in entries if e.author_id == uid and e.purged)
        author_files = sum(1 for e in entries if e.author_id == uid and e.attachments)
        if author_purged:
            extras.append(f"{author_purged} purged")
        if author_files:
            extras.append(f"{author_files} with files")
        suffix = f" ({', '.join(extras)})" if extras else ""
        top.append(f"`{pos}.` <@{uid}> — **{count}**{suffix}")
    embed.add_field(name=f"Top {who}", value="\n".join(top), inline=False)

    times_by_author = defaultdict(list)
    for e in entries:
        if not e.purged:
            times_by_author[e.author_id].append(e.time)
    alerts = []
    for uid, times in times_by_author.items():
        times.sort()
        peak = _peak_burst(times)
        if peak >= SNIPE_BURST_COUNT:
            alerts.append((peak, uid))
    if alerts:
        alerts.sort(reverse=True)
        lines = [f"🚨 <@{uid}> — **{peak}** {noun} within {SNIPE_BURST_WINDOW}s" for peak, uid in alerts[:5]]
        embed.add_field(name=f"Rapid {who}", value="\n".join(lines), inline=False)

    by_channel = Counter(e.channel_id for e in entries)
    if len(by_channel) > 1:
        lines = [f"<#{cid}> — **{n}**" for cid, n in by_channel.most_common(5)]
        embed.add_field(name="Busiest channels", value="\n".join(lines), inline=False)

    embed.set_footer(text=f"{config.FOOTER_TEXT} • snipe stats • asked by {requester}")
    return embed


def _indent(text: str) -> str:
    lines = (text or "").splitlines() or ["[no text]"]
    return "\n".join(f"      {ln}" for ln in lines)


def _snipe_export_text(results: list, kind: str, guild: discord.Guild, scope: str, requester: str) -> str:
    is_edit = kind == "edit"
    now = discord.utils.utcnow()
    out = [
        f"{config.BOT_NAME} snipe export — {'edited' if is_edit else 'deleted'} messages",
        f"Server:    {guild.name} ({guild.id})",
        f"Scope:     {scope}",
        f"Generated: {now:%Y-%m-%d %H:%M:%S} UTC, requested by {requester}",
        f"Entries:   {len(results)} (newest first; #N is the serial number in the cache)",
        "=" * 64,
        "",
    ]
    for rank, e in results:
        ch = _guild_channel(guild, e.channel_id)
        ch_name = f"#{ch.name}" if ch is not None else str(e.channel_id)
        tag = "EDIT" if is_edit else ("DELETE (purged)" if e.purged else "DELETE")
        out.append(f"#{rank}  [{e.time:%Y-%m-%d %H:%M:%S} UTC]  {ch_name}  {tag}")
        out.append(f"    Author:     {e.author_name} ({e.author_id})")
        out.append(f"    Message ID: {e.message_id}")
        out.append(f"    Sent:       {e.sent_at:%Y-%m-%d %H:%M:%S} UTC")
        if e.reply_to:
            out.append("    Reply:      (replying to another message)")
        if is_edit:
            out.append("    Before:")
            out.append(_indent(e.before))
            out.append("    After:")
            out.append(_indent(e.content))
        else:
            out.append("    Content:")
            out.append(_indent(e.content))
        for filename, url, _ctype in e.attachments:
            out.append(f"    Attachment: {filename} — {url}")
        if e.stickers:
            out.append(f"    Stickers:   {', '.join(e.stickers)}")
        out.append("")
    return "\n".join(out)


def _snipe_usage_embed(kind: str) -> discord.Embed:
    p = config.COMMAND_PREFIX
    is_edit = kind == "edit"
    short = f"{p}es" if is_edit else f"{p}s"
    full = f"{p}editsnipe" if is_edit else f"{p}snipe"
    what = "edited" if is_edit else "deleted"
    hours = int(SNIPE_TTL.total_seconds() // 3600)

    embed = discord.Embed(
        title=f"{'Edit snipe' if is_edit else 'Snipe'} — Syntax",
        description=(
            f"Every {what} message is remembered per channel — up to **{SNIPE_DEPTH}** of them, "
            f"for **{hours}h**. `{short}` is the short form of `{full}`.\n"
            f"Anything you type after the command is a mix of the options below."
        ),
        color=config.EMBED_COLOR_DARK,
    )
    embed.add_field(
        name="Pick a message",
        value=(
            f"`{short}` — the latest\n"
            f"`{short} 3` — the 3rd most recent (`#N` in a result = `{short} N`)\n"
            f"`{short} 2-6` — a range, shown as a list\n"
            f"`{short} 15m` — only the last 15 minutes (`s` `m` `h` `d`)"
        ),
        inline=False,
    )
    filters = [
        f"`{short} contains <word>` — text search (**put it last**, it takes the rest of the line)",
        f"`{short} user <@user|id>` — one person's messages",
    ]
    if not is_edit:
        filters += [
            f"`{short} files` — only messages with attachments",
            f"`{short} purged` — only messages removed by `{p}clear`",
        ]
    filters += [
        f"`{short} in <#channel>` — another channel",
        f"`{short} all` — every channel you can see",
    ]
    embed.add_field(name="Filters (stackable)", value="\n".join(filters), inline=False)
    embed.add_field(
        name="Tools",
        value=(
            f"`{short} list` — compact log\n"
            f"`{short} top` — who {'edits' if is_edit else 'deletes'} the most, with rapid-fire alerts\n"
            f"`{short} export` — the results as a .txt file (sent privately)\n"
            f"`{short} clear` — forget the matching entries (**Mod** only, asks first)"
        ),
        inline=False,
    )
    embed.add_field(
        name="Examples",
        value=(
            f"`{short} user @troll files last 1h`\n"
            f"`{short} all contains discord.gg`\n"
            f"`{short} in #general 5`"
        ),
        inline=False,
    )
    embed.set_footer(text=config.FOOTER_TEXT)
    return embed


# ---------- SNIPE BROWSER ----------


class SnipeView(discord.ui.View):
    """Detail/List browser over a set of snipe results. Only the asker can press."""

    def __init__(
        self,
        *,
        author_id: int,
        results: list,
        kind: str,
        total: int,
        scope: str,
        requester: str,
        start: int = 0,
        mode: str = "detail",
    ):
        super().__init__(timeout=SNIPE_VIEW_TIMEOUT)
        self.author_id = author_id
        self.results = results
        self.kind = kind
        self.total = total
        self.scope = scope
        self.requester = requester
        self.index = start
        self.mode = mode
        self.message: Optional[discord.Message] = None
        self._sync()

    @property
    def pages(self) -> int:
        return max(1, math.ceil(len(self.results) / SNIPE_PER_PAGE))

    @property
    def page(self) -> int:
        return self.index // SNIPE_PER_PAGE

    def current_embed(self) -> discord.Embed:
        if self.mode == "list":
            return _snipe_list_embed(self.results, self.page, self.kind, self.total, self.scope, self.requester)
        rank, entry = self.results[self.index]
        return _snipe_detail_embed(entry, rank, self.total, self.index, len(self.results), self.requester)

    def _sync(self) -> None:
        count = len(self.results)
        if self.mode == "list":
            at_start, at_end = self.page == 0, self.page >= self.pages - 1
            self.btn_mode.label, self.btn_mode.emoji = "Detail", "🔍"
        else:
            at_start, at_end = self.index == 0, self.index >= count - 1
            self.btn_mode.label, self.btn_mode.emoji = "List", "📋"
        self.btn_first.disabled = self.btn_newer.disabled = at_start
        self.btn_older.disabled = self.btn_last.disabled = at_end
        self.btn_mode.disabled = count <= 1

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "This browser isn't yours — run the command yourself.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_first(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = 0
        await self._refresh(interaction)

    @discord.ui.button(label="Newer", emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_newer(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode == "list":
            self.index = max(0, self.page - 1) * SNIPE_PER_PAGE
        else:
            self.index = max(0, self.index - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Older", emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_older(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode == "list":
            self.index = min(self.page + 1, self.pages - 1) * SNIPE_PER_PAGE
        else:
            self.index = min(self.index + 1, len(self.results) - 1)
        await self._refresh(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def btn_last(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.mode == "list":
            self.index = (self.pages - 1) * SNIPE_PER_PAGE
        else:
            self.index = len(self.results) - 1
        await self._refresh(interaction)

    @discord.ui.button(label="List", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def btn_mode(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.mode = "detail" if self.mode == "list" else "list"
        await self._refresh(interaction)

    @discord.ui.button(emoji="✖️", style=discord.ButtonStyle.danger, row=1)
    async def btn_close(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        try:
            await interaction.response.defer()
            if self.message:
                await self.message.delete()
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass

    async def on_timeout(self):
        for item in self.children:
            item.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


# ---------- IN-MEMORY STORES ----------
mod_warnings: dict[int, list[dict]] = defaultdict(list)


# ---------- HELPERS ----------


def _mod_embed(title: str, color: int, fields: list[tuple]) -> discord.Embed:
    embed = discord.Embed(title=title, color=color, timestamp=datetime.utcnow())
    for name, value in fields:
        embed.add_field(name=name, value=value, inline=False)
    embed.set_footer(text=config.FOOTER_TEXT)
    return embed


# Roles carrying any of these permissions can't be mass-assigned: handing
# them to every member at once would effectively hand over the server.
_MASSROLE_BLOCKED_PERMS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "moderate_members",
    "mention_everyone",
)


class _ConfirmView(discord.ui.View):
    """Confirm / Cancel buttons that only the invoking moderator can press."""

    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This prompt isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


# ---------- COG ----------


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._massrole_guilds: set[int] = set()

    async def cog_check(self, ctx: commands.Context) -> bool:
        # Everything in this cog assumes a guild (roles, members, channels).
        if ctx.guild is None:
            raise commands.CheckFailure("This command only works inside a server.")
        return True

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error), ephemeral=True)
        else:
            raise error

    # ---------- KICK (Mod only) ----------

    @commands.hybrid_command(description="Kick a member from the server.")
    @has_mod_role()
    async def kick(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.kick(reason=reason)
        await ctx.send(embed=_mod_embed(
            "👢 Member Kicked",
            config.EMBED_COLOR,
            [("Member", member.mention), ("Reason", reason), ("Moderator", ctx.author.mention)]
        ))

    # ---------- BAN (Mod only) ----------

    @commands.hybrid_command(description="Ban a member from the server.")
    @has_mod_role()
    async def ban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.ban(reason=reason)
        await ctx.send(embed=_mod_embed(
            "🩸 Member Banned",
            config.EMBED_COLOR,
            [("Member", str(member)), ("Reason", reason), ("Moderator", ctx.author.mention)]
        ))

    # ---------- UNBAN (Mod only) ----------

    @commands.hybrid_command(description="Unban a user by ID.")
    @has_mod_role()
    async def unban(self, ctx: commands.Context, user_id: str):
        try:
            user = await self.bot.fetch_user(int(user_id))
            await ctx.guild.unban(user)
            await ctx.send(embed=_mod_embed(
                "Member Unbanned",
                config.EMBED_COLOR_DARK,
                [("User", str(user)), ("Moderator", ctx.author.mention)]
            ))
        except ValueError:
            await ctx.send("That doesn't look like a valid user ID.", ephemeral=True)
        except discord.NotFound:
            await ctx.send("User not found or not banned.", ephemeral=True)

    # ---------- SOFTBAN (Mod only) ----------

    @commands.hybrid_command(description="Ban then immediately unban a member, clearing their recent messages.")
    @has_mod_role()
    async def softban(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        await member.ban(reason=f"Softban: {reason}", delete_message_days=1)
        await ctx.guild.unban(member, reason="Softban unban")
        await ctx.send(embed=_mod_embed(
            "Member Softbanned",
            config.EMBED_COLOR,
            [("Member", member.mention), ("Reason", reason), ("Moderator", ctx.author.mention)]
        ))

    # ---------- TIMEOUT (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["mute"], description="Timeout a member for a number of minutes.")
    @has_any_mod_role()
    async def timeout(self, ctx: commands.Context, member: discord.Member, minutes: int, *, reason: str = "No reason provided"):
        if minutes <= 0:
            return await ctx.send("Duration must be greater than 0 minutes.", ephemeral=True)
        if minutes > 40320:
            return await ctx.send("Max timeout is **28 days** (40,320 minutes).", ephemeral=True)

        until = discord.utils.utcnow() + timedelta(minutes=minutes)
        await member.timeout(until, reason=reason)

        h, m = divmod(minutes, 60)
        duration_str = f"{h}h {m}m" if h else f"{m}m"

        await ctx.send(embed=_mod_embed(
            "⛓️ Member Timed Out",
            config.EMBED_COLOR,
            [
                ("Member", member.mention),
                ("Duration", duration_str),
                ("Reason", reason),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- UNTIMEOUT (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["unmute", "untimeout"], description="Remove an active timeout from a member.")
    @has_any_mod_role()
    async def removetimeout(self, ctx: commands.Context, member: discord.Member):
        await member.timeout(None)
        await ctx.send(embed=_mod_embed(
            "Timeout Removed",
            config.EMBED_COLOR_DARK,
            [("Member", member.mention), ("Moderator", ctx.author.mention)]
        ))

    # ---------- WARN (Trial Mod+) ----------

    @commands.hybrid_command(description="Issue a warning to a member.")
    @has_any_mod_role()
    async def warn(self, ctx: commands.Context, member: discord.Member, *, reason: str = "No reason provided"):
        entry = {
            "reason": reason,
            "moderator": str(ctx.author),
            "time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        }
        mod_warnings[member.id].append(entry)
        count = len(mod_warnings[member.id])

        await ctx.send(embed=_mod_embed(
            f"⚠️ Warning Issued (#{count})",
            config.EMBED_COLOR,
            [
                ("Member", member.mention),
                ("Reason", reason),
                ("Total Warnings", str(count)),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- WARNINGS (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["warns", "infractions"], description="View a member's warnings.")
    @has_any_mod_role()
    async def warnings(self, ctx: commands.Context, member: discord.Member):
        user_warns = mod_warnings.get(member.id, [])

        if not user_warns:
            return await ctx.send(f"{member.mention} has no warnings.")

        embed = discord.Embed(
            title=f"⚠️ Warnings — {member.display_name}",
            color=config.EMBED_COLOR_DARK,
            timestamp=datetime.utcnow(),
        )
        for i, w in enumerate(user_warns, 1):
            embed.add_field(
                name=f"#{i} — {w['time']}",
                value=f"{w['reason']}\nBy: {w['moderator']}",
                inline=False,
            )
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ---------- CLEAR WARNINGS (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["clearwarns"], description="Clear all warnings for a member.")
    @has_any_mod_role()
    async def clearwarnings(self, ctx: commands.Context, member: discord.Member):
        count = len(mod_warnings.pop(member.id, []))
        await ctx.send(embed=_mod_embed(
            "Warnings Cleared",
            config.EMBED_COLOR_DARK,
            [("Member", member.mention), ("Removed", f"{count} warning(s)"), ("Moderator", ctx.author.mention)]
        ))

    # ---------- CLEAR / PURGE (Trial Mod+) ----------
    # Prefix-only: the free-form argument ("bots" / "user @x" / "contains
    # word" / a bare number) doesn't map onto one slash option cleanly.

    @commands.command(
        name="clear",
        aliases=["purge"],
        description="Bulk delete messages. Prefix-only: also supports `bots` / `user @x` / `contains <word>`.",
    )
    @has_any_mod_role()
    async def clear(self, ctx: commands.Context, *, arg: str):
        MAX_SCAN = 100
        MAX_DELETE = 50

        await ctx.message.delete()
        arg_lower = arg.lower().strip()

        if arg_lower in ("bots", "contains bots"):
            deleted = []
            async for msg in ctx.channel.history(limit=MAX_SCAN):
                if msg.author.bot:
                    deleted.append(msg)
                    if len(deleted) >= MAX_DELETE:
                        break
            if not deleted:
                return await ctx.send("No recent bot messages found.", delete_after=3)
            await ctx.channel.delete_messages(deleted)
            return await ctx.send(f"Deleted **{len(deleted)}** bot messages.", delete_after=3)

        if arg_lower.startswith("user "):
            raw = arg[5:].strip()
            member_id = None
            if ctx.message.mentions:
                member_id = ctx.message.mentions[0].id
            else:
                try:
                    member_id = int(raw.strip("<@!>"))
                except ValueError:
                    return await ctx.send("Mention a valid user.")

            deleted = []
            async for msg in ctx.channel.history(limit=MAX_SCAN):
                if msg.author.id == member_id:
                    deleted.append(msg)
                    if len(deleted) >= MAX_DELETE:
                        break
            if not deleted:
                return await ctx.send("No recent messages from that user.", delete_after=3)
            await ctx.channel.delete_messages(deleted)
            return await ctx.send(f"Deleted **{len(deleted)}** messages from that user.", delete_after=3)

        if arg_lower.startswith("contains "):
            keyword = arg[9:].strip().lower()
            if not keyword:
                return await ctx.send("Provide a keyword.")
            deleted = []
            async for msg in ctx.channel.history(limit=MAX_SCAN):
                if keyword in msg.content.lower():
                    deleted.append(msg)
                    if len(deleted) >= MAX_DELETE:
                        break
            if not deleted:
                return await ctx.send(f"No messages found containing **'{keyword}'**.", delete_after=3)
            await ctx.channel.delete_messages(deleted)
            return await ctx.send(f"Deleted **{len(deleted)}** messages containing **'{keyword}'**.", delete_after=3)

        try:
            amount = int(arg)
        except ValueError:
            return await ctx.send(
                "Invalid usage.\n"
                f"`{config.COMMAND_PREFIX}clear <amount>`\n"
                f"`{config.COMMAND_PREFIX}clear bots`\n"
                f"`{config.COMMAND_PREFIX}clear user @member`\n"
                f"`{config.COMMAND_PREFIX}clear contains <keyword>`"
            )

        if amount <= 0 or amount > 100:
            return await ctx.send("Enter a number between 1 and 100.")

        deleted = await ctx.channel.purge(limit=amount)
        await ctx.send(f"Deleted **{len(deleted)}** messages.", delete_after=3)

    # ---------- LOCK / UNLOCK (Trial Mod+) ----------

    @commands.hybrid_command(description="Lock a channel so @everyone can't send messages.")
    @has_any_mod_role()
    async def lock(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = False
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(embed=_mod_embed(
            "🔒 Channel Locked",
            config.EMBED_COLOR,
            [("Channel", channel.mention), ("Moderator", ctx.author.mention)]
        ))

    @commands.hybrid_command(description="Unlock a previously locked channel.")
    @has_any_mod_role()
    async def unlock(self, ctx: commands.Context, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        overwrite = channel.overwrites_for(ctx.guild.default_role)
        overwrite.send_messages = True
        await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
        await ctx.send(embed=_mod_embed(
            "🔓 Channel Unlocked",
            config.EMBED_COLOR_DARK,
            [("Channel", channel.mention), ("Moderator", ctx.author.mention)]
        ))

    # ---------- SLOWMODE (Trial Mod+) ----------

    @commands.hybrid_command(description="Set a channel's slowmode delay, in seconds.")
    @has_any_mod_role()
    async def slowmode(self, ctx: commands.Context, seconds: int, channel: discord.TextChannel = None):
        channel = channel or ctx.channel
        if not (0 <= seconds <= 21600):
            return await ctx.send("Slowmode must be between **0** and **21600** seconds.", ephemeral=True)
        await channel.edit(slowmode_delay=seconds)
        msg = f"**{seconds}s** delay set." if seconds > 0 else "Slowmode **disabled**."
        await ctx.send(embed=_mod_embed(
            "🐢 Slowmode Updated",
            config.EMBED_COLOR_DARK,
            [("Channel", channel.mention), ("Delay", msg), ("Moderator", ctx.author.mention)]
        ))

    # ---------- NICK (Trial Mod+) ----------

    @commands.hybrid_command(description="Change a member's nickname.")
    @has_any_mod_role()
    async def nick(self, ctx: commands.Context, member: discord.Member, *, nickname: str = None):
        old_nick = member.display_name
        await member.edit(nick=nickname)
        await ctx.send(embed=_mod_embed(
            "Nickname Changed",
            config.EMBED_COLOR_DARK,
            [
                ("Member", member.mention),
                ("Before", old_nick),
                ("After", nickname or "*reset*"),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- USERINFO (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["ui", "whois"], description="View a member's info.")
    @has_any_mod_role()
    async def userinfo(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        roles = [r.mention for r in member.roles[1:]] or ["None"]

        embed = discord.Embed(
            title=member.display_name,
            color=member.color if member.color.value else config.EMBED_COLOR_DARK,
            timestamp=datetime.utcnow(),
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        embed.add_field(name="Bot", value="Yes" if member.bot else "No", inline=True)
        embed.add_field(name="Account Created", value=discord.utils.format_dt(member.created_at, style="R"), inline=False)
        embed.add_field(name="Joined Server", value=discord.utils.format_dt(member.joined_at, style="R") if member.joined_at else "Unknown", inline=False)
        embed.add_field(name=f"Roles ({len(member.roles) - 1})", value=" ".join(roles[:10]) + (" …" if len(roles) > 10 else ""), inline=False)
        warns = len(mod_warnings.get(member.id, []))
        embed.add_field(name="Warnings", value=str(warns), inline=True)
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ---------- SERVERINFO (Trial Mod+) ----------

    @commands.hybrid_command(aliases=["si", "server"], description="View server info.")
    @has_any_mod_role()
    async def serverinfo(self, ctx: commands.Context):
        g = ctx.guild
        embed = discord.Embed(title=g.name, color=config.EMBED_COLOR_DARK, timestamp=datetime.utcnow())
        if g.icon:
            embed.set_thumbnail(url=g.icon.url)
        embed.add_field(name="ID", value=str(g.id), inline=True)
        embed.add_field(name="Owner", value=g.owner.mention if g.owner else "Unknown", inline=True)
        embed.add_field(name="Members", value=str(g.member_count), inline=True)
        embed.add_field(name="Channels", value=str(len(g.channels)), inline=True)
        embed.add_field(name="Roles", value=str(len(g.roles)), inline=True)
        embed.add_field(name="Emojis", value=str(len(g.emojis)), inline=True)
        embed.add_field(name="Created", value=discord.utils.format_dt(g.created_at, style="R"), inline=False)
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ══════════════════════════════════════════
    #  SNIPE / EDITSNIPE (Trial Mod+)
    # ══════════════════════════════════════════

    # ---------- RECORDING ----------

    async def _is_ignored_invocation(self, message: discord.Message) -> bool:
        """True for command messages that must never be recorded (see SNIPE_IGNORED_COMMANDS)."""
        if not message.content or not message.content.startswith(config.COMMAND_PREFIX):
            return False
        try:
            ctx = await self.bot.get_context(message)
        except Exception:
            return False
        return bool(ctx.valid and ctx.command is not None and ctx.command.qualified_name in SNIPE_IGNORED_COMMANDS)

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return
        if await self._is_ignored_invocation(message):
            return
        sniped_messages[message.channel.id].appendleft(_snapshot_delete(message))

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list):
        # `clear` / `purge` land here instead of on_message_delete. Oldest
        # first so the newest message ends up as #1.
        for message in sorted(messages, key=lambda m: m.created_at):
            if message.guild is None or message.author.bot:
                continue
            if await self._is_ignored_invocation(message):
                continue
            sniped_messages[message.channel.id].appendleft(_snapshot_delete(message, purged=True))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None or before.author.bot or before.content == after.content:
            return
        edited_messages[before.channel.id].appendleft(_snapshot_edit(before, after))

    # ---------- QUERY PARSING ----------

    async def _resolve_user_id(self, ctx: commands.Context, token: str) -> int:
        m = _ID_RE.match(token)
        if m:
            return int(m.group(1) or m.group(2))
        try:
            member = await commands.MemberConverter().convert(ctx, token)
        except commands.BadArgument:
            raise SnipeQueryError(f"I couldn't find a member matching `{token}` — use a mention or their ID.")
        return member.id

    async def _resolve_channel(self, ctx: commands.Context, token: str):
        try:
            return await commands.GuildChannelConverter().convert(ctx, token)
        except commands.BadArgument:
            raise SnipeQueryError(f"I couldn't find a channel matching `{token}`.")

    async def _parse_snipe_query(self, ctx: commands.Context, raw: Optional[str], kind: str) -> SnipeQuery:
        q = SnipeQuery()
        tokens = (raw or "").split()
        n = len(tokens)
        i = 0

        def take(what: str) -> str:
            nonlocal i
            i += 1
            if i >= n:
                raise SnipeQueryError(f"`{tokens[i - 1]}` needs {what} after it.")
            return tokens[i]

        while i < n:
            tok = tokens[i]
            low = tok.lower()

            if low in ("help", "?"):
                q.action = "help"
                return q

            if low in _KW_CONTAINS:
                keyword = " ".join(tokens[i + 1:]).strip()
                if not keyword:
                    raise SnipeQueryError(f"`{low}` needs a word or phrase after it.")
                q.keyword = keyword.lower()
                break

            if low in _KW_USER:
                q.user_id = await self._resolve_user_id(ctx, take("a user"))
            elif low in _KW_CHANNEL:
                q.channel = await self._resolve_channel(ctx, take("a channel"))
            elif low in _KW_ALL:
                q.everywhere = True
            elif low in _KW_FILES:
                q.files_only = True
            elif low in _KW_PURGED:
                q.purged_only = True
            elif low in _KW_AGE:
                q.max_age = _parse_duration(take("a duration like `15m`"))
            elif low in _KW_LIST:
                q.mode = "list"
            elif low in _KW_STATS:
                q.action = "stats"
            elif low in _KW_EXPORT:
                q.action = "export"
            elif low in _KW_CLEAR:
                q.action = "clear"
            elif _DUR_RE.match(low):
                q.max_age = _parse_duration(low)
            elif _RANGE_RE.match(low):
                a, b = (int(x) for x in _RANGE_RE.match(low).groups())
                if a < 1 or b < a:
                    raise SnipeQueryError(f"`{tok}` isn't a valid range — numbering starts at 1, e.g. `2-6`.")
                q.span = (a, b)
            elif low.isdigit() and len(low) <= 4:
                if int(low) < 1:
                    raise SnipeQueryError("Numbering starts at **1** (the latest message).")
                q.index = int(low)
            elif _ID_RE.match(tok):
                q.user_id = int(_ID_RE.match(tok).group(1) or _ID_RE.match(tok).group(2))
            elif _CHANNEL_MENTION_RE.match(tok):
                q.channel = await self._resolve_channel(ctx, tok)
            else:
                raise SnipeQueryError(f"I don't understand `{tok}`.")
            i += 1

        if q.everywhere and q.channel is not None:
            raise SnipeQueryError("Pick either `all` or `in <#channel>`, not both.")
        if q.index is not None and q.span is not None:
            raise SnipeQueryError("Use a single number or a range, not both.")
        if kind == "edit" and (q.files_only or q.purged_only):
            raise SnipeQueryError("`files` and `purged` only apply to deleted messages.")
        if q.span is not None:
            q.mode = "list"
        return q

    def _gather(self, ctx: commands.Context, store: dict, q: SnipeQuery):
        """In-scope entries (newest first), a mention-style scope label, and a plain one."""
        if q.everywhere:
            pool = []
            for cid, dq in store.items():
                channel = _guild_channel(ctx.guild, cid)
                if channel is None or not _can_view(channel, ctx.author):
                    continue
                pool.extend(dq)
            pool.sort(key=lambda e: e.time, reverse=True)
            return pool, "the whole server", "server-wide"

        channel = q.channel or ctx.channel
        if not _can_view(channel, ctx.author):
            raise SnipeQueryError(f"You can't view {channel.mention}, so you can't snipe it either.")
        return list(store.get(channel.id, ())), channel.mention, f"#{channel.name}"

    # ---------- RUNNER ----------

    async def _run_snipe(self, ctx: commands.Context, kind: str, raw: Optional[str]):
        store = sniped_messages if kind == "delete" else edited_messages
        cmd_name = "snipe" if kind == "delete" else "editsnipe"
        no_mentions = discord.AllowedMentions.none()
        _prune(store)

        try:
            q = await self._parse_snipe_query(ctx, raw, kind)
            if q.action == "help":
                return await ctx.send(embed=_snipe_usage_embed(kind))
            entries, scope, scope_plain = self._gather(ctx, store, q)
        except SnipeQueryError as exc:
            return await ctx.send(
                f"{exc}\nTry `{config.COMMAND_PREFIX}{cmd_name} help` for the syntax.",
                ephemeral=True,
                allowed_mentions=no_mentions,
            )

        total = len(entries)
        if not total:
            return await ctx.send(
                "Nothing to snipe here. The dead keep their silence." if kind == "delete"
                else "No recently edited messages here."
            )

        now = discord.utils.utcnow()
        results = [(rank, e) for rank, e in enumerate(entries, 1) if _snipe_matches(e, q, now)]
        if not results:
            return await ctx.send(
                f"Nothing matched — **{total}** cached, filtered by {_describe_query(q)}.",
                allowed_mentions=no_mentions,
            )

        if q.action == "stats":
            return await ctx.send(
                embed=_snipe_stats_embed([e for _, e in results], kind, scope, ctx.author.display_name)
            )
        if q.action == "export":
            return await self._send_snipe_export(ctx, kind, results, scope_plain)
        if q.action == "clear":
            return await self._clear_snipes(ctx, kind, store, results, scope)

        start, mode = 0, q.mode
        if q.span is not None:
            a, b = q.span
            results = results[a - 1:b]
            if not results:
                return await ctx.send(f"There are only **{len(entries)}** cached here — nothing in `{a}-{b}`.")
        elif q.index is not None:
            if q.index > len(results):
                return await ctx.send(
                    f"Only **{len(results)}** matching message(s) cached — you asked for **#{q.index}**."
                )
            start = q.index - 1

        view = SnipeView(
            author_id=ctx.author.id,
            results=results,
            kind=kind,
            total=total,
            scope=scope,
            requester=ctx.author.display_name,
            start=start,
            mode=mode,
        )
        view.message = await ctx.send(embed=view.current_embed(), view=view)

    async def _send_snipe_export(self, ctx: commands.Context, kind: str, results: list, scope_plain: str):
        text = _snipe_export_text(results, kind, ctx.guild, scope_plain, str(ctx.author))
        stamp = discord.utils.utcnow().strftime("%Y%m%d-%H%M%S")
        filename = f"snipe-{kind}-{stamp}.txt"
        summary = f"Exported **{len(results)}** {'edit' if kind == 'edit' else 'deletion'} record(s)."

        def make_file() -> discord.File:
            return discord.File(io.BytesIO(text.encode("utf-8")), filename=filename)

        if ctx.interaction is not None:
            return await ctx.send(summary, file=make_file(), ephemeral=True)

        # Prefix form: never post deleted content publicly — DM it instead.
        try:
            await ctx.author.send(summary, file=make_file())
        except discord.Forbidden:
            return await ctx.send("I couldn't DM you the export. Open your DMs and try again.")
        await ctx.send("📬 Export sent to your DMs — it holds deleted content, so I kept it out of the channel.")

    async def _clear_snipes(self, ctx: commands.Context, kind: str, store: dict, results: list, scope: str):
        is_owner = await ctx.bot.is_owner(ctx.author)
        if not (is_owner or (config.MOD_ROLE_ID and config.MOD_ROLE_ID in _member_role_ids(ctx))):
            return await ctx.send("Clearing snipe history requires the **Mod** role or higher.", ephemeral=True)

        noun = "edit" if kind == "edit" else "deleted-message"
        doomed = {id(e) for _, e in results}
        view = _ConfirmView(ctx.author.id)
        prompt = await ctx.send(
            embed=_mod_embed(
                "Confirm Snipe Clear",
                config.EMBED_COLOR,
                [("Forgetting", f"**{len(results)}** cached {noun} record(s)"), ("Scope", scope)],
            ),
            view=view,
        )
        await view.wait()
        if view.value is not True:
            await prompt.edit(content="Cancelled." if view.value is False else "Timed out.", embed=None, view=None)
            return

        removed = 0
        for cid, dq in list(store.items()):
            keep = [e for e in dq if id(e) not in doomed]
            removed += len(dq) - len(keep)
            dq.clear()
            dq.extend(keep)
            if not dq:
                del store[cid]

        await prompt.edit(
            content=None,
            embed=_mod_embed(
                "Snipe History Cleared",
                config.EMBED_COLOR_DARK,
                [("Removed", f"**{removed}** record(s)"), ("Scope", scope), ("Moderator", ctx.author.mention)],
            ),
            view=None,
        )

    # ---------- SNIPE (Trial Mod+) ----------

    @commands.hybrid_command(
        name="snipe",
        aliases=["s"],
        description="Browse deleted messages — by number, keyword, user, and more.",
    )
    @app_commands.describe(query="n, contains <word>, user <id>, all, files, last 10m, list, top, export, clear, help")
    @has_any_mod_role()
    async def snipe(self, ctx: commands.Context, *, query: str = None):
        await self._run_snipe(ctx, "delete", query)

    # ---------- EDIT SNIPE (Trial Mod+) ----------

    @commands.hybrid_command(
        name="editsnipe",
        aliases=["es"],
        description="Browse edited messages — by number, keyword, user, and more.",
    )
    @app_commands.describe(query="n, contains <word>, user <id>, all, last 10m, list, top, export, clear, help")
    @has_any_mod_role()
    async def editsnipe(self, ctx: commands.Context, *, query: str = None):
        await self._run_snipe(ctx, "edit", query)

    # ---------- MODINFO ----------

    @commands.hybrid_command(description="Show what each staff tier can do.")
    @has_any_mod_role()
    async def modinfo(self, ctx: commands.Context):
        prefix = config.COMMAND_PREFIX
        mod_role = f"<@&{config.MOD_ROLE_ID}>" if config.MOD_ROLE_ID else "*(MOD_ROLE_ID not set)*"
        trial_role = f"<@&{config.TRIAL_MOD_ROLE_ID}>" if config.TRIAL_MOD_ROLE_ID else "*(TRIAL_MOD_ROLE_ID not set)*"

        embed = discord.Embed(
            title="Discipline of the Garden",
            description="A breakdown of what each staff tier can do.",
            color=config.EMBED_COLOR_DARK,
            timestamp=datetime.utcnow(),
        )
        embed.add_field(
            name=f"Mod only — {mod_role}",
            value=(
                f"`{prefix}ban` — Permanently ban a member\n"
                f"`{prefix}unban` — Unban a user by ID\n"
                f"`{prefix}kick` — Kick a member\n"
                f"`{prefix}softban` — Ban + unban to clear messages\n"
                f"`{prefix}massrole` — Give role(s) to every member"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"Trial Mod + Mod — {trial_role} {mod_role}",
            value=(
                f"`{prefix}timeout / {prefix}mute` — Timeout a member\n"
                f"`{prefix}removetimeout / {prefix}unmute` — Remove a timeout\n"
                f"`{prefix}warn` — Issue a warning\n"
                f"`{prefix}warnings / {prefix}warns` — View a member's warnings\n"
                f"`{prefix}clearwarnings / {prefix}clearwarns` — Clear all warnings\n"
                f"`{prefix}clear / {prefix}purge` — Bulk delete messages\n"
                f"`{prefix}lock` — Lock a channel\n"
                f"`{prefix}unlock` — Unlock a channel\n"
                f"`{prefix}slowmode` — Set channel slowmode\n"
                f"`{prefix}nick` — Change a member's nickname\n"
                f"`{prefix}userinfo / {prefix}whois` — View member info\n"
                f"`{prefix}serverinfo / {prefix}server` — View server info\n"
                f"`{prefix}snipe / {prefix}s` — Browse deleted messages (`{prefix}s help`)\n"
                f"`{prefix}editsnipe / {prefix}es` — Browse edited messages\n"
                f"`{prefix}modinfo` — Show this panel\n"
                f"`{prefix}newrole` — Create a new role\n"
                f"`{prefix}role setposition` — Set a role's position\n"
                f"`{prefix}rolename ch` — Rename a role\n"
                f"`{prefix}arole` — Assign a role to a user\n"
                f"`{prefix}remrole` — Remove a role from a user\n"
                f"`{prefix}rolepurge` — Strip all roles from a user\n"
                f"`{prefix}rolelist` — Paginated list of all roles"
            ),
            inline=False,
        )
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    # ══════════════════════════════════════════
    #  ROLE MANAGEMENT COMMANDS (Trial Mod+)
    # ══════════════════════════════════════════

    # ---------- NEWROLE ----------

    @commands.hybrid_command(description="Create a new role.")
    @has_any_mod_role()
    async def newrole(self, ctx: commands.Context, *, name: str):
        if not name:
            return await ctx.send(f"Usage: `{config.COMMAND_PREFIX}newrole <name>`", ephemeral=True)

        role = await ctx.guild.create_role(name=name, reason=f"Created by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Created",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Name", role.name),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- ROLE SETPOSITION ----------

    @commands.hybrid_group(name="role", invoke_without_command=True, description="Role management group.")
    @has_any_mod_role()
    async def role_group(self, ctx: commands.Context):
        await ctx.send(
            "Usage:\n"
            f"`{config.COMMAND_PREFIX}role setposition <@role or name> <position>` — "
            "set a role's position in the hierarchy"
        )

    @role_group.command(name="setposition", description="Set a role's position in the hierarchy.")
    @has_any_mod_role()
    async def role_setposition(self, ctx: commands.Context, role: discord.Role, position: int):
        if position < 1:
            return await ctx.send("Position must be 1 or higher.", ephemeral=True)

        max_pos = len(ctx.guild.roles) - 1
        if position > max_pos:
            return await ctx.send(f"Position can't exceed **{max_pos}** (total roles in server).", ephemeral=True)

        old_pos = role.position
        await role.edit(position=position, reason=f"Position set by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Position Updated",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Before", str(old_pos)),
                ("After", str(position)),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- RENAME ROLE ----------

    @commands.hybrid_command(name="rolename", description="Rename a role. Usage: ch <role> <new name>")
    @has_any_mod_role()
    async def rolename(self, ctx: commands.Context, subcommand: str, role: discord.Role, *, new_name: str):
        if subcommand.lower() != "ch":
            return await ctx.send(f"Usage: `{config.COMMAND_PREFIX}rolename ch <@role or name> <new name>`", ephemeral=True)
        if not new_name:
            return await ctx.send("Provide a new name.", ephemeral=True)

        old_name = role.name
        await role.edit(name=new_name, reason=f"Renamed by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Renamed",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Before", old_name),
                ("After", new_name),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- ASSIGN ROLE ----------

    @commands.hybrid_command(name="arole", description="Assign a role to a member.")
    @has_any_mod_role()
    async def arole(self, ctx: commands.Context, role: discord.Role, member: discord.Member):
        if role in member.roles:
            return await ctx.send(f"{member.mention} already has {role.mention}.", ephemeral=True)

        await member.add_roles(role, reason=f"Assigned by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Assigned",
            config.EMBED_COLOR_DARK,
            [
                ("Role", role.mention),
                ("Member", member.mention),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- REMOVE ROLE ----------

    @commands.hybrid_command(name="remrole", description="Remove a role from a member.")
    @has_any_mod_role()
    async def remrole(self, ctx: commands.Context, role: discord.Role, member: discord.Member):
        if role not in member.roles:
            return await ctx.send(f"{member.mention} doesn't have {role.mention}.", ephemeral=True)

        await member.remove_roles(role, reason=f"Removed by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Role Removed",
            config.EMBED_COLOR,
            [
                ("Role", role.mention),
                ("Member", member.mention),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- ROLEPURGE ----------

    @commands.hybrid_command(name="rolepurge", description="Strip all removable roles from a member.")
    @has_any_mod_role()
    async def rolepurge(self, ctx: commands.Context, member: discord.Member):
        removable = [
            r for r in member.roles
            if not r.is_default() and not r.managed
        ]

        if not removable:
            return await ctx.send(f"{member.mention} has no removable roles.", ephemeral=True)

        await member.remove_roles(*removable, reason=f"Role purge by {ctx.author}")
        await ctx.send(embed=_mod_embed(
            "Roles Purged",
            config.EMBED_COLOR,
            [
                ("Member", member.mention),
                ("Removed", f"**{len(removable)}** role(s)"),
                ("Moderator", ctx.author.mention),
            ]
        ))

    # ---------- MASSROLE (Mod only) ----------
    # Prefix-only: a variable number of roles doesn't map onto one slash option.

    @commands.command(
        name="massrole",
        description="Give one or more roles to every member of the server. Prefix-only.",
    )
    @has_mod_role()
    async def massrole(self, ctx: commands.Context, *roles: discord.Role):
        if not roles:
            return await ctx.send(f"Usage: `{config.COMMAND_PREFIX}massrole <@role> [@role ...]`")

        roles = list(dict.fromkeys(roles))  # drop duplicates, keep order
        guild = ctx.guild
        me = guild.me

        if guild.id in self._massrole_guilds:
            return await ctx.send("A mass role operation is already running in this server.")

        if not me.guild_permissions.manage_roles:
            return await ctx.send("I need the **Manage Roles** permission to do that.")

        trusted = ctx.author.id == guild.owner_id or await ctx.bot.is_owner(ctx.author)
        for role in roles:
            if role.is_default():
                return await ctx.send("`@everyone` can't be assigned.")
            if role.managed:
                return await ctx.send(f"{role.mention} is managed by an integration and can't be assigned.")
            if role >= me.top_role:
                return await ctx.send(f"{role.mention} is at or above my highest role, so I can't assign it.")
            if not trusted and role >= ctx.author.top_role:
                return await ctx.send(f"{role.mention} is at or above your highest role.")
            blocked = [p for p in _MASSROLE_BLOCKED_PERMS if getattr(role.permissions, p)]
            if blocked:
                names = ", ".join(p.replace("_", " ") for p in blocked)
                return await ctx.send(
                    f"{role.mention} has dangerous permissions ({names}) and can't be given to everyone."
                )

        if not guild.chunked:
            await guild.chunk()

        targets = []
        for member in guild.members:
            missing = [r for r in roles if r not in member.roles]
            if missing:
                targets.append((member, missing))

        role_mentions = " ".join(r.mention for r in roles)
        if not targets:
            return await ctx.send(f"Everyone already has {role_mentions}.")

        # Confirm before touching every member.
        view = _ConfirmView(ctx.author.id)
        prompt = await ctx.send(
            embed=_mod_embed(
                "Confirm Mass Role",
                config.EMBED_COLOR,
                [
                    ("Roles", role_mentions),
                    ("Members affected", f"**{len(targets)}** of {len(guild.members)}"),
                ],
            ),
            view=view,
        )
        await view.wait()
        if view.value is not True:
            await prompt.edit(content="Cancelled." if view.value is False else "Timed out.", embed=None, view=None)
            return

        reason = f"Massrole by {ctx.author}"
        done = failed = 0
        self._massrole_guilds.add(guild.id)
        try:
            for i, (member, missing) in enumerate(targets, 1):
                try:
                    await member.add_roles(*missing, reason=reason)
                    done += 1
                except discord.HTTPException:
                    failed += 1

                if i % 25 == 0:
                    await prompt.edit(
                        content=f"Assigning roles… **{i}/{len(targets)}**",
                        embed=None,
                        view=None,
                    )
        finally:
            self._massrole_guilds.discard(guild.id)

        fields = [
            ("Roles", role_mentions),
            ("Assigned to", f"**{done}** member(s)"),
        ]
        if failed:
            fields.append(("Failed", f"**{failed}** member(s)"))
        fields.append(("Moderator", ctx.author.mention))
        await prompt.edit(
            content=None,
            embed=_mod_embed("Mass Role Complete", config.EMBED_COLOR_DARK, fields),
            view=None,
        )

    # ---------- ROLELIST ----------

    @commands.hybrid_command(name="rolelist", description="Paginated list of all roles in the server.")
    @has_any_mod_role()
    async def rolelist(self, ctx: commands.Context):
        roles = sorted(
            [r for r in ctx.guild.roles if not r.is_default()],
            key=lambda r: r.position,
            reverse=True,
        )

        if not roles:
            return await ctx.send("This server has no roles.", ephemeral=True)

        CHUNK = 15
        chunks = [roles[i:i + CHUNK] for i in range(0, len(roles), CHUNK)]
        total_pages = len(chunks)

        def make_embed(page: int) -> discord.Embed:
            chunk = chunks[page]
            lines = []
            for r in chunk:
                members = len(r.members)
                color_hex = f"#{r.color.value:06X}" if r.color.value else "#000000"
                lines.append(f"`#{r.position:>3}`  {r.mention}  ·  {members} member{'s' if members != 1 else ''}  ·  {color_hex}")

            embed = discord.Embed(
                title=f"Role List — {ctx.guild.name}",
                description="\n".join(lines),
                color=config.EMBED_COLOR_DARK,
                timestamp=datetime.utcnow(),
            )
            embed.set_footer(text=f"{config.FOOTER_TEXT} · Page {page + 1}/{total_pages} · {len(roles)} roles total")
            return embed

        current = 0
        msg = await ctx.send(embed=make_embed(current))

        if total_pages == 1:
            return

        for emoji in ["◀️", "▶️", "❌"]:
            await msg.add_reaction(emoji)

        def check(reaction: discord.Reaction, user: discord.User) -> bool:
            return (
                user.id == ctx.author.id
                and reaction.message.id == msg.id
                and str(reaction.emoji) in ["◀️", "▶️", "❌"]
            )

        while True:
            try:
                reaction, user = await ctx.bot.wait_for("reaction_add", timeout=60.0, check=check)
                emoji = str(reaction.emoji)

                if emoji == "▶️":
                    current = (current + 1) % total_pages
                elif emoji == "◀️":
                    current = (current - 1) % total_pages
                elif emoji == "❌":
                    await msg.delete()
                    return

                await msg.edit(embed=make_embed(current))

                try:
                    await msg.remove_reaction(reaction, user)
                except discord.Forbidden:
                    pass

            except asyncio.TimeoutError:
                try:
                    await msg.clear_reactions()
                except discord.Forbidden:
                    pass
                break


# ---------- SETUP ----------


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))