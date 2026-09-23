"""
Welcomer cog — posts a decorated embed to a welcome channel whenever
someone joins the server, and (optionally) a goodbye embed to a
configurable channel whenever someone leaves.

Commands:
  welctest — [owner only] preview the welcome embed in the current
             channel using yourself as the joining member, without
             needing to actually leave and rejoin the server to test it

Slash commands (need Manage Server):
  /leavelog set <channel> -> choose the channel leave notifications go to
  /leavelog disable       -> turn leave notifications off for this server
  /leavelog status        -> show the current setting

Storage layout (cogs/data/leave_channels.json):
{
  "<guild_id>": <channel_id>,
  ...
}
A guild simply has no key (or a null value) when leave notifications are off.

Set WELCOME_CHANNEL_ID in .env or this cog has nowhere to post welcome
messages and stays silent (rather than crashing). The four channel-mention
vars (RULES_, UPDATES_, GENERAL_, ROLES_CHANNEL_ID) are optional on top of
that — set any of them and that line becomes a real clickable channel
mention; leave one unset and it falls back to plain "#rules"-style text.

Design notes:
  - The member count ("you're our 529th member!") and the timestamp
    ("Today at 9:06 AM") are NOT hardcoded — the count reads live from
    guild.member_count and the timestamp comes from Discord's own embed
    timestamp rendering (embed.timestamp).
  - Colour is a soft pink rather than Rosarium's usual blood
    red/near-black brand pair (config.EMBED_COLOR / EMBED_COLOR_DARK).
    Swap WELCOME_COLOR below for config.EMBED_COLOR to match the rest of
    the bot. Leave embeds use a muted lavender (LEAVE_COLOR).
  - WELCOME_GIF_URL must be a real https URL Discord can fetch. Left
    blank, the embed just skips the image.
  - The leave channel is configured per-server with a slash command (stored
    in JSON) rather than .env, so it can differ between servers and be
    changed without restarting the bot.
"""

from __future__ import annotations

import logging
import os

import discord
from discord import app_commands
from discord.ext import commands

import config
from storage import JSONStore

logger = logging.getLogger("rosarium.welcomer")

LEAVE_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "leave_channels.json")

# Deliberately not one of Rosarium's brand colors — see module docstring.
WELCOME_COLOR = 0xFFC0DC
LEAVE_COLOR = 0xC9B8D6

# Paste a direct GIF/image URL here (or leave blank to skip the image).
WELCOME_GIF_URL = "https://static2.klipy.com/ii/c3a19a0b747a76e98651f2b9a3cca5ff/51/ff/9zcdNbN1.gif"


def _ordinal(n: int) -> str:
    """1 -> '1st', 2 -> '2nd', 3 -> '3rd', 11 -> '11th', 529 -> '529th', ..."""
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def _channel_ref(guild: discord.Guild, channel_id: int | None, fallback: str) -> str:
    """A clickable mention if the channel is configured and still exists, else plain text."""
    if channel_id:
        channel = guild.get_channel(channel_id)
        if channel is not None:
            return channel.mention
    return fallback


def _build_welcome_embed(member: discord.Member) -> discord.Embed:
    """Builds the welcome embed for a given member. Shared by on_member_join and .welctest."""
    rules = _channel_ref(member.guild, config.RULES_CHANNEL_ID, "#rules")
    notifs = _channel_ref(member.guild, config.UPDATES_CHANNEL_ID, "#notifs")
    chat = _channel_ref(member.guild, config.GENERAL_CHANNEL_ID, "#chat")
    hex_channel = _channel_ref(member.guild, config.ROLES_CHANNEL_ID, "#hex")

    member_count = _ordinal(member.guild.member_count)

    embed = discord.Embed(
        title="Welcome to Rosarium!",
        description=(
            f"welcome, {member.mention}! so glad you're here.\n\n"
            "make sure to check these channels out:\n"
            f"   • {rules}\n"
            f"   • {notifs}\n"
            f"   • {hex_channel}\n"
            f"   • {chat}\n\n"
            "thanks for joining us <3\n\n"
            f"you're our **{member_count}** member!"
        ),
        color=WELCOME_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    if WELCOME_GIF_URL:
        embed.set_image(url=WELCOME_GIF_URL)
    embed.set_footer(text=f"{config.BOT_NAME} • member #{member.guild.member_count}")
    return embed


def _build_leave_embed(member: discord.Member) -> discord.Embed:
    """Builds the goodbye embed for a member who just left."""
    lines = [f"{member.mention} (`{member}`) has left the server."]

    if member.joined_at is not None:
        lines.append(f"they joined {discord.utils.format_dt(member.joined_at, 'R')}.")

    roles = [r.mention for r in reversed(member.roles) if not r.is_default()]
    if roles:
        shown = roles[:10]
        extra = len(roles) - len(shown)
        text = " ".join(shown) + (f" +{extra} more" if extra > 0 else "")
        lines.append(f"roles: {text}")

    embed = discord.Embed(
        title="Member left",
        description="\n".join(lines),
        color=LEAVE_COLOR,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=str(member), icon_url=member.display_avatar.url)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text=f"{config.BOT_NAME} • {member.guild.member_count} members remain • ID {member.id}")
    return embed


