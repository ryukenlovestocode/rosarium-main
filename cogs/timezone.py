"""
Timezone & Time Management cog (hybrid: every command works as /slash and $prefix).

This file only contains commands, UI and user-facing text. Storage, validation
and formatting live in services/timezone_service.py; pure time maths lives in
utils/time.py.

Uses from config.py: EMBED_COLOR, EMBED_COLOR_DARK (errors), FOOTER_TEXT,
COMMAND_PREFIX. Optional config.py values (all have defaults):
    TIMEZONE_DATA_FILE     str   path of the JSON store (default data/timezones.json)
    WAKE_START_HOUR        int   start of "likely awake" window (default 7)
    WAKE_END_HOUR          int   end of "likely awake" window   (default 23)

Usage:
    timezone set <place> | view [@user] | remove | list | status     (alias: tz)
    time [@user] [time] [in <zone>]        e.g.  time 8pm   |   time @Sam 20:00   |   time 9am in Tokyo
    timein [@user]
    timefix <text>                         e.g.  timefix Meeting tomorrow at 8pm
"""

from __future__ import annotations

import logging
from datetime import timedelta
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from services.timezone_service import (
    InvalidTimezone,
    StorageError,
    TimezoneService,
    format_timezone,
    group_by_timezone,
    paginate_blocks,
    resolve_timezone_input,
    search_timezones,
    split_zone_clause,
)
from utils.time import (
    NonexistentTimeError,
    TimeParseError,
    day_shift_label,
    format_clock,
    format_discord_timestamp,
    format_long_date,
    get_current_time,
    get_zone,
    parse_when,
    resolve_local,
)

log = logging.getLogger("rosarium.timezone")

EMBED_COLOR: int = config.EMBED_COLOR
ERROR_COLOR: int = config.EMBED_COLOR_DARK
FOOTER_TEXT: str = config.FOOTER_TEXT
PREFIX: str = config.COMMAND_PREFIX
WAKE_START: int = getattr(config, "WAKE_START_HOUR", 7)
WAKE_END: int = getattr(config, "WAKE_END_HOUR", 23)
DATA_FILE = Path(
    getattr(config, "TIMEZONE_DATA_FILE", Path(__file__).resolve().parent.parent / "data" / "timezones.json")
)

MAX_NAMES_PER_ZONE = 20
MAX_TARGET_ZONES = 6

NOT_SET_HINT = f"Use `/timezone set` (or `{PREFIX}timezone set`) to tell Roselle your timezone."
USAGE = (
    f"`{PREFIX}timezone set <place>` — save your timezone\n"
    f"`{PREFIX}timezone view [@user]` — see a timezone\n"
    f"`{PREFIX}timezone remove` — remove yours\n"
    f"`{PREFIX}timezone list` — who is where\n"
    f"`{PREFIX}timezone status` — server overview\n\n"
    f"Also: `{PREFIX}time`, `{PREFIX}timein`, `{PREFIX}timefix` (all work as slash commands too)."
)


def _embed(title: str, description: str = "", *, color: int | None = None) -> discord.Embed:
    embed = discord.Embed(title=f"🌹 {title}", description=description, color=color or EMBED_COLOR)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def _names(guild: discord.Guild, user_ids: list[int]) -> list[str]:
    """Display names of users who are still in the guild (others are skipped)."""
    out = []
    for uid in user_ids:
        member = guild.get_member(uid)
        if member is not None:
            out.append(discord.utils.escape_markdown(member.display_name))
    return sorted(out, key=str.lower)


def _clean(member: discord.abc.User) -> str:
    return discord.utils.escape_markdown(member.display_name)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------


