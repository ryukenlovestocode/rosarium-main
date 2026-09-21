"""
General/utility cog — sanity-check commands to confirm the bot
is alive and cogs are loading correctly.

Uses hybrid_command: each command below works BOTH as a prefix
command (!ping) and a slash command (/ping) from one definition.
No need to write two versions of the same command.
"""

import discord
from discord.ext import commands

import config


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.hybrid_command(
        name="ping",
        description="Check if Rosarium is awake."
    )
    async def ping(self, ctx: commands.Context):
        latency_ms = round(self.bot.latency * 1000)

        embed = discord.Embed(
            title="🥀 Still here.",
            description=f"Latency: `{latency_ms}ms`",
            color=config.EMBED_COLOR,
        )

        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)

    @commands.hybrid_command(
        name="about",
        description="About Rosarium."
    )
    async def about(self, ctx: commands.Context):
        embed = discord.Embed(
            title=config.BOT_NAME,
            description="""🌹 Roselle

A quiet keeper of the garden of thorns.

Roselle is the heart of Rosarium's everyday experience — built to keep the community connected, entertained, and running smoothly.

From useful tools and moderation to games, Petals, economy, casino and community features, Roselle brings the essentials of Rosarium together in one place.

╭─────────────── 🌹 ───────────────╮

🌿  General
   Information, bot details & essentials

🕯️  Utility
   Actions, AFK, avatars & quote cards

💞  Fun
   Marriage, shipping & confessions

🌹  Economy
   Petals, daily rewards, payments & rankings

🎲  Casino
   Games of chance, fishing, slots & more

⛓️  Moderation
   Warnings, staff tools & role management

🔒  Owner
   Private tools for maintaining Roselle

╰─────────────── 🌹 ───────────────╯

Built exclusively for Rosarium.
Developed by Ryuken.
Shaped by the ideas & improvements of Anakyn.

⚙️ Always evolving. Always in the garden.""",
            color=config.EMBED_COLOR_DARK,
        )

        embed.set_footer(text=config.FOOTER_TEXT)
        await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(General(bot))