class Welcomer(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.leave_store = JSONStore(LEAVE_DATA_PATH, default={})

    # ------------------------------------------------------------------
    # Leave-channel storage helpers
    # ------------------------------------------------------------------

    def _get_leave_channel_id(self, guild_id: int) -> int | None:
        return self.leave_store.get(str(guild_id), None)

    def _set_leave_channel_id(self, guild_id: int, channel_id: int | None) -> None:
        self.leave_store.set(str(guild_id), channel_id)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if config.WELCOME_CHANNEL_ID is None:
            return

        channel = self.bot.get_channel(config.WELCOME_CHANNEL_ID)
        if channel is None:
            return

        embed = _build_welcome_embed(member)
        await channel.send(content=member.mention, embed=embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        channel_id = self._get_leave_channel_id(member.guild.id)
        if channel_id is None:
            return

        channel = member.guild.get_channel(channel_id)
        if channel is None:
            logger.warning(
                "Leave channel %s for guild %s no longer exists; skipping.",
                channel_id,
                member.guild.id,
            )
            return

        try:
            await channel.send(embed=_build_leave_embed(member))
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to post leave message in channel %s (guild %s).",
                channel_id,
                member.guild.id,
            )
        except discord.HTTPException as e:
            logger.warning(
                "Failed to post leave message in guild %s: %s", member.guild.id, e
            )

    # ------------------------------------------------------------------
    # Owner test command
    # ------------------------------------------------------------------

    @commands.hybrid_command(
        name="welctest",
        description="[Owner only] Preview the welcome message using yourself as the joining member.",
    )
    @commands.is_owner()
    async def welctest(self, ctx: commands.Context):
        embed = _build_welcome_embed(ctx.author)
        await ctx.send(
            content=f"**Test welcome message** (posting here, not the real welcome channel):",
        )
        await ctx.send(content=ctx.author.mention, embed=embed)

    @welctest.error
    async def welctest_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.NotOwner):
            await ctx.send("This command is owner-only.", ephemeral=True)
        else:
            raise error

    # ------------------------------------------------------------------
    # /leavelog — configure where leave notifications go
    # ------------------------------------------------------------------

    leavelog_group = app_commands.Group(
        name="leavelog",
        description="Configure the channel that gets notified when a member leaves",
        default_permissions=discord.Permissions(manage_guild=True),
        guild_only=True,
    )

    @leavelog_group.command(name="set", description="Set the channel for leave notifications")
    @app_commands.describe(channel="Where to post a message when someone leaves")
    async def leavelog_set(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        guild = interaction.guild
        assert guild is not None

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I need **View Channel**, **Send Messages** and **Embed Links** in "
                f"{channel.mention} to post leave notifications there.",
                ephemeral=True,
            )
            return

        self._set_leave_channel_id(guild.id, channel.id)
        await interaction.response.send_message(
            f"Leave notifications will now be posted in {channel.mention}.", ephemeral=True
        )

    @leavelog_group.command(name="disable", description="Stop posting leave notifications")
    async def leavelog_disable(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        if self._get_leave_channel_id(guild.id) is None:
            await interaction.response.send_message(
                "Leave notifications are already disabled for this server.", ephemeral=True
            )
            return

        self._set_leave_channel_id(guild.id, None)
        await interaction.response.send_message("Leave notifications disabled.", ephemeral=True)

    @leavelog_group.command(name="status", description="Show the current leave notification channel")
    async def leavelog_status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        channel_id = self._get_leave_channel_id(guild.id)
        if channel_id is None:
            await interaction.response.send_message(
                "Leave notifications are currently disabled.", ephemeral=True
            )
            return

        channel = guild.get_channel(channel_id)
        channel_txt = channel.mention if channel else f"`deleted-channel:{channel_id}`"
        await interaction.response.send_message(
            f"Leave notifications are posted in {channel_txt}.", ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Welcomer(bot))