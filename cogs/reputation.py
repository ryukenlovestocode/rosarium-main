"""
Reputation cog — word-based reputation, a leaderboard, and a $profile card.

How it works:
  Moderators maintain two word lists ("good" and "bad"). Whenever a member
  says a good word in chat their reputation goes up; a bad word and it goes
  down. Matching is case-insensitive and whole-word only, so "ass" won't
  fire on "class". Phrases with spaces work too ("thank you").

Anti-abuse (all tunable in the constants below):
  - At most one change per message (one good hit and/or one bad hit,
    no matter how many times the word is repeated).
  - A per-user cooldown, so spamming "thanks thanks thanks" doesn't farm rep.
  - Bot messages, DMs, and command invocations are ignored. That last one
    matters: when a mod runs `$rep addbad <word>`, the message itself
    contains the word and must not be counted against them.

Data persistence:
  data/reputation.json via storage.JSONStore (same pattern as economy.py,
  so it lives next to data/economy.json). Only the plain get/set interface
  is used, so it keeps working if storage.py is swapped for SQLite later.

Commands (prefix `$` or slash):
  reputation [member]            — alias: rep. Shows reputation (default: you)
  reputation check [member]      — same as above
  reputation leaderboard         — aliases: lb, top. Top 10 by reputation
  reputation addgood <word>      — [mod] add a word that earns reputation
  reputation addgood mass        — [mod] open a form to add many good words at once
  reputation addbad <word>       — [mod] add a word that costs reputation
  reputation addbad mass         — [mod] open a form to add many bad words at once
  reputation removegood <word>   — [mod] remove a good word
  reputation removebad <word>    — [mod] remove a bad word
  reputation words               — [mod] show both lists
  profile [member]               — profile card: petals, reputation, ranks, dates

"Mod" means: bot owner (OWNER_IDS), server administrator, or a member with
config.MOD_ROLE_ID.
"""

import logging
import os
import re
import time
from typing import Final

import discord
from discord.ext import commands

import config
from storage import JSONStore

log = logging.getLogger("rosarium.reputation")

# ---------- CONSTANTS ----------

GOOD_DELTA: Final = 1               # reputation gained for a message with a good word
BAD_DELTA: Final = -1               # reputation lost for a message with a bad word
REP_COOLDOWN_SECONDS: Final = 10    # min seconds between reputation changes per user
REACT_ON_CHANGE: Final = False      # True = bot reacts 🌹 / 🥀 to messages that changed rep
LEADERBOARD_SIZE: Final = 10

MIN_WORD_LEN: Final = 2
MAX_WORD_LEN: Final = 40
MAX_WORDS_PER_LIST: Final = 200

# (minimum reputation, title) — checked top to bottom.
REP_TIERS: Final = [
    (250, "🌹 Beloved"),
    (100, "🌸 Cherished"),
    (25, "🌷 Welcome Guest"),
    (-24, "🍃 Unnoticed"),
    (-99, "🥀 Withering"),
]
REP_TIER_FLOOR: Final = "🗡️ Thorned"

CURRENCY: Final = "petals"  # matches cogs/economy.py

DATA_PATH = os.path.join(os.path.dirname(__file__), "data", "reputation.json")


# ---------- HELPERS ----------

def rep_title(rep: int) -> str:
    for threshold, title in REP_TIERS:
        if rep >= threshold:
            return title
    return REP_TIER_FLOOR


def _embed(
    title: str,
    color: int,
    *,
    description: str | None = None,
    fields: list[tuple[str, str, bool]] | None = None,
    thumbnail: str | None = None,
) -> discord.Embed:
    """Same shape as economy.py's _embed, so the two cogs look alike."""
    embed = discord.Embed(title=title, color=color)
    if description:
        embed.description = description
    for name, value, inline in fields or []:
        embed.add_field(name=name, value=value, inline=inline)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    embed.set_footer(text=config.FOOTER_TEXT)
    return embed


def is_rep_manager():
    """Owner, server administrator, or the configured mod role."""

    async def predicate(ctx: commands.Context) -> bool:
        if await ctx.bot.is_owner(ctx.author):
            return True
        perms = getattr(ctx.author, "guild_permissions", None)
        if perms and perms.administrator:
            return True
        if config.MOD_ROLE_ID and any(
            role.id == config.MOD_ROLE_ID for role in getattr(ctx.author, "roles", [])
        ):
            return True
        raise commands.CheckFailure("Only moderators can manage the reputation word lists.")

    return commands.check(predicate)