class PagedView(discord.ui.View):
    """Prev/Next buttons over a list of embed descriptions. Only the invoker can flip pages."""

    def __init__(self, owner_id: int, title: str, pages: list[str]):
        super().__init__(timeout=120)
        self.owner_id = owner_id
        self.title = title
        self.pages = pages
        self.index = 0
        self.message: discord.Message | None = None
        self._sync()

    def _sync(self) -> None:
        self.prev_button.disabled = self.index == 0
        self.next_button.disabled = self.index >= len(self.pages) - 1

    def build(self) -> discord.Embed:
        embed = _embed(self.title, self.pages[self.index])
        if len(self.pages) > 1:
            embed.set_footer(text=f"{FOOTER_TEXT} · Page {self.index + 1}/{len(self.pages)}")
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("Only the person who ran the command can flip pages.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.build(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.build(), view=self)

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True  # type: ignore[attr-defined]
        if self.message:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class ConfirmView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=30)
        self.owner_id = owner_id
        self.confirmed: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("This confirmation isn't for you.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.confirmed = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Keep it", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        self.confirmed = False
        await interaction.response.defer()
        self.stop()


# --------------------------------------------------------------------------
# Cog
# --------------------------------------------------------------------------


class TimezoneCog(commands.Cog, name="Timezone"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.service = TimezoneService(DATA_FILE)

    # -- shared helpers ----------------------------------------------------

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.guild is not None

    async def _send(
        self,
        ctx: commands.Context,
        embed: discord.Embed,
        *,
        private: bool = False,
        view: discord.ui.View | None = None,
    ) -> discord.Message:
        """Send an embed. ``private`` = ephemeral for slash; for prefix (which can't
        do ephemeral) it's a self-deleting message so errors don't clutter the channel."""
        kwargs: dict = {"embed": embed}
        if view is not None:
            kwargs["view"] = view
        if private:
            kwargs["ephemeral"] = True
            if ctx.interaction is None:
                kwargs["delete_after"] = 30
        return await ctx.send(**kwargs)

    async def _error(self, ctx: commands.Context, title: str, text: str) -> None:
        await self._send(ctx, _embed(title, text, color=ERROR_COLOR), private=True)

    async def _zone_autocomplete(self, _: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        try:
            return [app_commands.Choice(name=e.label, value=e.zone) for e in search_timezones(current, limit=25)]
        except Exception:  # noqa: BLE001 - autocomplete must never raise into Discord
            log.exception("Timezone autocomplete failed")
            return []

    async def _guild_zone_counts(self, guild: discord.Guild) -> dict[str, list[int]]:
        """timezone -> [user ids], limited to people currently in this guild."""
        everyone = await self.service.all_user_timezones()
        present = {uid: z for uid, z in everyone.items() if guild.get_member(uid) is not None}
        return group_by_timezone(present)

    async def _send_pages(self, ctx: commands.Context, title: str, pages: list[str]) -> None:
        view = PagedView(ctx.author.id, title, pages)
        if len(pages) == 1:
            await ctx.send(embed=view.build())
            return
        view.message = await ctx.send(embed=view.build(), view=view)

    # -- timezone group ----------------------------------------------------

    @commands.hybrid_group(
        name="timezone",
        aliases=["tz"],
        invoke_without_command=True,
        description="Save and look up timezones",
    )
    @commands.guild_only()
    async def tz(self, ctx: commands.Context):
        """Bare `$timezone` just shows what's available."""
        await ctx.send(embed=_embed("Timezones", USAGE))

    # /timezone set ---------------------------------------------------------

    @tz.command(name="set", description="Save your timezone (search by city or country)")
    @app_commands.describe(timezone="e.g. India, Kolkata, New York, London, Asia/Tokyo")
    async def tz_set(self, ctx: commands.Context, *, timezone: str):
        zone, suggestions = resolve_timezone_input(timezone)
        if zone is None:
            if suggestions:
                lines = "\n".join(f"• {s.label}" for s in suggestions)
                text = f"I wasn't sure which one you meant. Try one of these:\n{lines}"
            else:
                text = "I couldn't find that timezone. Try a city or country name, like `Kolkata`, `New York` or `Asia/Tokyo`."
            return await self._error(ctx, "Timezone Not Found", text)

        try:
            zone = await self.service.set_user_timezone(ctx.author.id, zone)
        except InvalidTimezone:
            return await self._error(ctx, "Timezone Not Found", "That isn't a valid timezone.")

        now = get_current_time(zone)
        info = format_timezone(zone, now)
        await ctx.send(
            embed=_embed(
                "Timezone Updated",
                f"**Timezone:** {info.zone}\n**Current time:** {format_clock(now)}\n**Offset:** {info.offset}\n\n"
                "Your timezone has been saved.\nUse `/timezone view` anytime to see it.",
            )
        )

    @tz_set.autocomplete("timezone")
    async def tz_set_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._zone_autocomplete(interaction, current)

    # /timezone view --------------------------------------------------------

    @tz.command(name="view", description="See your timezone, or another member's")
    @app_commands.describe(user="Member to look up (defaults to you)")
    async def tz_view(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target = user or ctx.author
        zone = await self.service.get_user_timezone(target.id)
        if zone is None:
            if target.id == ctx.author.id:
                text = f"You haven't set a timezone yet.\n\n{NOT_SET_HINT}"
            else:
                text = (
                    f"**{_clean(target)}** hasn't configured a timezone yet.\n\n"
                    f"You can let them know to use:\n`/timezone set`"
                )
            return await self._send(ctx, _embed("Timezone Not Set", text), private=True)

        now = get_current_time(zone)
        info = format_timezone(zone, now)
        await ctx.send(
            embed=_embed(
                f"{_clean(target)}’s Timezone",
                f"{info.label}\n{info.abbr_offset}\n\n"
                f"**Current local time**\n🕘 {format_clock(now)}\n\n"
                f"**Local date**\n{format_long_date(now)}",
            )
        )

    # /timezone remove ------------------------------------------------------

    @tz.command(name="remove", description="Remove your saved timezone")
    async def tz_remove(self, ctx: commands.Context):
        zone = await self.service.get_user_timezone(ctx.author.id)
        if zone is None:
            return await self._send(
                ctx, _embed("Nothing to Remove", f"You don't have a saved timezone.\n\n{NOT_SET_HINT}"), private=True
            )
        view = ConfirmView(ctx.author.id)
        message = await ctx.send(
            embed=_embed("Remove Timezone?", f"Remove your saved timezone (**{zone}**)?"),
            view=view,
            ephemeral=True,
        )
        await view.wait()
        if view.confirmed:
            try:
                await self.service.remove_user_timezone(ctx.author.id)
            except StorageError:
                embed = _embed(
                    "Something Went Wrong", "I couldn't remove that just now. Please try again in a moment.", color=ERROR_COLOR
                )
            else:
                embed = _embed("Timezone Removed", "Your saved timezone has been removed.")
        elif view.confirmed is False:
            embed = _embed("Kept", "No changes made — your timezone is still saved.")
        else:
            embed = _embed("Timed Out", "No changes made.")
        try:
            await message.edit(embed=embed, view=None)
        except discord.HTTPException:
            log.warning("Could not update the remove-confirmation message")

    # /timezone list --------------------------------------------------------

    @tz.command(name="list", description="Who is in which timezone")
    async def tz_list(self, ctx: commands.Context):
        assert ctx.guild is not None
        groups = await self._guild_zone_counts(ctx.guild)
        if not groups:
            return await ctx.send(embed=_embed("Rosarium Timezones", f"Nobody has set a timezone yet.\n\n{NOT_SET_HINT}"))
        blocks = []
        for zone, ids in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            names = _names(ctx.guild, ids)
            shown = names[:MAX_NAMES_PER_ZONE]
            if len(names) > len(shown):
                shown.append(f"…and {len(names) - len(shown)} more")
            blocks.append(f"{format_timezone(zone).label}\n" + "\n".join(shown))
        await self._send_pages(ctx, "Rosarium Timezones", paginate_blocks(blocks))

    # /timezone status ------------------------------------------------------

    @tz.command(name="status", description="Overview of where Rosarium's members are right now")
    async def tz_status(self, ctx: commands.Context):
        assert ctx.guild is not None
        groups = await self._guild_zone_counts(ctx.guild)
        if not groups:
            return await ctx.send(
                embed=_embed("Rosarium — Timezone Status", f"Nobody has set a timezone yet.\n\n{NOT_SET_HINT}")
            )

        awake, asleep = [], []
        for zone, ids in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            now = get_current_time(zone)
            info = format_timezone(zone, now)
            count = len(_names(ctx.guild, ids))
            block = f"{info.friendly}\n{format_clock(now)}\n{count} member{'s' if count != 1 else ''}"
            (awake if WAKE_START <= now.hour < WAKE_END else asleep).append(block)

        blocks = []
        if awake:
            blocks.append("🟢 **Likely awake**")
            blocks.extend(awake)
        if asleep:
            blocks.append("🌙 **Likely sleeping**")
            blocks.extend(asleep)
        pages = paginate_blocks(blocks, max_blocks=10)
        pages[-1] += f"\n\n*Waking hours are assumed to be {WAKE_START:02d}:00–{WAKE_END:02d}:00 — just an estimate.*"
        await self._send_pages(ctx, "Rosarium — Timezone Status", pages)

    # -- timein ------------------------------------------------------------

    @commands.hybrid_command(name="timein", description="What time is it for a member?")
    @commands.guild_only()
    @app_commands.describe(user="Member to check (defaults to you)")
    async def timein(self, ctx: commands.Context, user: Optional[discord.Member] = None):
        target = user or ctx.author
        zone = await self.service.get_user_timezone(target.id)
        if zone is None:
            who = "You haven't" if target.id == ctx.author.id else f"**{_clean(target)}** hasn't"
            return await self._send(
                ctx, _embed(_clean(target), f"{who} set a timezone yet.\n\n{NOT_SET_HINT}"), private=True
            )
        now = get_current_time(zone)
        info = format_timezone(zone, now)
        await ctx.send(
            embed=_embed(
                _clean(target),
                f"🕘 **{format_clock(now)}**\n{info.label}\n{info.abbr_offset}\n📅 {format_long_date(now)}",
            )
        )

    # -- time --------------------------------------------------------------

    @commands.hybrid_command(name="time", description="Convert a time between timezones")
    @commands.guild_only()
    @app_commands.describe(
        user="Read the time in this member's timezone (defaults to yours)",
        time="e.g. 8pm, 20:00, tomorrow 9:30am, or '9am in Tokyo'. Leave empty for 'now'",
    )
    async def time_cmd(
        self,
        ctx: commands.Context,
        user: Optional[discord.Member] = None,
        *,
        time: Optional[str] = None,
    ):
        assert ctx.guild is not None
        time_text, zone_text = split_zone_clause(time)

        # 1. Which timezone is the input in?
        if zone_text:
            source, _ = resolve_timezone_input(zone_text)
            if source is None:
                return await self._error(
                    ctx, "Timezone Not Found", "I couldn't find that timezone. Try a city or country, like `9am in Tokyo`."
                )
        else:
            owner = user or ctx.author
            source = await self.service.get_user_timezone(owner.id)
            if source is None:
                if owner.id == ctx.author.id:
                    text = f"You haven't set a timezone yet. {NOT_SET_HINT}\nOr name one: `9am in Tokyo`."
                else:
                    text = f"**{_clean(owner)}** hasn't set a timezone yet."
                return await self._error(ctx, "Timezone Not Set", text)

        src_zone = get_zone(source)
        assert src_zone is not None
        now_src = get_current_time(src_zone)

        # 2. Which moment?
        note = ""
        if time_text:
            try:
                parsed = parse_when(time_text, now_src.date())
                moment, ambiguous = resolve_local(parsed.as_naive(), src_zone)
            except TimeParseError as exc:
                return await self._error(ctx, "I Couldn't Read That Time", str(exc))
            except NonexistentTimeError:
                return await self._error(
                    ctx,
                    "That Time Doesn't Exist",
                    "The clocks skip that hour on that date (daylight saving). Try a time just before or after.",
                )
            if ambiguous:
                note = "\n*That hour happens twice on that date (clocks go back); I used the first one.*"
        else:
            moment = now_src

        # 3. Targets: the invoker's zone + the server's most common zones, minus the source
        groups = await self._guild_zone_counts(ctx.guild)
        ranked = [z for z, _ in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))]
        mine = await self.service.get_user_timezone(ctx.author.id)
        targets: list[str] = ([mine] if mine else []) + ranked
        targets = [z for z in dict.fromkeys(targets) if z != source][:MAX_TARGET_ZONES]

        src_info = format_timezone(source, moment)
        lines = [
            f"**Input**\n{format_clock(moment)} {src_info.abbr or src_info.offset} · {format_long_date(moment)}{note}",
            f"{src_info.friendly}\n{format_clock(moment)}",
        ]
        for z in targets:
            local = moment.astimezone(get_zone(z))  # type: ignore[arg-type]
            shift = day_shift_label(local, moment)
            info = format_timezone(z, local)
            lines.append(f"{info.friendly}\n{format_clock(local)}{' ' + shift if shift else ''}")
        if not targets:
            lines.append("*Nobody else in the server has set a timezone yet, so there's nothing to convert to.*")
        lines.append(
            f"**In your own timezone**\n{format_discord_timestamp(moment, 'full')} ({format_discord_timestamp(moment, 'relative')})"
        )
        await ctx.send(embed=_embed("Time Conversion", "\n\n".join(lines)))

    # -- timefix -----------------------------------------------------------

    @commands.hybrid_command(
        name="timefix", description="Turn a written time into a Discord timestamp everyone sees correctly"
    )
    @commands.guild_only()
    @app_commands.describe(text="e.g. Meeting tomorrow at 8pm")
    async def timefix(self, ctx: commands.Context, *, text: str):
        zone = await self.service.get_user_timezone(ctx.author.id)
        if zone is None:
            return await self._error(ctx, "Timezone Not Set", f"I need to know your timezone to read that. {NOT_SET_HINT}")
        src_zone = get_zone(zone)
        assert src_zone is not None
        now = get_current_time(src_zone)

        try:
            parsed = parse_when(text, now.date())
            moment, ambiguous = resolve_local(parsed.as_naive(), src_zone)
        except TimeParseError as exc:
            return await self._error(ctx, "I Couldn't Read That", f"{exc}\nTry something like `Meeting tomorrow at 8pm`.")
        except NonexistentTimeError:
            return await self._error(
                ctx,
                "That Time Doesn't Exist",
                "The clocks skip that hour on that date (daylight saving). Try a time just before or after.",
            )

        rolled = ""
        if moment <= now:
            if parsed.explicit_day:
                return await self._error(
                    ctx, "That's In The Past", "That time has already passed. Give me a date and time in the future."
                )
            try:
                moment, ambiguous = resolve_local(parsed.as_naive() + timedelta(days=1), src_zone)
            except NonexistentTimeError:
                return await self._error(
                    ctx, "That Time Doesn't Exist", "Tomorrow's clocks skip that hour (daylight saving). Try a slightly different time."
                )
            rolled = "\n*That time already passed today, so I used tomorrow.*"

        full = format_discord_timestamp(moment, "full")
        rel = format_discord_timestamp(moment, "relative")
        title = parsed.title or "Event"
        body = (
            f"🗓️ **{discord.utils.escape_markdown(title)}**\n\n{full}\n{rel}{rolled}"
            + ("\n*That hour happens twice that day; I used the first one.*" if ambiguous else "")
            + f"\n\nCopy-paste:\n```{full}\n{rel}```"
        )
        await ctx.send(embed=_embed("Timestamp Ready", body))

    # -- errors ------------------------------------------------------------

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        original = getattr(error, "original", error)
        if isinstance(error, (commands.NoPrivateMessage, commands.CheckFailure)):
            return  # server-only; the bot already stays silent in DMs
        if isinstance(error, (commands.MemberNotFound, commands.BadArgument)):
            title, text = "Member Not Found", "I couldn't find that member. Mention them or use their exact name."
        elif isinstance(error, commands.MissingRequiredArgument):
            title, text = "Missing Something", f"You need to include `{error.param.name}`.\n\n{USAGE}"
        elif isinstance(original, StorageError):
            log.error("Timezone storage failure in %s: %s", ctx.command, original)
            title, text = "Something Went Wrong", "I couldn't save that just now. Please try again in a moment."
        else:
            log.error("Unhandled error in %s", ctx.command, exc_info=original)
            title, text = "Something Went Wrong", "Something went wrong on my end. Please try again in a moment."
        try:
            await self._error(ctx, title, text)
        except discord.HTTPException:
            log.warning("Could not deliver error message to user")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TimezoneCog(bot))