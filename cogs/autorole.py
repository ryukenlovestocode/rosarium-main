"""
AutoRole cog for Roselle (Rosarium).

Assigns one configured role to every member automatically when they join,
and optionally posts a notification to a configured channel each time it
does (or fails to).

Commands:
- /autorole set              -> set (or change) the role given to new members
- /autorole disable          -> turn autorole off for this server
- /autorole status           -> show the current settings
- /autorole log set          -> set the channel autorole notifications go to
- /autorole log disable      -> stop posting autorole notifications

Storage layout:

cogs/data/autorole.json  (unchanged, so existing data keeps working)
{
  "<guild_id>": <role_id>,
  ...
}

cogs/data/autorole_log.json
{
  "<guild_id>": <channel_id>,
  ...
}

A guild simply has no key (or a null value) when the setting is disabled.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from storage import JSONStore

logger = logging.getLogger("rosarium.autorole")

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "autorole.json")
LOG_DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "autorole_log.json")

SUCCESS_COLOR = 0xFFC0DC
FAIL_COLOR = 0xE05A5A


class AutoRole(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JSONStore(DATA_PATH, default={})
        self.log_store = JSONStore(LOG_DATA_PATH, default={})

    def _get_role_id(self, guild_id: int) -> Optional[int]:
        return self.store.get(str(guild_id), None)

    def _set_role_id(self, guild_id: int, role_id: Optional[int]) -> None:
        self.store.set(str(guild_id), role_id)

    def _get_log_channel_id(self, guild_id: int) -> Optional[int]:
        return self.log_store.get(str(guild_id), None)

    def _set_log_channel_id(self, guild_id: int, channel_id: Optional[int]) -> None:
        self.log_store.set(str(guild_id), channel_id)

    async def _notify(self, guild: discord.Guild, embed: discord.Embed) -> None:
        """Post an embed to the configured log channel, if there is one. Never raises."""
        channel_id = self._get_log_channel_id(guild.id)
        if channel_id is None:
            return

        channel = guild.get_channel(channel_id)
        if channel is None:
            logger.warning(
                "Autorole log channel %s for guild %s no longer exists; skipping.",
                channel_id,
                guild.id,
            )
            return

        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to post in autorole log channel %s (guild %s).",
                channel_id,
                guild.id,
            )
        except discord.HTTPException as e:
            logger.warning(
                "Failed to post autorole notification in guild %s: %s", guild.id, e
            )

    autorole_group = app_commands.Group(
        name="autorole",
        description="Automatically assign a role to new members",
        default_permissions=discord.Permissions(manage_roles=True),
    )

    log_group = app_commands.Group(
        name="log",
        description="Choose where autorole notifications are posted",
        parent=autorole_group,
    )

    @autorole_group.command(name="set", description="Set the role new members get automatically")
    @app_commands.describe(role="The role to assign on join")
    async def set(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = interaction.guild
        assert guild is not None

        if role.is_default():
            await interaction.response.send_message(
                "Can't use @everyone as the autorole.", ephemeral=True
            )
            return

        if role.managed:
            await interaction.response.send_message(
                f"{role.mention} is managed by an integration (e.g. a bot or booster "
                "role) and can't be assigned manually.",
                ephemeral=True,
            )
            return

        if role >= guild.me.top_role:
            await interaction.response.send_message(
                f"I can't assign {role.mention} — it's the same as or higher than my "
                "own top role. Move my role above it in Server Settings > Roles.",
                ephemeral=True,
            )
            return

        self._set_role_id(guild.id, role.id)
        await interaction.response.send_message(
            f"New members will now automatically get {role.mention}.", ephemeral=True
        )

    @autorole_group.command(name="disable", description="Stop automatically assigning a role to new members")
    async def disable(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        if self._get_role_id(guild.id) is None:
            await interaction.response.send_message(
                "Autorole is already disabled for this server.", ephemeral=True
            )
            return

        self._set_role_id(guild.id, None)
        await interaction.response.send_message("Autorole disabled.", ephemeral=True)

    @autorole_group.command(name="status", description="Show the current autorole settings")
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        role_id = self._get_role_id(guild.id)
        if role_id is None:
            role_line = "Autorole is currently disabled."
        else:
            role = guild.get_role(role_id)
            role_txt = role.mention if role else f"`deleted-role:{role_id}`"
            role_line = f"New members currently get {role_txt}."

        channel_id = self._get_log_channel_id(guild.id)
        if channel_id is None:
            log_line = "Notifications are off (no log channel set)."
        else:
            channel = guild.get_channel(channel_id)
            channel_txt = channel.mention if channel else f"`deleted-channel:{channel_id}`"
            log_line = f"Notifications are posted in {channel_txt}."

        await interaction.response.send_message(
            f"{role_line}\n{log_line}", ephemeral=True
        )

    @log_group.command(name="set", description="Set the channel autorole notifications are posted in")
    @app_commands.describe(channel="Where to post a message each time a role is auto-assigned")
    async def log_set(
        self, interaction: discord.Interaction, channel: discord.TextChannel
    ) -> None:
        guild = interaction.guild
        assert guild is not None

        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.send_messages and perms.embed_links):
            await interaction.response.send_message(
                f"I need **View Channel**, **Send Messages** and **Embed Links** in "
                f"{channel.mention} to post autorole notifications there.",
                ephemeral=True,
            )
            return

        self._set_log_channel_id(guild.id, channel.id)
        await interaction.response.send_message(
            f"Autorole notifications will now be posted in {channel.mention}.",
            ephemeral=True,
        )

    @log_group.command(name="disable", description="Stop posting autorole notifications")
    async def log_disable(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        if self._get_log_channel_id(guild.id) is None:
            await interaction.response.send_message(
                "Autorole notifications are already disabled for this server.",
                ephemeral=True,
            )
            return

        self._set_log_channel_id(guild.id, None)
        await interaction.response.send_message(
            "Autorole notifications disabled.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            # Bots joining (e.g. via an OAuth invite) skip autorole — they
            # usually come with their own permissions/role already, and
            # granting a member role to them is rarely intended.
            return

        role_id = self._get_role_id(member.guild.id)
        if role_id is None:
            return

        role = member.guild.get_role(role_id)
        if role is None:
            logger.warning(
                "Autorole for guild %s points at a deleted role (%s); skipping.",
                member.guild.id,
                role_id,
            )
            await self._notify(
                member.guild,
                self._build_embed(
                    member,
                    title="Autorole failed",
                    description=(
                        f"couldn't give {member.mention} a role: the configured autorole "
                        f"(`{role_id}`) no longer exists. Use `/autorole set` to pick a new one."
                    ),
                    color=FAIL_COLOR,
                ),
            )
            return

        try:
            await member.add_roles(role, reason="Autorole on join")
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to assign autorole %s in guild %s.",
                role.id,
                member.guild.id,
            )
            await self._notify(
                member.guild,
                self._build_embed(
                    member,
                    title="Autorole failed",
                    description=(
                        f"couldn't give {member.mention} the role {role.mention}: I'm missing "
                        "permissions. Make sure I have **Manage Roles** and my top role sits "
                        "above it."
                    ),
                    color=FAIL_COLOR,
                ),
            )
        except discord.HTTPException as e:
            logger.warning(
                "Failed to assign autorole %s to %s in guild %s: %s",
                role.id,
                member.id,
                member.guild.id,
                e,
            )
            await self._notify(
                member.guild,
                self._build_embed(
                    member,
                    title="Autorole failed",
                    description=f"couldn't give {member.mention} the role {role.mention}: `{e}`",
                    color=FAIL_COLOR,
                ),
            )
        else:
            await self._notify(
                member.guild,
                self._build_embed(
                    member,
                    title="Autorole assigned",
                    description=f"gave {role.mention} to {member.mention} on join.",
                    color=SUCCESS_COLOR,
                ),
            )

    @staticmethod
    def _build_embed(
        member: discord.Member, *, title: str, description: str, color: int
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.set_footer(text=f"User ID {member.id}")
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoRole(bot))