def _build_pattern(words: list[str]) -> re.Pattern | None:
    """Compile a whole-word, case-insensitive matcher for a list of words/phrases."""
    if not words:
        return None
    # Longest first so "thank you" wins over "thank"; spaces match any whitespace run.
    parts = sorted((re.escape(w).replace(r"\ ", r"\s+") for w in words), key=len, reverse=True)
    return re.compile(r"(?<!\w)(?:" + "|".join(parts) + r")(?!\w)", re.IGNORECASE)


def _normalize_word(raw: str) -> str | None:
    word = " ".join(raw.lower().split())
    if not (MIN_WORD_LEN <= len(word) <= MAX_WORD_LEN):
        return None
    return word


def _fmt_words(words: list[str], spoiler: bool, limit: int = 1000) -> str:
    """Format a word list for an embed field / message (stays under `limit` chars)."""
    if not words:
        return "*None yet.*"
    items = [f"||{w}||" if spoiler else f"`{w}`" for w in sorted(words)]
    out: list[str] = []
    length = 0
    for i, item in enumerate(items):
        if length + len(item) + 2 > limit:
            out.append(f"…and {len(items) - i} more")
            break
        out.append(item)
        length += len(item) + 2
    return ", ".join(out)


def _split_words(text: str) -> list[str]:
    """Split pasted text on newlines and commas; drop blanks and duplicates, keep order."""
    seen: set[str] = set()
    out: list[str] = []
    for chunk in re.split(r"[\n,]+", text):
        word = " ".join(chunk.lower().split())
        if word and word not in seen:
            seen.add(word)
            out.append(word)
    return out


def _summarize_add(kind: str, added: list[str], skipped: dict[str, list[str]]) -> str:
    """Result message for a (mass) add. Bad words are spoilered."""
    other = "bad" if kind == "good" else "good"
    spoiler = kind == "bad"
    delta = GOOD_DELTA if kind == "good" else BAD_DELTA

    lines = []
    if added:
        plural = "" if len(added) == 1 else "s"
        lines.append(
            f"✅ Added **{len(added)}** {kind} word{plural} ({delta:+d} reputation): "
            f"{_fmt_words(added, spoiler, 300)}"
        )
    else:
        lines.append("Nothing was added.")

    reasons = [
        ("already", "Already on the list"),
        ("other", f"On the **{other}** list (remove it there first)"),
        ("invalid", f"Wrong length ({MIN_WORD_LEN}–{MAX_WORD_LEN} characters)"),
        ("full", f"List is full ({MAX_WORDS_PER_LIST} words max)"),
    ]
    for key, label in reasons:
        if skipped[key]:
            lines.append(f"⚠️ {label} — {len(skipped[key])}: {_fmt_words(skipped[key], spoiler, 250)}")
    return "\n".join(lines)


# ---------- MASS-ADD FORM ----------

