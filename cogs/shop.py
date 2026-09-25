"""
Shop cog — spend Petals (economy.py's currency) on cosmetic and prestige
items.

Requires economy.py to be loaded (reads/writes balances through its
Economy cog instance — no separate currency logic lives here).

Items:
  Custom Role     (2,000,000)  — a personal role, name + hex color your
                                 choice, created once and kept forever.
  Auto-Reactor    (3,000,000)  — the bot reacts to a chosen emoji on any
                                 message that mentions you or contains
                                 your name. Stacks up to 3 emojis.
  Prestige roles  (4M – 100M)  — a five-rung ladder of increasingly
                                 absurd, purely cosmetic status roles.
                                 One-time purchases, don't expire, and
                                 you can eventually own all five as a
                                 show of how long you've been grinding.

Commands:
  $servershop              — browse the catalog
  $servershop buy <item>   — purchase an item (prompts for details where needed)
  $servershop inventory    — see what you own
  $servershop setup        — [owner/staff] create the 5 prestige roles for
                              this server (one-time, run once per server)

Storage layout (cogs/data/shop.json):
{
  "<guild_id>": {
    "prestige_role_ids": {"<item_key>": <role_id>, ...},
    "purchases": {
      "<user_id>": {
        "custom_role_id": <int or null>,
        "reactor_emojis": [<str>, ...],   // max 3
        "prestige": [<item_key>, ...]
      }
    }
  }
}

Design notes:
  - Custom roles and reactor bindings are created/stored per-user, since
    they're unique to each buyer. Prestige roles are created ONCE per
    server (via $servershop setup) and then just assigned/tracked per
    buyer, since everyone who buys "Diamond Crown" shares the same role.
  - All new roles (custom + prestige) are created below the bot's own
    top role, matching the hierarchy pattern used by autorole.py and
    reaction_roles.py in this project — Discord can't assign a role at
    or above the bot's own position regardless of permissions.
  - Auto-reactor matches on the author's current server display name,
    global username, AND an explicit @mention appearing in a message's
    content — whichever the message contains. Case-insensitive substring
    match on names; this can occasionally false-positive on short names
    (e.g. someone named "Max" reacts to "at max capacity") — this is a
    known, accepted trade-off of name-based matching, not a bug.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

import discord
from discord.ext import commands

import config
from storage import JSONStore

logger = logging.getLogger("rosarium.shop")

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "shop.json")

CURRENCY = "petals"
MAX_REACTOR_EMOJIS = 3

HEX_COLOR_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")

# ---------- CATALOG ----------
# Prestige items are ordered cheapest-to-most-expensive on purpose — this
# list order is what $servershop displays, and the ladder should read as
# a climb. "role_name"/"role_color" are only used the FIRST time
# $servershop setup creates these roles; changing them later doesn't
# rename/recolor an already-created role (re-run setup's rename path, or
# edit the role manually in Discord, for that).
PRESTIGE_ITEMS: dict[str, dict] = {
    "diamond_crown": {
        "label": "💎 Diamond Crown",
        "price": 4_000_000,
        "role_name": "💎 Diamond Crown",
        "role_color": 0x9EE6FF,
    },
    "royalty": {
        "label": "👑 Royalty",
        "price": 8_000_000,
        "role_name": "👑 Royalty",
        "role_color": 0xE8B023,
    },
    "economy_legend": {
        "label": "🏛️ Economy Legend",
        "price": 15_000_000,
        "role_name": "🏛️ Economy Legend",
        "role_color": 0xD4A24E,
    },
    "ultra_rare_collectible": {
        "label": "🌌 Ultra-Rare Collectible",
        "price": 35_000_000,
        "role_name": "🌌 Ultra-Rare Collectible",
        "role_color": 0x8B0000,
    },
    "server_exclusive_artifact": {
        "label": "🪐 Server-Exclusive Artifact",
        "price": 100_000_000,
        "role_name": "🪐 Server-Exclusive Artifact",
        "role_color": 0x1A1A1A,
    },
}

CUSTOM_ROLE_PRICE = 2_000_000
AUTO_REACTOR_PRICE = 3_000_000


def _fmt(amount: int) -> str:
    return f"{amount:,} {CURRENCY}"


def _shop_embed(title: str, description: str = "", color: Optional[int] = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=color or config.EMBED_COLOR)
    embed.set_footer(text=config.FOOTER_TEXT)
    return embed


class Shop(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = JSONStore(DATA_PATH, default={})

    # ---------------------------------------------------------------
    # internal helpers
    # ---------------------------------------------------------------

    def _economy(self) -> Optional[commands.Cog]:
        """Look up the Economy cog by its class name. Returns None if
        economy.py isn't loaded — every command below checks this and
        fails with a clear message instead of crashing on AttributeError."""
        return self.bot.get_cog("Economy")

    def _guild_data(self, guild_id: int) -> dict:
        return self.store.get(
            str(guild_id), {"prestige_role_ids": {}, "purchases": {}}
        )

    def _save_guild_data(self, guild_id: int, data: dict) -> None:
        self.store.set(str(guild_id), data)

    def _get_purchase_record(self, gdata: dict, user_id: int) -> dict:
        purchases = gdata.setdefault("purchases", {})
        key = str(user_id)
        if key not in purchases:
            purchases[key] = {
                "custom_role_id": None,
                "reactor_emojis": [],
                "prestige": [],
            }
        return purchases[key]

    async def _charge(self, ctx: commands.Context, price: int) -> bool:
        """Checks balance and deducts `price` from ctx.author if they can
        afford it. Returns True on success, sends an error and returns
        False otherwise. Centralizing this ensures every item is charged
        the same way the economy cog's own `pay` command does it."""
        economy = self._economy()
        if economy is None:
            await ctx.send(
                "The economy system isn't loaded right now — the shop can't "
                "process payments. Try again later or tell an admin."
            )
            return False

        balance = economy.get_balance(ctx.author.id)
        if price > balance:
            await ctx.send(
                f"You need **{_fmt(price)}** for that, but you only have "
                f"**{_fmt(balance)}**."
            )
            return False

        economy.set_balance(ctx.author.id, balance - price)
        return True

    def _role_assignable(self, guild: discord.Guild, role: discord.Role) -> Optional[str]:
        """Returns an error string if the bot can't assign this role, else None."""
        if role.managed:
            return f"{role.mention} is managed by an integration and can't be assigned manually."
        if role >= guild.me.top_role:
            return (
                f"I can't assign {role.mention} — it's at or above my own "
                "highest role. Ask an admin to move my role up."
            )
        return None

    # ---------------------------------------------------------------
    # $servershop (browse)
    # ---------------------------------------------------------------

    @commands.hybrid_group(
        name="servershop", invoke_without_command=True, description="Browse the server shop."
    )
    async def servershop(self, ctx: commands.Context) -> None:
        economy = self._economy()
        balance_line = ""
        if economy is not None:
            balance_line = f"\nYour balance: **{_fmt(economy.get_balance(ctx.author.id))}**\n"

        lines = [
            f"🎨 **Custom Role** — {_fmt(CUSTOM_ROLE_PRICE)}",
            "   `$servershop buy customrole <name> <#hexcolor>`",
            "",
            f"🔔 **Auto-Reactor** — {_fmt(AUTO_REACTOR_PRICE)}",
            f"   Reacts to your name in chat. Max {MAX_REACTOR_EMOJIS} at once.",
            "   `$servershop buy reactor <emoji>`",
            "",
            "🏆 **Prestige** — one-time, permanent status roles",
        ]
        for key, item in PRESTIGE_ITEMS.items():
            lines.append(f"   {item['label']} — {_fmt(item['price'])}  `$servershop buy {key}`")

        embed = _shop_embed(
            f"🌹 {config.BOT_NAME} Shop",
            balance_line + "\n" + "\n".join(lines),
        )
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------
    # $servershop setup (owner/staff, one-time per server)
    # ---------------------------------------------------------------

    @servershop.command(
        name="setup", description="[Owner only] Create the prestige roles for this server."
    )
    @commands.is_owner()
    async def setup_shop(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        gdata = self._guild_data(guild.id)
        existing = gdata.get("prestige_role_ids", {})

        created, skipped = [], []
        for key, item in PRESTIGE_ITEMS.items():
            if key in existing and guild.get_role(existing[key]) is not None:
                skipped.append(item["label"])
                continue
            try:
                role = await guild.create_role(
                    name=item["role_name"],
                    color=discord.Color(item["role_color"]),
                    reason="Shop prestige item setup",
                )
            except discord.Forbidden:
                await ctx.send(
                    "I don't have permission to create roles here. "
                    "I need the Manage Roles permission."
                )
                return
            existing[key] = role.id
            created.append(item["label"])

        gdata["prestige_role_ids"] = existing
        self._save_guild_data(guild.id, gdata)

        msg = []
        if created:
            msg.append(f"Created: {', '.join(created)}")
        if skipped:
            msg.append(f"Already existed, left alone: {', '.join(skipped)}")
        await ctx.send(
            "Shop setup complete.\n" + "\n".join(msg)
            + "\n\nDrag these roles below my own role if they aren't already, "
            "so I'm able to assign them on purchase."
        )

    @setup_shop.error
    async def setup_shop_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.NotOwner):
            await ctx.send("This command is owner-only.")
        else:
            raise error

    # ---------------------------------------------------------------
    # $servershop inventory
    # ---------------------------------------------------------------

    @servershop.command(name="inventory", description="See what you own from the shop.")
    async def inventory(self, ctx: commands.Context) -> None:
        gdata = self._guild_data(ctx.guild.id)
        record = self._get_purchase_record(gdata, ctx.author.id)
        self._save_guild_data(ctx.guild.id, gdata)  # persist auto-created record

        lines = []

        if record["custom_role_id"]:
            role = ctx.guild.get_role(record["custom_role_id"])
            lines.append(f"🎨 Custom Role: {role.mention if role else '*deleted*'}")

        if record["reactor_emojis"]:
            lines.append(f"🔔 Auto-Reactor: {' '.join(record['reactor_emojis'])}")

        for key in record["prestige"]:
            item = PRESTIGE_ITEMS.get(key)
            if item:
                lines.append(item["label"])

        if not lines:
            lines.append("*Nothing yet — check `$servershop` to see what's available.*")

        embed = _shop_embed(f"🌹 {ctx.author.display_name}'s Inventory", "\n".join(lines))
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------
    # $servershop buy <item> [...args]
    # ---------------------------------------------------------------

    @servershop.command(
        name="buy",
        description="Buy an item: customrole <name> <#color> | reactor <emoji> | a prestige key",
    )
    async def buy(self, ctx: commands.Context, item: str, *, args: str = "") -> None:
        item = item.lower()

        if item in ("customrole", "custom_role", "custom-role"):
            await self._buy_custom_role(ctx, args)
        elif item in ("reactor", "autoreactor", "auto_reactor", "auto-reactor"):
            await self._buy_reactor(ctx, args)
        elif item in PRESTIGE_ITEMS:
            await self._buy_prestige(ctx, item)
        else:
            valid = ", ".join(["customrole", "reactor", *PRESTIGE_ITEMS.keys()])
            await ctx.send(f"Unknown item `{item}`. Valid items: {valid}")

    async def _buy_custom_role(self, ctx: commands.Context, args: str) -> None:
        parts = args.rsplit(maxsplit=1)
        if len(parts) != 2:
            await ctx.send(
                "Usage: `$servershop buy customrole <name> <#hexcolor>`\n"
                "Example: `$servershop buy customrole Nightshade #8B0000`"
            )
            return

        name, color_raw = parts
        name = name.strip()
        if not name or len(name) > 100:
            await ctx.send("Role name must be 1–100 characters.")
            return

        if not HEX_COLOR_RE.match(color_raw.strip()):
            await ctx.send(f"`{color_raw}` isn't a valid hex color (e.g. `#8B0000`).")
            return
        color_value = int(color_raw.strip().lstrip("#"), 16)

        gdata = self._guild_data(ctx.guild.id)
        record = self._get_purchase_record(gdata, ctx.author.id)

        if record["custom_role_id"] and ctx.guild.get_role(record["custom_role_id"]):
            await ctx.send(
                "You already have a custom role. Ask an admin to remove it first "
                "if you want to buy a new one."
            )
            return

        if not await self._charge(ctx, CUSTOM_ROLE_PRICE):
            return

        try:
            role = await ctx.guild.create_role(
                name=name, color=discord.Color(color_value), reason=f"Shop custom role for {ctx.author}"
            )
            await ctx.author.add_roles(role, reason="Shop custom role purchase")
        except discord.Forbidden:
            # Refund since the purchase didn't actually deliver anything
            economy = self._economy()
            economy.set_balance(ctx.author.id, economy.get_balance(ctx.author.id) + CUSTOM_ROLE_PRICE)
            await ctx.send(
                "I don't have permission to create/assign roles here, so your "
                f"purchase was refunded ({_fmt(CUSTOM_ROLE_PRICE)})."
            )
            return

        record["custom_role_id"] = role.id
        self._save_guild_data(ctx.guild.id, gdata)

        embed = _shop_embed(
            "🎨 Custom Role Purchased!",
            f"{ctx.author.mention} now has {role.mention}.",
            color=color_value,
        )
        await ctx.send(embed=embed)

    async def _buy_reactor(self, ctx: commands.Context, emoji_raw: str) -> None:
        emoji_raw = emoji_raw.strip()
        if not emoji_raw:
            await ctx.send("Usage: `$servershop buy reactor <emoji>`")
            return

        gdata = self._guild_data(ctx.guild.id)
        record = self._get_purchase_record(gdata, ctx.author.id)

        if emoji_raw in record["reactor_emojis"]:
            await ctx.send("You already have that reactor emoji.")
            return

        if len(record["reactor_emojis"]) >= MAX_REACTOR_EMOJIS:
            await ctx.send(
                f"You already have the max of {MAX_REACTOR_EMOJIS} reactor emojis. "
                "Ask an admin to clear one first."
            )
            return

        # Validate the emoji is actually usable by trying to react with it
        # to the invoking message itself — cheap, immediate feedback if
        # it's an emoji from a server the bot can't access.
        try:
            await ctx.message.add_reaction(emoji_raw)
        except discord.HTTPException:
            await ctx.send(
                f"`{emoji_raw}` doesn't look like a usable emoji — either it's not "
                "valid, or it's a custom emoji from a server I'm not in."
            )
            return

        if not await self._charge(ctx, AUTO_REACTOR_PRICE):
            return

        record["reactor_emojis"].append(emoji_raw)
        self._save_guild_data(ctx.guild.id, gdata)

        embed = _shop_embed(
            "🔔 Auto-Reactor Purchased!",
            f"{ctx.author.mention}, I'll now react with {emoji_raw} whenever your "
            "name is mentioned in chat.",
        )
        await ctx.send(embed=embed)

    async def _buy_prestige(self, ctx: commands.Context, key: str) -> None:
        item = PRESTIGE_ITEMS[key]
        gdata = self._guild_data(ctx.guild.id)
        record = self._get_purchase_record(gdata, ctx.author.id)

        if key in record["prestige"]:
            await ctx.send(f"You already own {item['label']}.")
            return

        role_id = gdata.get("prestige_role_ids", {}).get(key)
        role = ctx.guild.get_role(role_id) if role_id else None
        if role is None:
            await ctx.send(
                f"{item['label']} hasn't been set up on this server yet. "
                "An admin needs to run `$servershop setup` first."
            )
            return

        hierarchy_error = self._role_assignable(ctx.guild, role)
        if hierarchy_error:
            await ctx.send(hierarchy_error)
            return

        if not await self._charge(ctx, item["price"]):
            return

        try:
            await ctx.author.add_roles(role, reason=f"Shop purchase: {item['label']}")
        except discord.Forbidden:
            economy = self._economy()
            economy.set_balance(ctx.author.id, economy.get_balance(ctx.author.id) + item["price"])
            await ctx.send(
                f"I couldn't assign that role, so your purchase was refunded "
                f"({_fmt(item['price'])})."
            )
            return

        record["prestige"].append(key)
        self._save_guild_data(ctx.guild.id, gdata)

        embed = _shop_embed(
            f"{item['label']} Purchased!",
            f"{ctx.author.mention} has ascended. Welcome to the ranks of "
            f"**{item['label']}**.",
            color=item["role_color"],
        )
        await ctx.send(embed=embed)

    # ---------------------------------------------------------------
    # auto-reactor listener
    # ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        gdata = self._guild_data(message.guild.id)
        purchases = gdata.get("purchases", {})
        if not purchases:
            return

        content_lower = message.content.lower()
        mentioned_ids = {m.id for m in message.mentions}

        for user_id_str, record in purchases.items():
            emojis = record.get("reactor_emojis")
            if not emojis:
                continue

            user_id = int(user_id_str)
            if user_id == message.author.id:
                continue  # don't react to people mentioning themselves

            member = message.guild.get_member(user_id)
            if member is None:
                continue

            name_hit = (
                member.display_name.lower() in content_lower
                or member.name.lower() in content_lower
            )
            mention_hit = user_id in mentioned_ids

            if not (name_hit or mention_hit):
                continue

            for emoji in emojis:
                try:
                    await message.add_reaction(emoji)
                except discord.HTTPException:
                    logger.warning(
                        "Auto-reactor: failed to react with %s for user %s in guild %s",
                        emoji,
                        user_id,
                        message.guild.id,
                    )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Shop(bot))