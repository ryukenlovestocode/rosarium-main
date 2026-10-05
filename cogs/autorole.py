"""
AutoRole cog for Roselle (Rosarium).

Assigns the configured roles (up to MAX_ROLES) to every member
automatically when they join, and optionally posts a notification to a
configured channel each time it does (or fails to).

Commands:
- /autorole add <role>       -> add a role to the list new members get
- /autorole remove <role>    -> take a role off that list
- /autorole disable          -> turn autorole off for this server (clears the list)
- /autorole status           -> show the current settings
- /autorole log set          -> set the channel autorole notifications go to
- /autorole log disable      -> stop posting autorole notifications

Storage layout:

cogs/data/autorole.json  (same file as before, so existing data keeps working)
{
  "<guild_id>": [<role_id>, <role_id>, ...],
  ...
}

Older versions stored a single role id (`"<guild_id>": <role_id>`). That
form is still read correctly, and is rewritten as a list the next time the
guild's autoroles change.

cogs/data/autorole_log.json
{
  "<guild_id>": <channel_id>,
  ...
}

A guild simply has no key (or a null value) when the setting is disabled.

Joining behaviour: all usable roles are given in a single request. A role
that no longer exists is dropped from the list; a role the bot can't assign
(managed, or at/above the bot's top role) is skipped but stays configured.
Either way the remaining roles are still given, and the log channel (if
set) says what was skipped. This matters because Discord rejects the whole
request if even one role in it is out of the bot's reach.
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

# Most roles a server can have on the autorole list.
MAX_ROLES = 10


class AutoRole(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JSONStore(DATA_PATH, default={})
        self.log_store = JSONStore(LOG_DATA_PATH, default={})

    # ---------- STORAGE ----------

    def _get_role_ids(self, guild_id: int) -> list[int]:
        """The configured autorole ids, in the order they were added."""
        raw = self.store.get(str(guild_id), None)
        if raw is None:
            return []
        if isinstance(raw, int):  # legacy format: a single role id
            return [raw]
        if isinstance(raw, list):
            return list(dict.fromkeys(r for r in raw if isinstance(r, int)))
        return []

    def _set_role_ids(self, guild_id: int, role_ids: list[int]) -> None:
        self.store.set(str(guild_id), list(role_ids) if role_ids else None)

    def _get_log_channel_id(self, guild_id: int) -> Optional[int]:
        return self.log_store.get(str(guild_id), None)

    def _set_log_channel_id(self, guild_id: int, channel_id: Optional[int]) -> None:
        self.log_store.set(str(guild_id), channel_id)

    # ---------- HELPERS ----------

    @staticmethod
    def _assign_problem(role: discord.Role, me: discord.Member) -> Optional[str]:
        """Why the bot can't hand out this role, or None if it can."""
        if role.is_default():
            return "Can't use @everyone as an autorole."
        if role.managed:
            return (
                f"{role.mention} is managed by an integration (e.g. a bot or booster "
                "role) and can't be assigned manually."
            )
        if role >= me.top_role:
            return (
                f"I can't assign {role.mention} — it's the same as or higher than my "
                "own top role. Move my role above it in Server Settings > Roles."
            )
        return None

    @staticmethod
    def _format_roles(guild: discord.Guild, role_ids: list[int]) -> str:
        parts = []
        for role_id in role_ids:
            role = guild.get_role(role_id)
            parts.append(role.mention if role else f"`deleted-role:{role_id}`")
        return ", ".join(parts)

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

    # ---------- COMMANDS ----------

    autorole_group = app_commands.Group(
        name="autorole",
        description="Automatically assign roles to new members",
        default_permissions=discord.Permissions(manage_roles=True),
    )

    log_group = app_commands.Group(
        name="log",
        description="Choose where autorole notifications are posted",
        parent=autorole_group,
    )

    @autorole_group.command(name="add", description="Add a role that new members get automatically")
    @app_commands.describe(role="The role to give new members when they join")
    async def add(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = interaction.guild
        assert guild is not None

        role_ids = self._get_role_ids(guild.id)

        if role.id in role_ids:
            await interaction.response.send_message(
                f"{role.mention} is already given to new members.", ephemeral=True
            )
            return

        problem = self._assign_problem(role, guild.me)
        if problem is not None:
            await interaction.response.send_message(problem, ephemeral=True)
            return

        if len(role_ids) >= MAX_ROLES:
            await interaction.response.send_message(
                f"You can have at most **{MAX_ROLES}** autoroles. "
                "Remove one with `/autorole remove` first.",
                ephemeral=True,
            )
            return

        role_ids.append(role.id)
        self._set_role_ids(guild.id, role_ids)
        await interaction.response.send_message(
            f"Added {role.mention}. New members will now automatically get: "
            f"{self._format_roles(guild, role_ids)}.",
            ephemeral=True,
        )

    @autorole_group.command(name="remove", description="Stop giving a role to new members")
    @app_commands.describe(role="The role to take off the autorole list")
    async def remove(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = interaction.guild
        assert guild is not None

        role_ids = self._get_role_ids(guild.id)

        if role.id not in role_ids:
            await interaction.response.send_message(
                f"{role.mention} isn't one of the autoroles.", ephemeral=True
            )
            return

        role_ids.remove(role.id)
        self._set_role_ids(guild.id, role_ids)

        if role_ids:
            message = (
                f"Removed {role.mention}. New members still get: "
                f"{self._format_roles(guild, role_ids)}."
            )
        else:
            message = f"Removed {role.mention}. No autoroles are left, so autorole is now disabled."
        await interaction.response.send_message(message, ephemeral=True)

    @autorole_group.command(name="disable", description="Stop automatically assigning roles to new members")
    async def disable(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        role_ids = self._get_role_ids(guild.id)
        if not role_ids:
            await interaction.response.send_message(
                "Autorole is already disabled for this server.", ephemeral=True
            )
            return

        self._set_role_ids(guild.id, [])
        await interaction.response.send_message(
            f"Autorole disabled ({len(role_ids)} role(s) cleared).", ephemeral=True
        )

    @autorole_group.command(name="status", description="Show the current autorole settings")
    async def status(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        assert guild is not None

        role_ids = self._get_role_ids(guild.id)
        if not role_ids:
            role_line = "Autorole is currently disabled."
        else:
            role_line = f"New members currently get: {self._format_roles(guild, role_ids)}."

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
    @app_commands.describe(channel="Where to post a message each time roles are auto-assigned")
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

    # ---------- ON JOIN ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            # Bots joining (e.g. via an OAuth invite) skip autorole — they
            # usually come with their own permissions/role already, and
            # granting a member role to them is rarely intended.
            return

        guild = member.guild
        configured = self._get_role_ids(guild.id)
        if not configured:
            return

        # Sort the configured roles into: usable, deleted, and out of reach.
        # Discord rejects the whole request if any single role in it can't be
        # assigned, so unusable roles must be left out rather than sent along.
        roles: list[discord.Role] = []
        missing_ids: list[int] = []
        blocked: list[discord.Role] = []
        for role_id in configured:
            role = guild.get_role(role_id)
            if role is None:
                missing_ids.append(role_id)
            elif self._assign_problem(role, guild.me) is not None:
                blocked.append(role)
            else:
                roles.append(role)

        if missing_ids:
            # Deleted roles can't be picked in /autorole remove, so drop them
            # here. (No awaits since the read above, so nothing can interleave.)
            self._set_role_ids(
                guild.id, [rid for rid in configured if rid not in missing_ids]
            )
            logger.warning(
                "Autorole for guild %s listed deleted role(s) %s; removed them from the list.",
                guild.id,
                missing_ids,
            )

        notes: list[str] = []
        if missing_ids:
            ids_txt = ", ".join(f"`{rid}`" for rid in missing_ids)
            notes.append(f"Removed deleted role(s) from the autorole list: {ids_txt}.")
        if blocked:
            blocked_txt = ", ".join(r.mention for r in blocked)
            notes.append(
                f"Skipped (managed, or at/above my top role): {blocked_txt}. "
                "Move my role above them or use `/autorole remove`."
            )
        notes_txt = ("\n" + "\n".join(notes)) if notes else ""

        if not roles:
            logger.warning(
                "No assignable autoroles for guild %s (deleted: %s, blocked: %s).",
                guild.id,
                missing_ids,
                [r.id for r in blocked],
            )
            await self._notify(
                guild,
                self._build_embed(
                    member,
                    title="Autorole failed",
                    description=(
                        f"couldn't give {member.mention} any roles: none of the "
                        f"configured autoroles can be assigned.{notes_txt}"
                    ),
                    color=FAIL_COLOR,
                ),
            )
            return

        roles_txt = ", ".join(r.mention for r in roles)
        try:
            await member.add_roles(*roles, reason="Autorole on join")
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to assign autoroles %s in guild %s.",
                [r.id for r in roles],
                guild.id,
            )
            await self._notify(
                guild,
                self._build_embed(
                    member,
                    title="Autorole failed",
                    description=(
                        f"couldn't give {member.mention} the role(s) {roles_txt}: I'm missing "
                        "permissions. Make sure I have **Manage Roles** and my top role sits "
                        f"above them.{notes_txt}"
                    ),
                    color=FAIL_COLOR,
                ),
            )
        except discord.HTTPException as e:
            logger.warning(
                "Failed to assign autoroles %s to %s in guild %s: %s",
                [r.id for r in roles],
                member.id,
                guild.id,
                e,
            )
            await self._notify(
                guild,
                self._build_embed(
                    member,
                    title="Autorole failed",
                    description=(
                        f"couldn't give {member.mention} the role(s) {roles_txt}: `{e}`{notes_txt}"
                    ),
                    color=FAIL_COLOR,
                ),
            )
        else:
            await self._notify(
                guild,
                self._build_embed(
                    member,
                    title="Autorole partly assigned" if notes else "Autorole assigned",
                    description=f"gave {roles_txt} to {member.mention} on join.{notes_txt}",
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