class MassWordsModal(discord.ui.Modal):
    """Paste many words — one per line, or comma-separated — into one form."""

    def __init__(self, cog: "Reputation", kind: str, source_message: discord.Message | None = None):
        super().__init__(title=f"Add {kind} words", timeout=600)
        self.cog = cog
        self.kind = kind
        self.source_message = source_message  # the "Open form" button message (prefix flow)
        delta = GOOD_DELTA if kind == "good" else BAD_DELTA
        self.words = discord.ui.TextInput(
            label=f"{kind.capitalize()} words ({delta:+d} reputation)",
            style=discord.TextStyle.paragraph,
            placeholder="One per line, or separated by commas",
            required=True,
            max_length=4000,
        )
        self.add_item(self.words)

    async def on_submit(self, interaction: discord.Interaction):
        raws = _split_words(str(self.words.value))
        if not raws:
            return await interaction.response.send_message(
                "No words found. Put one per line, or separate them with commas.", ephemeral=True
            )
        added, skipped = self.cog._add_many(self.kind, raws)
        await interaction.response.send_message(_summarize_add(self.kind, added, skipped), ephemeral=True)
        if self.source_message is not None:
            try:
                await self.source_message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.error("Mass-add form failed", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong adding those words.", ephemeral=True)


class OpenFormView(discord.ui.View):
    """Prefix commands can't open a modal directly, so they post this button instead."""

    def __init__(self, cog: "Reputation", kind: str, author_id: int):
        super().__init__(timeout=120)
        self.cog = cog
        self.kind = kind
        self.author_id = author_id
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This form isn't yours.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Open word form", emoji="📝", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(MassWordsModal(self.cog, self.kind, self.message))

    async def on_timeout(self):
        if self.message is not None:
            try:
                await self.message.delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass


# ---------- COG ----------

class Reputation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Structure on disk:
        # {
        #   "users": {"<user_id>": {"rep": 12}},
        #   "words": {"good": ["thanks", ...], "bad": ["idiot", ...]}
        # }
        self.store = JSONStore(DATA_PATH, default={"users": {}, "words": {"good": [], "bad": []}})
        self._last_change: dict[int, float] = {}
        self._good_re: re.Pattern | None = None
        self._bad_re: re.Pattern | None = None
        self._compile()

    # ---------- Storage helpers ----------

    def _users(self) -> dict:
        users = self.store.get("users")
        if not isinstance(users, dict):
            users = {}
            self.store.set("users", users)
        return users

    def _words(self) -> dict:
        words = self.store.get("words")
        if not isinstance(words, dict):
            words = {}
        words.setdefault("good", [])
        words.setdefault("bad", [])
        return words

    def _compile(self):
        words = self._words()
        self._good_re = _build_pattern(words["good"])
        self._bad_re = _build_pattern(words["bad"])

    def get_rep(self, user_id: int) -> int:
        return int(self._users().get(str(user_id), {}).get("rep", 0))

    def add_rep(self, user_id: int, delta: int) -> int:
        users = self._users()
        entry = users.setdefault(str(user_id), {"rep": 0})
        entry["rep"] = int(entry.get("rep", 0)) + delta
        self.store.set("users", users)
        return entry["rep"]

    def ranked(self, guild: discord.Guild) -> list[tuple[int, int]]:
        """(user_id, rep) for everyone with a record who is still in this server, best first."""
        rows = []
        for uid, entry in self._users().items():
            if guild.get_member(int(uid)) is None:
                continue
            rows.append((int(uid), int(entry.get("rep", 0))))
        rows.sort(key=lambda row: row[1], reverse=True)
        return rows

    def get_rank(self, guild: discord.Guild, user_id: int) -> int | None:
        for position, (uid, _) in enumerate(self.ranked(guild), start=1):
            if uid == user_id:
                return position
        return None

    @staticmethod
    def _petal_rank(economy: commands.Cog, guild: discord.Guild, user_id: int) -> int | None:
        rank = 0
        for uid, _ in economy.get_top_balances(limit=10**9):
            if guild.get_member(uid) is None:
                continue
            rank += 1
            if uid == user_id:
                return rank
        return None

    # ---------- Chat listener ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None or not message.content:
            return
        if self._good_re is None and self._bad_re is None:
            return

        now = time.monotonic()
        if now - self._last_change.get(message.author.id, float("-inf")) < REP_COOLDOWN_SECONDS:
            return

        # Don't count command invocations (e.g. a mod typing `$rep addbad <word>`).
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        delta = 0
        if self._good_re and self._good_re.search(message.content):
            delta += GOOD_DELTA
        if self._bad_re and self._bad_re.search(message.content):
            delta += BAD_DELTA
        if delta == 0:
            return

        self._last_change[message.author.id] = now
        self.add_rep(message.author.id, delta)

        if REACT_ON_CHANGE:
            try:
                await message.add_reaction("🌹" if delta > 0 else "🥀")
            except (discord.Forbidden, discord.HTTPException):
                pass

    # ---------- Reputation commands ----------

    async def _send_rep_card(self, ctx: commands.Context, member: discord.Member):
        if member.bot:
            return await ctx.send("Bots don't have reputation.")
        rep = self.get_rep(member.id)
        rank = self.get_rank(ctx.guild, member.id)

        embed = _embed(
            "🌹 Reputation",
            config.EMBED_COLOR,
            fields=[
                ("User", member.mention, True),
                ("Reputation", f"**{rep:,}**", True),
                ("Standing", rep_title(rep), True),
                ("Rank", f"`#{rank}`" if rank else "Unranked", True),
            ],
            thumbnail=member.display_avatar.url,
        )
        await ctx.send(embed=embed)

    @commands.hybrid_group(
        name="reputation",
        aliases=["rep"],
        invoke_without_command=True,
        description="Check reputation, see the leaderboard, or manage word lists.",
    )
    async def reputation(self, ctx: commands.Context):
        await self._send_rep_card(ctx, ctx.author)

    @reputation.command(name="check", description="Check your (or someone else's) reputation.")
    async def rep_check(self, ctx: commands.Context, member: discord.Member = None):
        await self._send_rep_card(ctx, member or ctx.author)

    @reputation.command(
        name="leaderboard",
        aliases=["lb", "top"],
        description="Top reputation in this server.",
    )
    async def rep_leaderboard(self, ctx: commands.Context):
        rows = self.ranked(ctx.guild)[:LEADERBOARD_SIZE]
        if not rows:
            return await ctx.send("No reputation recorded yet.")

        medals = ["🥇", "🥈", "🥉"]
        lines = []
        for i, (uid, rep) in enumerate(rows):
            member = ctx.guild.get_member(uid)
            medal = medals[i] if i < 3 else f"`#{i + 1}`"
            lines.append(f"{medal} {member.display_name} — **{rep:,}** · {rep_title(rep)}")

        embed = _embed(
            "🌹 Reputation Leaderboard",
            config.EMBED_COLOR_DARK,
            description=f"The most esteemed members of **{ctx.guild.name}**.\n\n" + "\n".join(lines),
        )
        await ctx.send(embed=embed)

    # ---------- Word list management (mods) ----------

    async def _tidy(self, ctx: commands.Context):
        """Delete the invoking prefix message so a typed word doesn't stay visible."""
        if ctx.interaction is None:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass

    def _add_many(self, kind: str, raws: list[str]) -> tuple[list[str], dict[str, list[str]]]:
        """Add words to a list with a single save. Returns (added, skipped-by-reason)."""
        other = "bad" if kind == "good" else "good"
        words = self._words()
        added: list[str] = []
        skipped: dict[str, list[str]] = {"invalid": [], "already": [], "other": [], "full": []}
        for raw in raws:
            word = _normalize_word(raw)
            if word is None:
                skipped["invalid"].append(" ".join(raw.split())[:MAX_WORD_LEN] or "(blank)")
            elif word in words[other]:
                skipped["other"].append(word)
            elif word in words[kind]:
                skipped["already"].append(word)
            elif len(words[kind]) >= MAX_WORDS_PER_LIST:
                skipped["full"].append(word)
            else:
                words[kind].append(word)
                added.append(word)
        if added:
            self.store.set("words", words)
            self._compile()
        return added, skipped

    async def _add_word(self, ctx: commands.Context, kind: str, raw: str):
        await self._tidy(ctx)
        added, skipped = self._add_many(kind, [raw])

        if added:
            shown = f"||{added[0]}||" if kind == "bad" else f"**{added[0]}**"
            delta = GOOD_DELTA if kind == "good" else BAD_DELTA
            return await ctx.send(
                f"Added {shown} to the **{kind}** words ({delta:+d} reputation).", ephemeral=True
            )

        other = "bad" if kind == "good" else "good"
        if skipped["other"]:
            msg = f"That word is already on the **{other}** list. Remove it there first."
        elif skipped["already"]:
            msg = f"That word is already on the **{kind}** list."
        elif skipped["full"]:
            msg = f"The {kind} list is full ({MAX_WORDS_PER_LIST} words max)."
        else:
            msg = f"Words must be between {MIN_WORD_LEN} and {MAX_WORD_LEN} characters."
        await ctx.send(msg, ephemeral=True)

    async def _open_mass_form(self, ctx: commands.Context, kind: str):
        if ctx.interaction is not None:
            # Slash command: we can open the modal straight away.
            return await ctx.interaction.response.send_modal(MassWordsModal(self, kind))
        # Prefix command: a modal can only answer an interaction, so hand over a button.
        view = OpenFormView(self, kind, ctx.author.id)
        view.message = await ctx.send(
            f"Press the button to add many **{kind}** words at once.", view=view
        )

    async def _remove_word(self, ctx: commands.Context, kind: str, raw: str):
        await self._tidy(ctx)
        word = _normalize_word(raw)
        words = self._words()
        if word is None or word not in words[kind]:
            return await ctx.send(f"That word isn't on the **{kind}** list.", ephemeral=True)

        words[kind].remove(word)
        self.store.set("words", words)
        self._compile()

        shown = f"||{word}||" if kind == "bad" else f"**{word}**"
        await ctx.send(f"Removed {shown} from the **{kind}** words.", ephemeral=True)

    @reputation.group(
        name="addgood",
        invoke_without_command=True,
        fallback="word",
        description="[Mod] Add a word that earns reputation.",
    )
    @is_rep_manager()
    async def rep_addgood(self, ctx: commands.Context, *, word: str):
        await self._add_word(ctx, "good", word)

    @rep_addgood.command(name="mass", description="[Mod] Add many good words at once (opens a form).")
    @is_rep_manager()
    async def rep_addgood_mass(self, ctx: commands.Context):
        await self._open_mass_form(ctx, "good")

    @reputation.group(
        name="addbad",
        invoke_without_command=True,
        fallback="word",
        description="[Mod] Add a word that costs reputation.",
    )
    @is_rep_manager()
    async def rep_addbad(self, ctx: commands.Context, *, word: str):
        await self._add_word(ctx, "bad", word)

    @rep_addbad.command(name="mass", description="[Mod] Add many bad words at once (opens a form).")
    @is_rep_manager()
    async def rep_addbad_mass(self, ctx: commands.Context):
        await self._open_mass_form(ctx, "bad")

    @reputation.command(name="removegood", description="[Mod] Remove a good word.")
    @is_rep_manager()
    async def rep_removegood(self, ctx: commands.Context, *, word: str):
        await self._remove_word(ctx, "good", word)

    @reputation.command(name="removebad", description="[Mod] Remove a bad word.")
    @is_rep_manager()
    async def rep_removebad(self, ctx: commands.Context, *, word: str):
        await self._remove_word(ctx, "bad", word)

    @reputation.command(name="words", description="[Mod] Show the good and bad word lists.")
    @is_rep_manager()
    async def rep_words(self, ctx: commands.Context):
        await self._tidy(ctx)
        words = self._words()
        embed = _embed(
            "🌹 Reputation Words",
            config.EMBED_COLOR_DARK,
            fields=[
                (f"Good ({GOOD_DELTA:+d})", _fmt_words(words["good"], spoiler=False), False),
                (f"Bad ({BAD_DELTA:+d})", _fmt_words(words["bad"], spoiler=True), False),
            ],
        )
        await ctx.send(embed=embed, ephemeral=True)

    # ---------- PROFILE ----------

    @commands.hybrid_command(description="View your profile (or someone else's).")
    async def profile(self, ctx: commands.Context, member: discord.Member = None):
        member = member or ctx.author
        if member.bot:
            return await ctx.send("Bots don't have profiles.")

        rep = self.get_rep(member.id)
        rep_rank = self.get_rank(ctx.guild, member.id)
        rep_value = f"**{rep:,}**" + (f"\nRank `#{rep_rank}`" if rep_rank else "")

        economy = self.bot.get_cog("Economy")
        if economy is not None:
            balance = economy.get_balance(member.id)
            petal_rank = self._petal_rank(economy, ctx.guild, member.id)
            petals_value = f"**{balance:,} {CURRENCY}**" + (f"\nRank `#{petal_rank}`" if petal_rank else "")
        else:
            petals_value = "*Economy unavailable.*"

        joined = discord.utils.format_dt(member.joined_at, "D") if member.joined_at else "Unknown"
        created = discord.utils.format_dt(member.created_at, "D")
        top_role = member.top_role.mention if not member.top_role.is_default() else "None"

        embed = _embed(
            f"🌹 {member.display_name}'s Profile",
            config.EMBED_COLOR,
            fields=[
                ("Petals", petals_value, True),
                ("Reputation", rep_value, True),
                ("Standing", rep_title(rep), True),
                ("Joined", joined, True),
                ("Account Created", created, True),
                ("Top Role", top_role, True),
            ],
            thumbnail=member.display_avatar.url,
        )
        await ctx.send(embed=embed)

    # ---------- Error handling ----------

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        # Having a cog-level handler stops discord.py from printing tracebacks
        # for this cog's commands, so anything unexpected is logged below.
        if ctx.guild is None:
            return  # bot.py's global guild-only check already blocks DMs silently
        error = getattr(error, "original", error)

        if isinstance(error, commands.CheckFailure):
            return await ctx.send(str(error) or "You can't use that command.", ephemeral=True)
        if isinstance(error, commands.MissingRequiredArgument):
            return await ctx.send(
                f"Missing `{error.param.name}`. Usage: "
                f"`{ctx.clean_prefix}{ctx.command.qualified_name} {ctx.command.signature}`",
                ephemeral=True,
            )
        if isinstance(error, commands.BadArgument):
            return await ctx.send(str(error), ephemeral=True)

        log.error("Unhandled error in %s", ctx.command, exc_info=error)


# ---------- SETUP ----------

async def setup(bot: commands.Bot):
    await bot.add_cog(Reputation(bot))