"""
Reaction Roles cog for Roselle (Rosarium).

Carl-bot style reaction roles:
- /reactionrole create   -> post a new panel embed in a channel
- /reactionrole add      -> bind an emoji to a role on a message
- /reactionrole addmany  -> open a form to bind several emoji-role pairs at once
- /reactionrole remove   -> unbind an emoji from a message
- /reactionrole list     -> show bindings for one message or the whole server
- /reactionrole delete   -> wipe all bindings for a message

Storage layout (cogs/data/reaction_roles.json):
{
  "<guild_id>": {
    "<message_id>": {
      "channel_id": <int>,
      "bindings": {
        "<emoji_key>": <role_id>,
        ...
      }
    },
    ...
  },
  ...
}

emoji_key is either the unicode emoji string itself, or the custom
emoji ID as a string (for server emojis), which is what
payload.emoji.id / str(payload.emoji) give us consistently.
"""

from __future__ import annotations

import logging
import os
from typing import Optional, Union

import discord
from discord import app_commands
from discord.ext import commands

import config
from storage import JSONStore

logger = logging.getLogger("rosarium.reaction_roles")

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "reaction_roles.json")
os.makedirs(os.path.dirname(DATA_PATH), exist_ok=True)

EMBED_COLOR = config.EMBED_COLOR

# Discord embed limits we need to respect when listing bindings.
FIELD_VALUE_LIMIT = 1024
EMBED_SOFT_LIMIT = 5000  # hard limit is 6000 total chars per message
MAX_LIST_MESSAGES = 5

# discord.py hands back different types for "an emoji" depending on where
# it comes from: raw gateway payloads always give PartialEmoji, but
# message.reactions / Reaction.emoji can give a plain str (unicode emoji),
# a discord.Emoji, or a discord.PartialEmoji. Normalize all of them here.
AnyEmoji = Union[str, discord.Emoji, discord.PartialEmoji]


def emoji_key(emoji: AnyEmoji) -> str:
    """Normalize any of discord.py's emoji representations into a stable
    string key.

    Custom emojis are keyed by their numeric ID (stable even if renamed).
    Unicode emojis are keyed by the emoji character itself.
    """
    if isinstance(emoji, str):
        return emoji  # plain unicode emoji string
    if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)):
        if emoji.id is not None:  # custom emoji (real or partial)
            return str(emoji.id)
        return emoji.name  # PartialEmoji representing a unicode emoji
    # Fallback for any other emoji-like object
    return str(emoji)


def emoji_display(emoji: AnyEmoji) -> str:
    """How to render any emoji representation back as text, e.g. for embeds."""
    if isinstance(emoji, str):
        return emoji
    if isinstance(emoji, (discord.Emoji, discord.PartialEmoji)):
        if emoji.id is not None:
            return f"<{'a' if emoji.animated else ''}:{emoji.name}:{emoji.id}>"
        return emoji.name
    return str(emoji)


async def resolve_emoji(
    guild: discord.Guild, raw: str
) -> Optional[discord.PartialEmoji]:
    """Parse user input (unicode emoji, <:name:id>, or bare ID) into a
    PartialEmoji, resolving custom emojis against the guild when possible."""
    raw = raw.strip()

    # Custom emoji format: <:name:id> or <a:name:id>
    if raw.startswith("<") and raw.endswith(">"):
        try:
            partial = discord.PartialEmoji.from_str(raw)
            return partial
        except Exception:
            return None

    # Bare custom emoji ID
    if raw.isdigit():
        emoji_obj = guild.get_emoji(int(raw))
        if emoji_obj:
            return discord.PartialEmoji(
                name=emoji_obj.name, id=emoji_obj.id, animated=emoji_obj.animated
            )
        return None

    # Assume unicode emoji
    return discord.PartialEmoji(name=raw)


