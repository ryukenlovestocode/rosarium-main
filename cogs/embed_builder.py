"""
Embed Builder cog for Roselle (Rosarium).

Lets staff build a rich embed via a modal, preview it, and send it to a
channel (or edit an existing message the bot posted). Custom emoji IDs
work anywhere text is entered (title, description, footer) — just type
them as <a:name:id> or <:name:id> and Discord renders them natively in
the embed, no special handling needed on our end.

Note on GIFs: Discord embeds cannot play a GIF in the footer (footer only
supports a small static icon + text). The large "image" slot at the
bottom of the embed is the correct place for an animated GIF — that's
what /embed create's "Image / GIF URL" field is for.

Commands:
- /embed create   -> open the builder modal, creates a draft for you
- /embed preview  -> see your current draft (ephemeral)
- /embed footer   -> set footer text + optional small footer icon
- /embed send     -> post the draft to a channel
- /embed edit     -> edit an existing message the bot sent, using your draft
- /embed reset    -> clear your draft and start over

Drafts are stored in memory per user ID and are NOT persisted across bot
restarts — they're meant to be a short build-then-send workflow, not a
saved template system.
"""

from __future__ import annotations

import logging
import re
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

logger = logging.getLogger("rosarium.embed_builder")

DEFAULT_COLOR = config.EMBED_COLOR

HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
URL_RE = re.compile(r"^https?://\S+$")


def parse_color(raw: Optional[str]) -> Optional[discord.Color]:
    """Parse a hex color string like '#FF00AA' or 'FF00AA' into a Color.
    Returns None if raw is empty; raises ValueError if raw is invalid."""
    if not raw or not raw.strip():
        return None
    raw = raw.strip()
    if not HEX_COLOR_RE.match(raw):
        raise ValueError(f"'{raw}' isn't a valid hex color (e.g. #5865F2 or 5865F2)")
    return discord.Color(int(raw.lstrip("#"), 16))


def looks_like_url(raw: Optional[str]) -> bool:
    return bool(raw and URL_RE.match(raw.strip()))


class EmbedDraft:
    """A mutable in-progress embed for one user."""

    def __init__(self) -> None:
        self.title: Optional[str] = None
        self.description: Optional[str] = None
        self.color: discord.Color = DEFAULT_COLOR
        self.image_url: Optional[str] = None
        self.thumbnail_url: Optional[str] = None
        self.footer_text: Optional[str] = None
        self.footer_icon_url: Optional[str] = None
        self.author_name: Optional[str] = None
        self.author_icon_url: Optional[str] = None

    def to_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title or None,
            description=self.description or None,
            color=self.color,
        )
        if self.image_url:
            embed.set_image(url=self.image_url)
        if self.thumbnail_url:
            embed.set_thumbnail(url=self.thumbnail_url)
        if self.footer_text or self.footer_icon_url:
            embed.set_footer(
                text=self.footer_text or None,
                icon_url=self.footer_icon_url or None,
            )
        if self.author_name:
            embed.set_author(name=self.author_name, icon_url=self.author_icon_url or None)
        return embed

    def is_empty(self) -> bool:
        return not any(
            [
                self.title,
                self.description,
                self.image_url,
                self.thumbnail_url,
                self.footer_text,
                self.author_name,
            ]
        )


class EmbedCreateModal(discord.ui.Modal, title="Create Embed"):
    """First step of building an embed: the core content fields.

    Custom emoji IDs can be typed directly into title/description as
    <a:name:id> (animated) or <:name:id> (static) — Discord renders them
    inline automatically once the embed is sent, no extra handling needed.
    """

    title_input = discord.ui.TextInput(
        label="Title",
        style=discord.TextStyle.short,
        required=False,
        max_length=256,
        placeholder="Announcement title (supports <a:name:id> custom emoji)",
    )
    description_input = discord.ui.TextInput(
        label="Description",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=4000,
        placeholder="Main body text (supports <a:name:id> custom emoji)",
    )
    color_input = discord.ui.TextInput(
        label="Color (hex, optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=7,
        placeholder="#5865F2",
    )
    image_input = discord.ui.TextInput(
        label="Image / GIF URL (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=500,
        placeholder="https://... — GIFs animate here (large image slot)",
    )
    thumbnail_input = discord.ui.TextInput(
        label="Thumbnail URL (optional)",
        style=discord.TextStyle.short,
        required=False,
        max_length=500,
        placeholder="https://... small image, top-right corner",
    )

    def __init__(self, cog: "EmbedBuilder") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = self.cog.get_draft(interaction.user.id)

        draft.title = self.title_input.value.strip() or None
        draft.description = self.description_input.value.strip() or None

        try:
            color = parse_color(self.color_input.value)
            if color is not None:
                draft.color = color
        except ValueError as e:
            await interaction.response.send_message(str(e), ephemeral=True)
            return

        image_raw = self.image_input.value.strip()
        if image_raw and not looks_like_url(image_raw):
            await interaction.response.send_message(
                f"Image URL doesn't look valid: `{image_raw}`. It must start with http(s)://",
                ephemeral=True,
            )
            return
        draft.image_url = image_raw or None

        thumb_raw = self.thumbnail_input.value.strip()
        if thumb_raw and not looks_like_url(thumb_raw):
            await interaction.response.send_message(
                f"Thumbnail URL doesn't look valid: `{thumb_raw}`. It must start with http(s)://",
                ephemeral=True,
            )
            return
        draft.thumbnail_url = thumb_raw or None

        await interaction.response.send_message(
            "Draft updated. Use `/embed footer` to set a footer, "
            "`/embed preview` to see it, or `/embed send` to post it.",
            embed=draft.to_embed(),
            ephemeral=True,
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Error in EmbedCreateModal: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong building that embed.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong building that embed.", ephemeral=True
                )
        except discord.HTTPException:
            pass


