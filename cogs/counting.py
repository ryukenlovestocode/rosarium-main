"""
Counting cog — a counting game for a channel.

`$countstart` (also `/countstart`) turns the channel it's run in into a
counting channel. From then on:

  - Members count up from 1, one number per message.
  - The same member can't count twice in a row.
  - A wrong number, or a double count, breaks the chain: the count goes
    back to 1 and anyone can start again.
  - Every correct number gets a ✅ reaction from the bot.
  - Plain arithmetic is accepted as long as it evaluates to the expected
    number: after 6, both `7` and `4+3` count. `5+3` would be 8, so it
    would break the chain. Supported: + - * / ** and parentheses.
  - Anything that isn't a number or arithmetic ("lol", "nice one") is
    ignored, so people can still talk in the channel.

Who can start it: bot owners, or members with Manage Channels. Running it
in a channel that's already counting just reports the current number; it
never resets a game in progress.

Edits and deletes: if someone deletes or edits the most recent counted
message, the bot says what the current number is, so nobody has to guess
what the missing/changed message was.

State is in-memory only, same as AFK / sticky messages elsewhere in the
bot: after a restart, run `$countstart` again.

Expressions are parsed by a small hand-written parser, never `eval`, with
caps on length, exponent size and result size, so a message like `9**9**9`
can't hang the bot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction

import discord
from discord.ext import commands

import config

CORRECT_EMOJI = "✅"
WRONG_EMOJI = "❌"

# ---------- EXPRESSION EVALUATION ----------

MAX_EXPR_LEN = 60     # longer messages are treated as chatter
MAX_EXPONENT = 64     # |exponent| above this is rejected
MAX_BITS = 2048       # cap on the size of any intermediate result

_TOKEN = re.compile(r"\s*(?:([0-9]+)|(\*\*|[-+*/()]))")


class _BadExpression(Exception):
    """Not a valid (or not a safe) arithmetic expression."""


def _tokenize(text: str) -> list[tuple[str, object]]:
    tokens: list[tuple[str, object]] = []
    pos = 0
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if match is None:
            raise _BadExpression
        number, op = match.groups()
        tokens.append(("n", int(number)) if number is not None else ("o", op))
        pos = match.end()
    return tokens


def _check_size(value: Fraction) -> Fraction:
    if max(value.numerator.bit_length(), value.denominator.bit_length()) > MAX_BITS:
        raise _BadExpression
    return value


def _power(base: Fraction, exponent: Fraction) -> Fraction:
    if exponent.denominator != 1:
        raise _BadExpression  # roots aren't supported
    e = exponent.numerator
    if abs(e) > MAX_EXPONENT:
        raise _BadExpression
    if base == 0 and e < 0:
        raise ZeroDivisionError
    bits = max(base.numerator.bit_length(), base.denominator.bit_length(), 1)
    if bits * abs(e) > MAX_BITS:
        raise _BadExpression
    return _check_size(base ** e)


class _Parser:
    """
    expr   := term (('+' | '-') term)*
    term   := unary (('*' | '/') unary)*
    unary  := ('+' | '-') unary | power
    power  := atom ('**' unary)?          (right-associative)
    atom   := INTEGER | '(' expr ')'
    """

    def __init__(self, tokens: list[tuple[str, object]]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _take(self):
        tok = self._peek()
        if tok is not None:
            self.pos += 1
        return tok

    def _match(self, *ops: str):
        tok = self._peek()
        if tok is not None and tok[0] == "o" and tok[1] in ops:
            self.pos += 1
            return tok[1]
        return None

    def parse(self) -> Fraction:
        value = self._expr()
        if self._peek() is not None:  # leftovers, e.g. "1 2"
            raise _BadExpression
        return value

    def _expr(self) -> Fraction:
        value = self._term()
        while True:
            op = self._match("+", "-")
            if op is None:
                return value
            rhs = self._term()
            value = _check_size(value + rhs if op == "+" else value - rhs)

    def _term(self) -> Fraction:
        value = self._unary()
        while True:
            op = self._match("*", "/")
            if op is None:
                return value
            rhs = self._unary()
            if op == "*":
                value = _check_size(value * rhs)
            else:
                if rhs == 0:
                    raise ZeroDivisionError
                value = _check_size(value / rhs)

    def _unary(self) -> Fraction:
        op = self._match("+", "-")
        if op is not None:
            value = self._unary()
            return -value if op == "-" else value
        return self._power()

    def _power(self) -> Fraction:
        base = self._atom()
        if self._match("**") is not None:
            return _power(base, self._unary())
        return base

    def _atom(self) -> Fraction:
        tok = self._take()
        if tok is None:
            raise _BadExpression
        if tok[0] == "n":
            return _check_size(Fraction(tok[1]))
        if tok == ("o", "("):
            value = self._expr()
            if self._match(")") is None:
                raise _BadExpression
            return value
        raise _BadExpression


def evaluate(text: str) -> Fraction | None:
    """
    Evaluate a message as arithmetic. Returns the exact value, or None if
    the text isn't a number/expression (ordinary chatter, bad syntax,
    division by zero, absurdly large results, ...).
    """
    text = text.strip()
    if not text or len(text) > MAX_EXPR_LEN:
        return None
    try:
        return _Parser(_tokenize(text)).parse()
    except (_BadExpression, ZeroDivisionError, RecursionError, OverflowError):
        return None


# ---------- GAME STATE ----------


@dataclass
class CountState:
    next_number: int = 1
    last_user_id: int | None = None
    last_message_id: int | None = None
    record: int = 0

    def reset(self) -> None:
        self.next_number = 1
        self.last_user_id = None
        self.last_message_id = None


# ---------- COG ----------


class Counting(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.games: dict[int, CountState] = {}  # channel/thread id -> state

    # ---------- CHECKS ----------

    async def cog_check(self, ctx: commands.Context) -> bool:
        if ctx.guild is None:
            raise commands.CheckFailure("This command only works inside a server.")
        if await ctx.bot.is_owner(ctx.author):
            return True
        if isinstance(ctx.author, discord.Member) and ctx.author.guild_permissions.manage_channels:
            return True
        raise commands.CheckFailure("You need the **Manage Channels** permission to start counting.")

    async def cog_command_error(self, ctx: commands.Context, error: commands.CommandError):
        if isinstance(error, commands.CheckFailure):
            await ctx.send(str(error), ephemeral=True)
        else:
            raise error

    # ---------- COMMAND ----------

    @commands.hybrid_command(name="countstart", description="Start the counting game in this channel.")
    async def countstart(self, ctx: commands.Context):
        channel = ctx.channel
        if not isinstance(channel, (discord.TextChannel, discord.Thread)):
            return await ctx.send("Counting can only run in a text channel or thread.", ephemeral=True)

        state = self.games.get(channel.id)
        if state is not None:
            return await ctx.send(
                f"Counting is already running here — the next number is **{state.next_number}**.",
                ephemeral=True,
            )

        missing = self._missing_permissions(channel)
        if missing:
            return await ctx.send(
                "I can't run the count here — I'm missing: " + ", ".join(f"**{m}**" for m in missing) + ".",
                ephemeral=True,
            )

        self.games[channel.id] = CountState()

        embed = discord.Embed(
            title="🔢  Counting Started",
            description=(
                "Count up from **1**, one number per message.\n"
                "> You can't count twice in a row.\n"
                "> Math works too — after 6, `4+3` counts as 7.\n"
                "> A wrong number or a double count sends us back to **1**."
            ),
            color=config.EMBED_COLOR,
        )
        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    @staticmethod
    def _missing_permissions(channel: discord.abc.GuildChannel | discord.Thread) -> list[str]:
        perms = channel.permissions_for(channel.guild.me)
        can_send = perms.send_messages_in_threads if isinstance(channel, discord.Thread) else perms.send_messages
        checks = [
            ("Send Messages", can_send),
            ("Embed Links", perms.embed_links),
            ("Add Reactions", perms.add_reactions),
            ("Read Message History", perms.read_message_history),
        ]
        return [name for name, ok in checks if not ok]

    # ---------- THE GAME ----------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or message.guild is None:
            return

        state = self.games.get(message.channel.id)
        if state is None:
            return

        value = evaluate(message.content)
        if value is None:
            return  # ordinary chatter, not a count attempt

        # Judge and update state in one synchronous step (no awaits), so two
        # messages arriving back to back can never both be judged against
        # the same state.
        expected = state.next_number
        if message.author.id == state.last_user_id:
            failure = "repeat"
        elif value != expected:
            failure = "wrong"
        else:
            failure = None

        if failure is None:
            state.last_user_id = message.author.id
            state.last_message_id = message.id
            state.next_number = expected + 1
            state.record = max(state.record, expected)
            await self._react(message, CORRECT_EMOJI)
            return

        reached = expected - 1
        record = state.record
        state.reset()
        await self._react(message, WRONG_EMOJI)
        await self._announce_reset(message, failure, expected, reached, record)

    async def _announce_reset(
        self,
        message: discord.Message,
        failure: str,
        expected: int,
        reached: int,
        record: int,
    ) -> None:
        who = message.author.mention
        said = " ".join(message.content.split())

        if failure == "repeat":
            lines = [f"{who} counted twice in a row."]
        else:
            lines = [f"{who} said `{said}`, but the next number was **{expected}**."]

        if reached > 0:
            lines.append(f"The chain broke at **{reached}**. Back to **1** — anyone can start.")
        else:
            lines.append("The count starts at **1**.")

        embed = discord.Embed(
            title="💥  Count Broken",
            description="\n".join(lines),
            color=config.EMBED_COLOR,
        )
        footer = config.FOOTER_TEXT if record == 0 else f"{config.FOOTER_TEXT} · Record: {record}"
        embed.set_footer(text=footer)

        try:
            await message.channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            pass

    @staticmethod
    async def _react(message: discord.Message, emoji: str) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass  # message deleted already, or reactions blocked

    # ---------- DELETED / EDITED COUNTS ----------

    @commands.Cog.listener()
    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent):
        await self._warn_altered(payload.channel_id, {payload.message_id}, "deleted")

    @commands.Cog.listener()
    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent):
        await self._warn_altered(payload.channel_id, payload.message_ids, "deleted")

    @commands.Cog.listener()
    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent):
        content = payload.data.get("content")
        if content is None:
            return  # embed unfurl or other non-content update

        state = self.games.get(payload.channel_id)
        if state is None or state.last_message_id != payload.message_id:
            return
        if evaluate(content) == state.next_number - 1:
            return  # still the same number, nothing to clear up

        await self._warn_altered(payload.channel_id, {payload.message_id}, "edited")

    async def _warn_altered(self, channel_id: int, message_ids: set[int], verb: str) -> None:
        state = self.games.get(channel_id)
        if state is None or state.last_message_id not in message_ids:
            return

        user_id = state.last_user_id
        last = state.next_number - 1
        state.last_message_id = None  # warn once per counted message

        channel = self.bot.get_channel(channel_id)
        if channel is None:
            return

        try:
            await channel.send(
                f"<@{user_id}> {verb} their number. The last number was **{last}** — "
                f"the next number is **{last + 1}**.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass


# ---------- SETUP ----------


async def setup(bot: commands.Bot):
    await bot.add_cog(Counting(bot))