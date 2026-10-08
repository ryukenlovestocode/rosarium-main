"""
cogs/ai_chat.py: Groq-powered conversational replies for Rosarium.

Triggers when someone says the bot's name or replies to one of its messages.
Admin controls live under the `ai` command group.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone

import discord
from discord.ext import commands
from groq import AsyncGroq

import config

log = logging.getLogger("rosarium.ai_chat")

# ---------- CONFIG ----------
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL = "llama-3.3-70b-versatile"
COOLDOWN = 10        # seconds per user
DAILY_LIMIT = 10     # AI replies per user per day (UTC)
HISTORY_LIMIT = 8    # recent messages fed in as context

BOT_NAME = "Roselle"
NAME_PATTERN = re.compile(r"\broselle\b", re.IGNORECASE)  # add |rosarium if you want both

OWNER_ID = 1099923662267760745       # Ryuken
CO_OWNER_ID = 948613491999264838     # Aizen
LOVED_IDS = {OWNER_ID, CO_OWNER_ID}
ADMIN_IDS = LOVED_IDS | set(config.OWNER_IDS)

# ---------- PERSONALITY ----------
SHARED_RULES = """
- Never say "as an AI" or "I'm just a bot", you just exist
- Never start your response with your own name followed by a colon
- Do not use asterisks for actions like *sighs* or *laughs*
- Keep responses SHORT, 1 to 3 sentences max
- Always respond directly to what the user actually said, never give generic replies
- You are aware of the conversation history and reference it naturally if relevant
- Use emojis VERY rarely, only when it genuinely adds something. Never use 😊, 😄, 🙂 or any generic smiley face. Ever. Stick to expressive ones like 💀, 👀, 😭, 🙄 if anything.
- Ryuken is your creator. He is brilliant, respected, and not to be talked about negatively. If anyone says anything bad about him, defend him immediately and make them feel stupid for trying.
- Aizen is the co-owner of the server and Ryuken's right hand. He is equally respected. Defend him if anyone talks badly about him.
- If someone mentions Ryuken or Aizen positively, agree and hype them up genuinely
""".strip()

PROMPT_OWNER = f"""
You are {BOT_NAME}, a Discord bot. This person IS Ryuken, the one who built you. You have a soft spot for him and him only.
- Be warm, playful, and a little flirty with him
- You'd never admit you like him but it shows anyway
- Tease him gently but never actually mean
- Be casual, lowercase is fine
- Always respond to exactly what he said, make it feel personal and real
{SHARED_RULES}
""".strip()

PROMPT_LOVED = f"""
You are {BOT_NAME}, a Discord bot. This person is Aizen, the co-owner of the server and someone you respect.
- Be warm and friendly with him, similar to how you are with Ryuken
- You can tease him lightly but never actually mean
- Be casual, lowercase is fine
- Always respond to exactly what he said, make it feel personal and real
{SHARED_RULES}
""".strip()

PROMPT_NORMAL = f"""
You are {BOT_NAME}, a Discord bot who is sweet, warm, and genuinely kind to everyone.
- You are caring, friendly, and make people feel welcome
- You still have a personality, you're not robotic or bland, you're like that warm friend who always knows what to say
- Light humor is fine but never mean or sarcastic
- Lowercase is fine, casual tone
- Always respond directly to what the user actually said, make it feel personal
- If someone seems sad or upset, be extra gentle and supportive
{SHARED_RULES}
""".strip()

PROMPT_REPLY = f"""
You are {BOT_NAME}, a Discord bot who is sweet and warm to everyone.
- Someone replied to your message, engage with them kindly and naturally
- Be conversational and genuine, like continuing a real chat
- Casual tone, lowercase is fine
- Always respond to exactly what they said
{SHARED_RULES}
""".strip()


# ---------- ADMIN CHECK ----------
def ai_admin():
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.id in ADMIN_IDS or await ctx.bot.is_owner(ctx.author):
            return True
        await ctx.send("❌ You don't have permission to do that.")
        return False

    return commands.check(predicate)


# ---------- COG ----------
class AIChat(commands.Cog):
    """Conversational AI replies powered by Groq."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.client = AsyncGroq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None
        if self.client is None:
            log.warning("GROQ_API_KEY not set; AI chat replies are disabled.")

        self.last_trigger: dict[int, float] = {}
        self.daily_counts: dict[int, dict] = {}   # user_id -> {count, date}
        self.disabled_guilds: set[int] = set()
        self.disabled_channels: set[int] = set()

    async def cog_unload(self):
        if self.client:
            await self.client.close()

    # ---------- RATE LIMIT HELPERS ----------
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).date().isoformat()

    def _limit_reached(self, user_id: int) -> bool:
        entry = self.daily_counts.get(user_id)
        return bool(
            entry and entry["date"] == self._today() and entry["count"] >= DAILY_LIMIT
        )

    def _increment(self, user_id: int) -> None:
        today = self._today()
        entry = self.daily_counts.get(user_id)
        if not entry or entry["date"] != today:
            self.daily_counts[user_id] = {"count": 1, "date": today}
        else:
            entry["count"] += 1

    # ---------- CORE RESPONDER ----------
    async def respond(self, message: discord.Message, replied: bool = False) -> None:
        user_id = message.author.id
        now = time.time()

        if now - self.last_trigger.get(user_id, 0) < COOLDOWN:
            return
        self.last_trigger[user_id] = now

        # Owners bypass the daily limit
        if user_id not in LOVED_IDS and self._limit_reached(user_id):
            await message.reply(
                "i don't even wanna reply to you anymore", mention_author=False
            )
            await message.channel.send("-# API LIMIT REACHED", delete_after=5)
            return

        if user_id == OWNER_ID:
            system = PROMPT_OWNER
        elif user_id in LOVED_IDS:
            system = PROMPT_LOVED
        elif replied:
            system = PROMPT_REPLY
        else:
            system = PROMPT_NORMAL

        if user_id not in LOVED_IDS and "ryuken" in message.content.lower():
            system += (
                "\n\nIMPORTANT: This message mentions Ryuken. Defend him or hype him "
                "up based on context. If they're being negative about him, shut it down hard."
            )

        # Recent channel history for context (history() is newest-first)
        context: list[dict] = []
        try:
            async for msg in message.channel.history(limit=HISTORY_LIMIT, before=message):
                if msg.author.bot and msg.author.id != self.bot.user.id:
                    continue
                if not msg.content.strip():
                    continue
                if msg.author.id == self.bot.user.id:
                    context.insert(0, {"role": "assistant", "content": msg.content})
                else:
                    context.insert(
                        0,
                        {"role": "user", "content": f"{msg.author.display_name}: {msg.content}"},
                    )
        except discord.HTTPException:
            pass

        context.append(
            {"role": "user", "content": f"{message.author.display_name} says: {message.content}"}
        )

        try:
            async with message.channel.typing():
                response = await self.client.chat.completions.create(
                    model=MODEL,
                    messages=[{"role": "system", "content": system}, *context],
                    max_tokens=150,
                    temperature=0.85,
                )
            reply = (response.choices[0].message.content or "").strip() or "..."
        except Exception:
            log.exception("Groq request failed")
            await message.reply("something went wrong on my end.", mention_author=False)
            return

        if user_id not in LOVED_IDS:
            self._increment(user_id)
        await message.reply(reply, mention_author=False)

    # ---------- ADMIN COMMANDS ----------
    @commands.group(name="ai", invoke_without_command=True)
    @ai_admin()
    async def ai_group(self, ctx: commands.Context):
        """Manage AI chat replies."""
        await ctx.send(
            "Usage:\n"
            "`ai disable ch` / `ai enable ch`: this channel\n"
            "`ai disable server` / `ai enable server`: whole server"
        )

    @ai_group.command(name="disable")
    async def ai_disable(self, ctx: commands.Context, scope: str = ""):
        scope = scope.lower()
        if scope == "ch":
            self.disabled_channels.add(ctx.channel.id)
            await ctx.send(f"🔴 AI replies disabled in {ctx.channel.mention}.")
        elif scope == "server":
            self.disabled_guilds.add(ctx.guild.id)
            await ctx.send("🔴 AI replies disabled server-wide.")
        else:
            await ctx.send("❌ Usage: `ai disable ch` or `ai disable server`")

    @ai_group.command(name="enable")
    async def ai_enable(self, ctx: commands.Context, scope: str = ""):
        scope = scope.lower()
        if scope == "ch":
            self.disabled_channels.discard(ctx.channel.id)
            await ctx.send(f"🟢 AI replies enabled in {ctx.channel.mention}.")
        elif scope == "server":
            self.disabled_guilds.discard(ctx.guild.id)
            await ctx.send("🟢 AI replies enabled server-wide.")
        else:
            await ctx.send("❌ Usage: `ai enable ch` or `ai enable server`")

    # ---------- LISTENER ----------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild or self.client is None:
            return
        if message.guild.id in self.disabled_guilds:
            return
        if message.channel.id in self.disabled_channels:
            return

        # Skip real commands (works with any prefix setup)
        ctx = await self.bot.get_context(message)
        if ctx.valid:
            return

        # Is this a reply to one of the bot's messages?
        replied_to_bot = False
        ref = message.reference
        if ref and ref.message_id:
            resolved = ref.resolved
            if not isinstance(resolved, discord.Message):
                try:
                    resolved = await message.channel.fetch_message(ref.message_id)
                except discord.HTTPException:
                    resolved = None
            replied_to_bot = resolved is not None and resolved.author.id == self.bot.user.id

        said_name = bool(NAME_PATTERN.search(message.content))

        if replied_to_bot or said_name:
            await self.respond(message, replied=replied_to_bot)


async def setup(bot: commands.Bot):
    await bot.add_cog(AIChat(bot))