class EmbedFooterModal(discord.ui.Modal, title="Set Embed Footer"):
    """Separate step for footer, since modals cap at 5 fields and footer
    (text + icon) plus author fields would crowd the main create modal."""

    footer_text_input = discord.ui.TextInput(
        label="Footer text",
        style=discord.TextStyle.short,
        required=False,
        max_length=2048,
        placeholder="Small text at the bottom (supports <a:name:id> emoji)",
    )
    footer_icon_input = discord.ui.TextInput(
        label="Footer icon URL (small, static only)",
        style=discord.TextStyle.short,
        required=False,
        max_length=500,
        placeholder="https://... — GIFs will NOT animate here, use /embed create's image field for that",
    )

    def __init__(self, cog: "EmbedBuilder") -> None:
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        draft = self.cog.get_draft(interaction.user.id)

        draft.footer_text = self.footer_text_input.value.strip() or None

        icon_raw = self.footer_icon_input.value.strip()
        if icon_raw and not looks_like_url(icon_raw):
            await interaction.response.send_message(
                f"Footer icon URL doesn't look valid: `{icon_raw}`. It must start with http(s)://",
                ephemeral=True,
            )
            return
        draft.footer_icon_url = icon_raw or None

        await interaction.response.send_message(
            "Footer updated.", embed=draft.to_embed(), ephemeral=True
        )

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        logger.exception("Error in EmbedFooterModal: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong setting the footer.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong setting the footer.", ephemeral=True
                )
        except discord.HTTPException:
            pass


class EmbedBuilder(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # user_id -> EmbedDraft. In-memory only; not persisted on purpose.
        self._drafts: dict[int, EmbedDraft] = {}

    def get_draft(self, user_id: int) -> EmbedDraft:
        if user_id not in self._drafts:
            self._drafts[user_id] = EmbedDraft()
        return self._drafts[user_id]

    embed_group = app_commands.Group(
        name="embed",
        description="Build and send rich embeds",
        default_permissions=discord.Permissions(manage_messages=True),
    )

    @embed_group.command(name="create", description="Open the embed builder")
    async def create(self, interaction: discord.Interaction) -> None:
        modal = EmbedCreateModal(cog=self)
        await interaction.response.send_modal(modal)

    @embed_group.command(name="footer", description="Set the embed's footer text and icon")
    async def footer(self, interaction: discord.Interaction) -> None:
        modal = EmbedFooterModal(cog=self)
        await interaction.response.send_modal(modal)

    @embed_group.command(name="preview", description="Preview your current embed draft")
    async def preview(self, interaction: discord.Interaction) -> None:
        draft = self.get_draft(interaction.user.id)
        if draft.is_empty():
            await interaction.response.send_message(
                "Your draft is empty. Use `/embed create` to start building one.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(embed=draft.to_embed(), ephemeral=True)

    @embed_group.command(name="reset", description="Clear your current embed draft")
    async def reset(self, interaction: discord.Interaction) -> None:
        self._drafts.pop(interaction.user.id, None)
        await interaction.response.send_message("Draft cleared.", ephemeral=True)

    @embed_group.command(name="send", description="Send your embed draft to a channel")
    @app_commands.describe(
        channel="Channel to send the embed to",
        content="Optional plain text to send alongside the embed",
    )
    async def send(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        content: Optional[str] = None,
    ) -> None:
        draft = self.get_draft(interaction.user.id)
        if draft.is_empty():
            await interaction.response.send_message(
                "Your draft is empty. Use `/embed create` to build one first.",
                ephemeral=True,
            )
            return

        try:
            await channel.send(content=content, embed=draft.to_embed())
        except discord.Forbidden:
            await interaction.response.send_message(
                f"I don't have permission to send messages in {channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Discord rejected that embed: {e}", ephemeral=True
            )
            return

        await interaction.response.send_message(
            f"Sent to {channel.mention}.", ephemeral=True
        )

    @embed_group.command(
        name="edit", description="Edit an existing message (sent by this bot) with your draft"
    )
    @app_commands.describe(
        message_id="ID of the message to edit (must be sent by this bot)",
        channel="Channel the message is in (only needed if not in this channel)",
    )
    async def edit(
        self,
        interaction: discord.Interaction,
        message_id: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        draft = self.get_draft(interaction.user.id)
        if draft.is_empty():
            await interaction.response.send_message(
                "Your draft is empty. Use `/embed create` to build one first.",
                ephemeral=True,
            )
            return

        try:
            msg_id_int = int(message_id)
        except ValueError:
            await interaction.response.send_message(
                "That doesn't look like a valid message ID.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        try:
            message = await target_channel.fetch_message(msg_id_int)
        except discord.NotFound:
            await interaction.response.send_message(
                f"Couldn't find a message with ID `{message_id}` in {target_channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.response.send_message(
                "I don't have permission to read messages in that channel.", ephemeral=True
            )
            return

        if message.author.id != self.bot.user.id:
            await interaction.response.send_message(
                "I can only edit messages that I sent myself.", ephemeral=True
            )
            return

        try:
            await message.edit(embed=draft.to_embed())
        except discord.HTTPException as e:
            await interaction.response.send_message(
                f"Couldn't edit that message: {e}", ephemeral=True
            )
            return

        await interaction.response.send_message("Message updated.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(EmbedBuilder(bot))