class AddManyModal(discord.ui.Modal, title="Add Multiple Reaction Roles"):
    """Popup form for pasting several emoji-role pairs at once.

    Modals give a proper multi-line textarea (unlike a slash command's
    single-line string option) and have their own longer interaction
    lifecycle, so large pastes don't risk expiring the interaction token
    before the bot can even acknowledge it.
    """

    pairs_input = discord.ui.TextInput(
        label="Emoji-Role Pairs (one per line)",
        style=discord.TextStyle.paragraph,
        placeholder="<a:hype:123456789012345678> @Hype\n<a:fire:123456789012345679> @Fire\n...",
        required=True,
        max_length=4000,
    )

    def __init__(
        self,
        cog: "ReactionRoles",
        message_id: str,
        channel: Optional[discord.TextChannel],
    ) -> None:
        super().__init__()
        self.cog = cog
        self.message_id = message_id
        self.channel = channel

    async def on_submit(self, interaction: discord.Interaction) -> None:
        # Modal submission counts as the initial interaction response, so
        # defer here to buy time for processing, then hand off to the
        # cog's shared logic which replies via followup.
        try:
            await interaction.response.defer(ephemeral=True)
        except discord.NotFound:
            logger.warning(
                "addmany modal: interaction expired before defer (message_id=%s)",
                self.message_id,
            )
            return

        await self.cog._process_addmany(
            interaction, self.message_id, self.pairs_input.value, self.channel
        )

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        logger.exception("Error in AddManyModal submission: %s", error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(
                    "Something went wrong processing that batch.", ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "Something went wrong processing that batch.", ephemeral=True
                )
        except discord.HTTPException:
            pass


class ReactionRoles(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JSONStore(DATA_PATH, default={})

    # ---------------------------------------------------------------
    # internal helpers
    # ---------------------------------------------------------------

    def _guild_data(self, guild_id: int) -> dict:
        data = self.store.get(str(guild_id), {})
        return data

    def _save_guild_data(self, guild_id: int, data: dict) -> None:
        self.store.set(str(guild_id), data)

    def _get_message_entry(self, guild_id: int, message_id: int) -> Optional[dict]:
        return self._guild_data(guild_id).get(str(message_id))

    def _persist_entry(self, guild_id: int, message_id: int, entry: dict) -> None:
        gdata = self._guild_data(guild_id)
        gdata[str(message_id)] = entry
        self._save_guild_data(guild_id, gdata)

    def _ensure_message_entry(
        self, guild_id: int, message_id: int, channel_id: int
    ) -> dict:
        gdata = self._guild_data(guild_id)
        key = str(message_id)
        if key not in gdata:
            gdata[key] = {"channel_id": channel_id, "bindings": {}}
            self._save_guild_data(guild_id, gdata)
        return gdata[key]

    def _role_problem(
        self, guild: discord.Guild, member: discord.abc.User, role: discord.Role
    ) -> Optional[str]:
        """Return a short reason this role can't be bound, or None if it's fine.

        Checks the bot's hierarchy AND the invoking member's hierarchy, so
        someone with Manage Roles can't hand out a role above their own.
        """
        if role.is_default() or role.managed:
            return "is `@everyone` or bot-managed and can't be assigned manually"
        if role >= guild.me.top_role:
            return (
                "is higher than or equal to my highest role "
                "(move my role above it in Server Settings > Roles)"
            )
        if member.id != guild.owner_id and role >= getattr(member, "top_role", role):
            return "is higher than or equal to your highest role"
        return None

    def _key_display(self, key: str) -> str:
        if key.isdigit():
            emoji_obj = self.bot.get_emoji(int(key))
            return str(emoji_obj) if emoji_obj else f"`emoji:{key}`"
        return key

    async def _build_panel_embed(
        self, guild: discord.Guild, title: str, description: str, bindings: dict
    ) -> discord.Embed:
        # Intentionally no "Reactions" field listing the bindings — the
        # reaction pills on the message itself are the only visible list
        # of what's bound. `bindings` is still accepted so callers don't
        # need to change, but it's no longer rendered into the embed.
        embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
        embed.set_footer(text="React below to receive a role • Roselle")
        return embed

    async def _refresh_panel_message(self, guild: discord.Guild, message_id: int) -> None:
        """Re-render the panel embed, and make sure the message has
        reactions for every bound emoji."""
        entry = self._get_message_entry(guild.id, message_id)
        if not entry:
            return
        channel = guild.get_channel_or_thread(entry["channel_id"])
        if channel is None:
            return
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException:  # NotFound, Forbidden, etc.
            return

        # Only touch the embed if it's one of our own messages; editing
        # someone else's message always 403s.
        if self.bot.user is not None and message.author.id == self.bot.user.id:
            if message.embeds:
                old = message.embeds[0]
                embed = await self._build_panel_embed(
                    guild,
                    old.title or "Reaction Roles",
                    old.description or "",
                    entry["bindings"],
                )
            else:
                embed = await self._build_panel_embed(
                    guild, "Reaction Roles", "React to get a role!", entry["bindings"]
                )
            try:
                await message.edit(embed=embed)
            except discord.HTTPException:
                pass

        # Make sure every bound emoji has a reaction present on the message
        existing = {emoji_key(r.emoji) for r in message.reactions}
        for key in entry["bindings"]:
            if key in existing:
                continue
            try:
                if key.isdigit():
                    emoji_obj = self.bot.get_emoji(int(key))
                    if emoji_obj:
                        await message.add_reaction(emoji_obj)
                else:
                    await message.add_reaction(key)
            except discord.HTTPException:
                logger.warning("Failed to add reaction %s to message %s", key, message_id)

    # ---------------------------------------------------------------
    # command group
    # ---------------------------------------------------------------

    reactionrole = app_commands.Group(
        name="reactionrole",
        description="Manage reaction roles",
        default_permissions=discord.Permissions(manage_roles=True),
        guild_only=True,
    )

    @reactionrole.command(name="create", description="Post a new reaction role panel")
    @app_commands.describe(
        channel="Channel to post the panel in",
        title="Panel embed title",
        description="Panel embed description",
    )
    async def create(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str = "Reaction Roles",
        description: str = "React below to receive a role!",
    ) -> None:
        await interaction.response.defer(ephemeral=True)

        embed = await self._build_panel_embed(interaction.guild, title, description, {})
        try:
            message = await channel.send(embed=embed)
        except discord.Forbidden:
            await interaction.followup.send(
                f"I don't have permission to send messages/embeds in {channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Couldn't post the panel: {e}", ephemeral=True
            )
            return

        self._ensure_message_entry(interaction.guild.id, message.id, channel.id)

        await interaction.followup.send(
            f"Panel created in {channel.mention}. Message ID: `{message.id}`\n"
            f"Use `/reactionrole add` with this message ID to bind emojis to roles.",
            ephemeral=True,
        )

    @reactionrole.command(name="add", description="Bind an emoji to a role on a message")
    @app_commands.describe(
        message_id="ID of the panel message (from /reactionrole create, or right-click > Copy ID)",
        emoji="The emoji to react with (unicode or custom server emoji)",
        role="The role to assign when someone reacts",
        channel="Channel the message is in (only needed if not in this channel)",
    )
    async def add(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
        role: discord.Role,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        guild = interaction.guild

        # This command does several API calls (fetch, react, edit) which can
        # easily blow past Discord's 3-second window, so ack immediately.
        await interaction.response.defer(ephemeral=True)

        problem = self._role_problem(guild, interaction.user, role)
        if problem:
            await interaction.followup.send(
                f"I can't bind {role.mention}: it {problem}.", ephemeral=True
            )
            return

        try:
            msg_id_int = int(message_id)
        except ValueError:
            await interaction.followup.send(
                "That doesn't look like a valid message ID.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        try:
            message = await target_channel.fetch_message(msg_id_int)
        except discord.NotFound:
            await interaction.followup.send(
                f"Couldn't find a message with ID `{message_id}` in {target_channel.mention}. "
                f"If it's in another channel, pass the `channel` option.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to read messages in that channel.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Couldn't fetch that message: {e}", ephemeral=True
            )
            return

        parsed_emoji = await resolve_emoji(guild, emoji)
        if parsed_emoji is None:
            await interaction.followup.send(
                "I couldn't parse that emoji. Use a standard emoji or one from this server.",
                ephemeral=True,
            )
            return

        key = emoji_key(parsed_emoji)
        existing_entry = self._get_message_entry(guild.id, message.id)

        if existing_entry and key in existing_entry["bindings"]:
            existing_role = guild.get_role(existing_entry["bindings"][key])
            await interaction.followup.send(
                f"{emoji_display(parsed_emoji)} is already bound to "
                f"{existing_role.mention if existing_role else 'a deleted role'} on that message. "
                f"Remove it first with `/reactionrole remove`.",
                ephemeral=True,
            )
            return

        # React FIRST, bind after. resolve_emoji accepts any text as a
        # "unicode emoji", so if the reaction fails (bad emoji, reaction cap,
        # missing perms) we must not leave a junk binding saved.
        try:
            if parsed_emoji.is_custom_emoji():
                await message.add_reaction(parsed_emoji)
            else:
                await message.add_reaction(parsed_emoji.name)
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Couldn't add that reaction to the message, so nothing was bound: {e}",
                ephemeral=True,
            )
            return

        entry = self._ensure_message_entry(guild.id, message.id, target_channel.id)
        entry["bindings"][key] = role.id
        self._persist_entry(guild.id, message.id, entry)

        await self._refresh_panel_message(guild, message.id)

        await interaction.followup.send(
            f"Bound {emoji_display(parsed_emoji)} → {role.mention} on that message.",
            ephemeral=True,
        )

    async def _process_addmany(
        self,
        interaction: discord.Interaction,
        message_id: str,
        pairs: str,
        channel: Optional[discord.TextChannel],
    ) -> None:
        """Core logic for binding multiple emoji-role pairs. Shared by the
        addmany modal (and reusable by a slash command if ever needed).
        Expects the interaction to already be deferred or otherwise still
        valid for a followup.send."""
        guild = interaction.guild

        try:
            msg_id_int = int(message_id)
        except ValueError:
            await interaction.followup.send(
                "That doesn't look like a valid message ID.", ephemeral=True
            )
            return

        target_channel = channel or interaction.channel
        try:
            message = await target_channel.fetch_message(msg_id_int)
        except discord.NotFound:
            await interaction.followup.send(
                f"Couldn't find a message with ID `{message_id}` in {target_channel.mention}. "
                f"If it's in another channel, run this again after switching to that channel.",
                ephemeral=True,
            )
            return
        except discord.Forbidden:
            await interaction.followup.send(
                "I don't have permission to read messages in that channel.", ephemeral=True
            )
            return
        except discord.HTTPException as e:
            await interaction.followup.send(
                f"Couldn't fetch that message: {e}", ephemeral=True
            )
            return

        # Parse each non-empty line as "<emoji> <role>"
        raw_lines = [ln.strip() for ln in pairs.splitlines() if ln.strip()]
        if not raw_lines:
            await interaction.followup.send(
                "No pairs found. Give one `emoji role` pair per line.", ephemeral=True
            )
            return

        # Work on a local entry and only persist if something actually
        # succeeded, so a fully-failed batch doesn't leave an empty entry.
        entry = self._get_message_entry(guild.id, message.id) or {
            "channel_id": target_channel.id,
            "bindings": {},
        }

        succeeded: list[str] = []
        failed: list[str] = []

        for line_num, line in enumerate(raw_lines, start=1):
            parts = line.split(maxsplit=1)
            if len(parts) != 2:
                failed.append(f"Line {line_num} (`{line}`): expected `emoji role`")
                continue

            emoji_raw, role_raw = parts

            parsed_emoji = await resolve_emoji(guild, emoji_raw)
            if parsed_emoji is None:
                failed.append(f"Line {line_num}: couldn't parse emoji `{emoji_raw}`")
                continue

            # Resolve role: mention, bare ID, or name (case-insensitive)
            role_obj: Optional[discord.Role] = None
            role_id_match = role_raw.strip("<@&>")
            if role_id_match.isdigit():
                role_obj = guild.get_role(int(role_id_match))
            if role_obj is None:
                role_obj = discord.utils.get(guild.roles, name=role_id_match)
            if role_obj is None:
                lowered = role_id_match.lower()
                role_obj = discord.utils.find(
                    lambda r: r.name.lower() == lowered, guild.roles
                )
            if role_obj is None:
                failed.append(f"Line {line_num}: couldn't find role `{role_raw}`")
                continue

            problem = self._role_problem(guild, interaction.user, role_obj)
            if problem:
                failed.append(f"Line {line_num}: {role_obj.name} {problem}")
                continue

            key = emoji_key(parsed_emoji)
            if key in entry["bindings"]:
                failed.append(
                    f"Line {line_num}: {emoji_display(parsed_emoji)} is already bound on this message"
                )
                continue

            try:
                if parsed_emoji.is_custom_emoji():
                    await message.add_reaction(parsed_emoji)
                else:
                    await message.add_reaction(parsed_emoji.name)
            except discord.HTTPException as e:
                failed.append(
                    f"Line {line_num}: couldn't react with {emoji_display(parsed_emoji)} ({e})"
                )
                continue

            entry["bindings"][key] = role_obj.id
            succeeded.append(f"{emoji_display(parsed_emoji)} → {role_obj.mention}")

        if succeeded:
            self._persist_entry(guild.id, message.id, entry)
            await self._refresh_panel_message(guild, message.id)

        result_lines = []
        if succeeded:
            result_lines.append(f"**Bound {len(succeeded)}:**\n" + "\n".join(succeeded))
        if failed:
            result_lines.append(f"**Failed {len(failed)}:**\n" + "\n".join(failed))

        response_text = "\n\n".join(result_lines) if result_lines else "Nothing to do."
        # Discord message limits: truncate if this got long
        if len(response_text) > 1900:
            response_text = response_text[:1900] + "\n... (truncated)"

        await interaction.followup.send(response_text, ephemeral=True)

    @reactionrole.command(
        name="addmany",
        description="Open a form to bind multiple emoji-role pairs to a message at once",
    )
    @app_commands.describe(
        message_id="ID of the panel message",
        channel="Channel the message is in (only needed if not in this channel)",
    )
    async def addmany(
        self,
        interaction: discord.Interaction,
        message_id: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        # Modals must be the FIRST response to an interaction (no defer
        # beforehand), so open it immediately with whatever context we
        # already have from the slash command's own options.
        modal = AddManyModal(cog=self, message_id=message_id, channel=channel)
        await interaction.response.send_modal(modal)

    @reactionrole.command(name="remove", description="Unbind an emoji from a message")
    @app_commands.describe(
        message_id="ID of the panel message",
        emoji="The emoji to unbind",
        channel="Channel the message is in (if not this channel)",
    )
    async def remove(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
        channel: Optional[discord.TextChannel] = None,
    ) -> None:
        guild = interaction.guild

        # Fetching + clearing a reaction + refreshing can exceed 3 seconds.
        await interaction.response.defer(ephemeral=True)

        try:
            msg_id_int = int(message_id)
        except ValueError:
            await interaction.followup.send(
                "That doesn't look like a valid message ID.", ephemeral=True
            )
            return

        entry = self._get_message_entry(guild.id, msg_id_int)
        if not entry:
            await interaction.followup.send(
                "No reaction role bindings found for that message.", ephemeral=True
            )
            return

        parsed_emoji = await resolve_emoji(guild, emoji)
        if parsed_emoji is None:
            await interaction.followup.send(
                "I couldn't parse that emoji.", ephemeral=True
            )
            return

        key = emoji_key(parsed_emoji)
        if key not in entry["bindings"]:
            await interaction.followup.send(
                "That emoji isn't bound on that message.", ephemeral=True
            )
            return

        del entry["bindings"][key]
        self._persist_entry(guild.id, msg_id_int, entry)

        target_channel = channel or guild.get_channel_or_thread(entry["channel_id"])
        if target_channel:
            try:
                message = await target_channel.fetch_message(msg_id_int)
                if parsed_emoji.is_custom_emoji():
                    await message.clear_reaction(parsed_emoji)
                else:
                    await message.clear_reaction(parsed_emoji.name)
            except discord.HTTPException:
                pass

        await self._refresh_panel_message(guild, msg_id_int)

        await interaction.followup.send(
            f"Unbound {emoji_display(parsed_emoji)} from that message.", ephemeral=True
        )

    @reactionrole.command(name="list", description="List reaction role bindings")
    @app_commands.describe(message_id="Optional: limit to one message ID")
    async def list_bindings(
        self, interaction: discord.Interaction, message_id: Optional[str] = None
    ) -> None:
        guild = interaction.guild
        gdata = self._guild_data(guild.id)

        if not gdata:
            await interaction.response.send_message(
                "No reaction role panels set up in this server yet.", ephemeral=True
            )
            return

        if message_id:
            entry = gdata.get(message_id)
            if not entry:
                await interaction.response.send_message(
                    "No bindings found for that message.", ephemeral=True
                )
                return
            entries = {message_id: entry}
        else:
            entries = gdata

        # Build (name, value) fields, splitting any binding list that would
        # exceed Discord's 1024-char field value limit (a full 20-reaction
        # panel with custom emojis easily does).
        fields: list[tuple[str, str]] = []
        for mid, entry in entries.items():
            channel = guild.get_channel_or_thread(entry["channel_id"])
            channel_txt = channel.mention if channel else "`#deleted-channel`"

            lines = []
            for key, role_id in entry["bindings"].items():
                role = guild.get_role(role_id)
                role_txt = role.mention if role else "`deleted-role`"
                lines.append(f"{self._key_display(key)} → {role_txt}")

            chunks: list[str] = []
            cur = ""
            for line in lines:
                if cur and len(cur) + len(line) + 1 > FIELD_VALUE_LIMIT:
                    chunks.append(cur)
                    cur = line
                else:
                    cur = f"{cur}\n{line}" if cur else line
            if cur:
                chunks.append(cur)
            if not chunks:
                chunks = ["*no bindings*"]

            for i, chunk in enumerate(chunks):
                name = (
                    f"Message {mid} ({channel_txt})" if i == 0 else f"Message {mid} (cont.)"
                )
                fields.append((name, chunk))

        # Pack fields into embeds (<=25 fields, <=~5000 chars each).
        title = "Reaction Role Panels"
        embeds: list[discord.Embed] = []
        current: Optional[discord.Embed] = None
        size = 0
        for name, value in fields:
            fsize = len(name) + len(value)
            if (
                current is None
                or len(current.fields) >= 25
                or size + fsize > EMBED_SOFT_LIMIT
            ):
                current = discord.Embed(title=title, color=EMBED_COLOR)
                embeds.append(current)
                size = len(title)
            current.add_field(name=name, value=value, inline=False)
            size += fsize

        truncated = len(embeds) > MAX_LIST_MESSAGES
        embeds = embeds[:MAX_LIST_MESSAGES]

        await interaction.response.send_message(embed=embeds[0], ephemeral=True)
        for extra in embeds[1:]:
            await interaction.followup.send(embed=extra, ephemeral=True)
        if truncated:
            await interaction.followup.send(
                "There are more panels than I can show here. "
                "Pass a `message_id` to look at a specific one.",
                ephemeral=True,
            )

    @reactionrole.command(name="delete", description="Delete all bindings for a message")
    @app_commands.describe(message_id="ID of the panel message to clear")
    async def delete(self, interaction: discord.Interaction, message_id: str) -> None:
        guild = interaction.guild
        gdata = self._guild_data(guild.id)

        if message_id not in gdata:
            await interaction.response.send_message(
                "No bindings found for that message.", ephemeral=True
            )
            return

        del gdata[message_id]
        self._save_guild_data(guild.id, gdata)

        await interaction.response.send_message(
            f"Deleted all reaction role bindings for message `{message_id}`. "
            f"(The message itself and its reactions were left untouched.)",
            ephemeral=True,
        )

    # ---------------------------------------------------------------
    # raw reaction listeners — the actual role assignment
    # ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.member is None:
            return
        if payload.member.bot:
            return

        entry = self._get_message_entry(payload.guild_id, payload.message_id)
        if not entry:
            return

        key = emoji_key(payload.emoji)
        role_id = entry["bindings"].get(key)
        if role_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(role_id)
        if role is None:
            logger.warning(
                "Reaction role %s on message %s points to a deleted role",
                key,
                payload.message_id,
            )
            return

        if role in payload.member.roles:
            return

        # Diagnose the specific reason up front, rather than a generic
        # Forbidden after the fact — this is almost always one of two things:
        # (1) the role sits at or above the bot's own top role (Discord
        #     blocks this regardless of Administrator or any permission),
        # or (2) it's a managed role (tied to a bot, boost tier, or a
        # linked Twitch/Spotify integration), which can never be assigned
        # manually by anyone, including the bot.
        if role.managed:
            logger.warning(
                "Cannot assign role '%s' (%s) in guild %s: it's a managed role "
                "(tied to a bot/integration/boost tier) and can't be assigned manually.",
                role.name,
                role_id,
                payload.guild_id,
            )
            return

        if role >= guild.me.top_role:
            logger.warning(
                "Cannot assign role '%s' (%s) in guild %s: it is at or above "
                "Roselle's own highest role (currently '%s'). Move Roselle's role "
                "above '%s' in Server Settings > Roles.",
                role.name,
                role_id,
                payload.guild_id,
                guild.me.top_role.name,
                role.name,
            )
            return

        try:
            await payload.member.add_roles(role, reason="Reaction role")
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to add role '%s' (%s) in guild %s — "
                "hierarchy and managed-role checks passed, so this is likely "
                "a permissions issue unrelated to role position (e.g. bot "
                "lacks Manage Roles entirely).",
                role.name,
                role_id,
                payload.guild_id,
            )
        except discord.HTTPException as e:
            logger.warning("Failed to add reaction role: %s", e)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return

        entry = self._get_message_entry(payload.guild_id, payload.message_id)
        if not entry:
            return

        key = emoji_key(payload.emoji)
        role_id = entry["bindings"].get(key)
        if role_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return

        member = guild.get_member(payload.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(payload.user_id)
            except discord.NotFound:
                return
            except discord.HTTPException as e:
                logger.warning("Failed to fetch member %s: %s", payload.user_id, e)
                return

        if member.bot:
            return

        role = guild.get_role(role_id)
        if role is None:
            return

        if role not in member.roles:
            return

        try:
            await member.remove_roles(role, reason="Reaction role removed")
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to remove role %s in guild %s", role_id, payload.guild_id
            )
        except discord.HTTPException as e:
            logger.warning("Failed to remove reaction role: %s", e)

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        """Clean up bindings if the panel message itself gets deleted."""
        if payload.guild_id is None:
            return
        gdata = self._guild_data(payload.guild_id)
        key = str(payload.message_id)
        if key in gdata:
            del gdata[key]
            self._save_guild_data(payload.guild_id, gdata)

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(
        self, payload: discord.RawBulkMessageDeleteEvent
    ) -> None:
        """Same cleanup for panels removed via bulk delete / purge."""
        if payload.guild_id is None:
            return
        gdata = self._guild_data(payload.guild_id)
        changed = False
        for mid in payload.message_ids:
            if str(mid) in gdata:
                del gdata[str(mid)]
                changed = True
        if changed:
            self._save_guild_data(payload.guild_id, gdata)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRoles(bot))