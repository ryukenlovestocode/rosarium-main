"""
Tickets cog for Roselle (Rosarium).

Members click a persistent "Create Ticket" button and get a private
channel visible only to themselves, the configured staff role(s), and
bot owners (via config.OWNER_IDS / bot.owner_ids). Closing a ticket
requires a confirm step before the channel is deleted.

Setup flow (staff):
- /ticket setup category:<category> roles:<@Staff, @Mod, ...>
    Configures which category tickets are created under and which
    role(s) can see them, for this server.
- /ticket panel
    Posts an embed with a persistent "Create Ticket" button in the
    current channel. Works after bot restarts because the view is
    registered with a fixed custom_id and re-added on cog load.

Member flow:
- Click "Create Ticket" -> gets a private channel named ticket-<name>
- One open ticket per member at a time (enforced via stored state)
- Inside the ticket: "Close Ticket" button -> confirm -> channel deleted

Storage layout (cogs/data/tickets.json):
{
  "<guild_id>": {
    "category_id": <int>,
    "staff_role_ids": [<int>, ...],
    "active_tickets": {"<user_id>": <channel_id>}
  }
}
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from storage import JSONStore

logger = logging.getLogger("rosarium.tickets")

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "tickets.json")

EMBED_COLOR = config.EMBED_COLOR

# Fixed custom_ids so these buttons keep working after a bot restart —
# discord.py needs a persistent View re-registered with the same
# custom_id(s) at startup (see setup() at the bottom of this file).
# Namespaced "rosarium:" to avoid any collision with other cogs' buttons.
CREATE_TICKET_CUSTOM_ID = "rosarium:ticket:create"
CLOSE_TICKET_CUSTOM_ID = "rosarium:ticket:close"
CONFIRM_CLOSE_CUSTOM_ID = "rosarium:ticket:confirm_close"
CANCEL_CLOSE_CUSTOM_ID = "rosarium:ticket:cancel_close"


def safe_channel_name(display_name: str) -> str:
    """Discord channel names: lowercase, alphanumeric + hyphens only,
    max 100 chars. Strip anything else so ticket creation never fails
    because of a member's display name."""
    cleaned = "".join(c if c.isalnum() else "-" for c in display_name.lower())
    cleaned = "-".join(filter(None, cleaned.split("-")))  # collapse repeats
    cleaned = cleaned.strip("-") or "user"
    return f"ticket-{cleaned}"[:100]


class TicketsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JSONStore(DATA_PATH, default={})
        # Serializes ticket creation per-user within THIS process, closing
        # the race where two near-simultaneous clicks (or two interactions
        # firing close together) both read "no active ticket" before either
        # has saved, resulting in two channels and a clobbered storage
        # record. This does NOT protect against two separate bot processes
        # running at once — only one bot process should ever run at a time.
        self._creation_locks: dict[int, asyncio.Lock] = {}

    def _lock_for(self, user_id: int) -> asyncio.Lock:
        if user_id not in self._creation_locks:
            self._creation_locks[user_id] = asyncio.Lock()
        return self._creation_locks[user_id]

    # ---------------------------------------------------------------
    # internal helpers
    # ---------------------------------------------------------------

    def _guild_data(self, guild_id: int) -> dict:
        return self.store.get(
            str(guild_id),
            {"category_id": None, "staff_role_ids": [], "active_tickets": {}},
        )

    def _save_guild_data(self, guild_id: int, data: dict) -> None:
        self.store.set(str(guild_id), data)

    async def _is_owner_or_staff(
        self, interaction: discord.Interaction, gdata: dict
    ) -> bool:
        if await self.bot.is_owner(interaction.user):
            return True
        member_role_ids = {r.id for r in interaction.user.roles}
        return bool(member_role_ids & set(gdata.get("staff_role_ids", [])))

    async def _create_ticket_channel(
        self, interaction: discord.Interaction, gdata: dict
    ) -> Optional[discord.TextChannel]:
        guild = interaction.guild
        member = interaction.user

        category_id = gdata.get("category_id")
        category = guild.get_channel(category_id) if category_id else None
        if category is None or not isinstance(category, discord.CategoryChannel):
            await interaction.response.send_message(
                "Tickets aren't set up yet — an admin needs to run `/ticket setup` first.",
                ephemeral=True,
            )
            return None

        overwrites: dict = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True, send_messages=True, manage_channels=True
            ),
        }
        for role_id in gdata.get("staff_role_ids", []):
            role = guild.get_role(role_id)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )
        # Owners individually, in case they don't hold a staff role
        for owner_id in getattr(self.bot, "owner_ids", set()):
            owner_member = guild.get_member(owner_id)
            if owner_member:
                overwrites[owner_member] = discord.PermissionOverwrite(
                    view_channel=True, send_messages=True, read_message_history=True
                )

        try:
            channel = await guild.create_text_channel(
                name=safe_channel_name(member.display_name),
                category=category,
                overwrites=overwrites,
                reason=f"Ticket opened by {member} ({member.id})",
            )
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to create channels in that category.",
                ephemeral=True,
            )
            return None
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Couldn't create the ticket channel: {e}", ephemeral=True
            )
            return None

        return channel

    # ---------------------------------------------------------------
    # slash commands
    # ---------------------------------------------------------------

    ticket_group = app_commands.Group(
        name="ticket",
        description="Manage the ticket system",
        default_permissions=discord.Permissions(manage_channels=True),
    )

    @ticket_group.command(
        name="setup", description="Configure the category and staff roles for tickets"
    )
    @app_commands.describe(
        category="Category new ticket channels will be created under",
        staff_role_1="A role that can see all tickets",
        staff_role_2="Another staff role (optional)",
        staff_role_3="Another staff role (optional)",
    )
    async def setup_tickets(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        staff_role_1: discord.Role,
        staff_role_2: Optional[discord.Role] = None,
        staff_role_3: Optional[discord.Role] = None,
    ) -> None:
        guild_id = interaction.guild.id
        gdata = self._guild_data(guild_id)

        staff_roles = [staff_role_1] + [r for r in (staff_role_2, staff_role_3) if r]
        gdata["category_id"] = category.id
        gdata["staff_role_ids"] = [r.id for r in staff_roles]
        gdata.setdefault("active_tickets", {})
        self._save_guild_data(guild_id, gdata)

        role_list = ", ".join(r.mention for r in staff_roles)
        await interaction.response.send_message(
            f"Ticket setup saved.\nCategory: {category.mention}\nStaff roles: {role_list}\n"
            f"Bot owners always have access regardless of role.\n\n"
            f"Run `/ticket panel` in a channel to post the create-ticket button.",
            ephemeral=True,
        )

    @ticket_group.command(name="panel", description="Post the ticket-creation panel here")
    @app_commands.describe(
        title="Panel embed title", description="Panel embed description"
    )
    async def panel(
        self,
        interaction: discord.Interaction,
        title: str = "Need Help?",
        description: str = "Click the button below to open a private ticket with staff.",
    ) -> None:
        gdata = self._guild_data(interaction.guild.id)
        if not gdata.get("category_id"):
            await interaction.response.send_message(
                "Run `/ticket setup` first so tickets have somewhere to go.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
        embed.set_footer(text="Vesper Tickets")
        await interaction.response.send_message(
            embed=embed, view=TicketPanelView()
        )

    @ticket_group.command(name="close", description="Close the current ticket channel")
    async def close_command(self, interaction: discord.Interaction) -> None:
        """Text-command fallback for closing, in case the buttons on an
        old panel message ever go missing."""
        await self._handle_close_request(interaction)

    # ---------------------------------------------------------------
    # shared close-flow logic, used by both the button and /ticket close
    # ---------------------------------------------------------------

    async def _handle_close_request(self, interaction: discord.Interaction) -> None:
        guild_id = interaction.guild.id
        gdata = self._guild_data(guild_id)

        active = gdata.get("active_tickets", {})
        owning_user_id = next(
            (uid for uid, cid in active.items() if cid == interaction.channel.id), None
        )
        if owning_user_id is None:
            await interaction.response.send_message(
                "This doesn't look like an active ticket channel.", ephemeral=True
            )
            return

        is_owner_or_staff = await self._is_owner_or_staff(interaction, gdata)
        is_ticket_creator = str(interaction.user.id) == owning_user_id
        if not (is_owner_or_staff or is_ticket_creator):
            await interaction.response.send_message(
                "You don't have permission to close this ticket.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            "Are you sure you want to close this ticket? This will delete the channel.",
            view=ConfirmCloseView(),
            ephemeral=True,
        )

    async def _finalize_close(
        self, interaction: discord.Interaction
    ) -> None:
        guild_id = interaction.guild.id
        gdata = self._guild_data(guild_id)
        active = gdata.get("active_tickets", {})

        owning_user_id = next(
            (uid for uid, cid in active.items() if cid == interaction.channel.id), None
        )
        if owning_user_id is not None:
            del active[owning_user_id]
            gdata["active_tickets"] = active
            self._save_guild_data(guild_id, gdata)

        try:
            await interaction.response.send_message("Closing ticket...", ephemeral=True)
        except discord.HTTPException:
            pass

        try:
            await interaction.channel.delete(reason=f"Ticket closed by {interaction.user}")
        except discord.HTTPException as e:
            logger.warning("Failed to delete ticket channel: %s", e)

    # ---------------------------------------------------------------
    # button interaction handling
    # ---------------------------------------------------------------

    async def handle_create_ticket(self, interaction: discord.Interaction) -> None:
        # Serialize per-user so two near-simultaneous triggers (double
        # click, or duplicate bot processes reacting to the same Discord
        # event) can't both pass the "no active ticket" check before
        # either has saved its result. The second caller will see the
        # first caller's freshly-saved ticket once it acquires the lock.
        async with self._lock_for(interaction.user.id):
            guild_id = interaction.guild.id
            gdata = self._guild_data(guild_id)
            active = gdata.get("active_tickets", {})

            existing_channel_id = active.get(str(interaction.user.id))
            if existing_channel_id:
                existing_channel = interaction.guild.get_channel(existing_channel_id)
                if existing_channel:
                    await interaction.response.send_message(
                        f"You already have an open ticket: {existing_channel.mention}",
                        ephemeral=True,
                    )
                    return
                # Stale record (channel was deleted some other way) — clear it
                del active[str(interaction.user.id)]
                gdata["active_tickets"] = active
                self._save_guild_data(guild_id, gdata)

            channel = await self._create_ticket_channel(interaction, gdata)
            if channel is None:
                return  # error already sent to the user

            active[str(interaction.user.id)] = channel.id
            gdata["active_tickets"] = active
            self._save_guild_data(guild_id, gdata)

        close_embed = discord.Embed(
            description=(
                f"{interaction.user.mention} Welcome to your ticket! "
                f"Staff will be with you shortly.\n\nClick **Close Ticket** below when this is resolved."
            ),
            color=EMBED_COLOR,
        )
        await channel.send(embed=close_embed, view=TicketCloseView())

        await interaction.response.send_message(
            f"Ticket created: {channel.mention}", ephemeral=True
        )


class TicketPanelView(discord.ui.View):
    """Persistent view for the 'Create Ticket' button on the panel message."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Create Ticket",
        style=discord.ButtonStyle.green,
        emoji="🎫",
        custom_id=CREATE_TICKET_CUSTOM_ID,
    )
    async def create_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        cog: Optional[TicketsCog] = interaction.client.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Tickets system isn't loaded right now.", ephemeral=True
            )
            return
        await cog.handle_create_ticket(interaction)


class TicketCloseView(discord.ui.View):
    """Persistent view for the 'Close Ticket' button inside a ticket channel."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.red,
        emoji="🔒",
        custom_id=CLOSE_TICKET_CUSTOM_ID,
    )
    async def close_ticket(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        cog: Optional[TicketsCog] = interaction.client.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Tickets system isn't loaded right now.", ephemeral=True
            )
            return
        await cog._handle_close_request(interaction)


