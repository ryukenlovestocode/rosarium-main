"""
AutoRole cog for Roselle (Rosarium).

Assigns one configured role to every member automatically when they join.

Commands:
- /autorole set      -> set (or change) the role given to new members
- /autorole disable  -> turn autorole off for this server
- /autorole status   -> show the current setting

Storage layout (cogs/data/autorole.json):
{
  "<guild_id>": <role_id>,
  ...
}

A guild simply has no key (or a null value) when autorole is disabled.
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


class AutoRole(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JSONStore(DATA_PATH, default={})

    def _get_role_id(self, guild_id: int) -> Optional[int]:
        return self.store.get(str(guild_id), None)

    def _set_role_id(self, guild_id: int, role_id: Optional[int]) -> None:
        self.store.set(str(guild_id), role_id)

    autorole_group = app_commands.Group(
        name="autorole",
        description="Automatically assign a role to new members",
        default_permissions=discord.Permissions(manage_roles=True),
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

    @autorole_group.command(name="status", description="Show the current autorole setting")
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        role_id = self._get_role_id(guild.id)
        if role_id is None:
            await interaction.response.send_message(
                "Autorole is currently disabled.", ephemeral=True
            )
            return

        role = guild.get_role(role_id)
        role_txt = role.mention if role else f"`deleted-role:{role_id}`"
        await interaction.response.send_message(
            f"New members currently get {role_txt}.", ephemeral=True
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            # Bots joining (e.g. via an OAuth invite) skip autorole — they
            # usually come with their own permissions/role already, and
            # granting a member role to them is rarely intended.......
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
            return

        try:
            await member.add_roles(role, reason="Autorole on join")
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to assign autorole %s in guild %s.",
                role.id,
                member.guild.id,
            )
        except discord.HTTPException as e:
            logger.warning(
                "Failed to assign autorole %s to %s in guild %s: %s",
                role.id,
                member.id,
                member.guild.id,
                e,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AutoRole(bot))