class ConfirmCloseView(discord.ui.View):
    """Ephemeral confirm/cancel view shown when someone tries to close a
    ticket. Not persistent (timeout applies) since it's a short-lived
    per-interaction prompt, not a standing panel."""

    def __init__(self) -> None:
        super().__init__(timeout=60)

    @discord.ui.button(
        label="Confirm Close",
        style=discord.ButtonStyle.red,
        custom_id=CONFIRM_CLOSE_CUSTOM_ID,
    )
    async def confirm(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        cog: Optional[TicketsCog] = interaction.client.get_cog("TicketsCog")
        if cog is None:
            await interaction.response.send_message(
                "Tickets system isn't loaded right now.", ephemeral=True
            )
            return
        await cog._finalize_close(interaction)

    @discord.ui.button(
        label="Cancel",
        style=discord.ButtonStyle.secondary,
        custom_id=CANCEL_CLOSE_CUSTOM_ID,
    )
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)


async def setup(bot: commands.Bot) -> None:
    cog = TicketsCog(bot)
    await bot.add_cog(cog)
    # Register persistent views so buttons on old messages keep working
    # after a bot restart — must use the same custom_id as when they were
    # first sent, which is guaranteed here since the views hard-code them.
    bot.add_view(TicketPanelView())
    bot.add_view(TicketCloseView())