"""
Errors cog — Rosarium's catch-all for command failures.

Why this exists:
  discord.py does NOT message the channel when a command errors out —
  by default it just prints a traceback to the console (which is the
  "silent in Discord, loud in the terminal" behavior you were seeing).
  This cog gives every error a user-facing response AND still logs the
  full traceback for you.

Two separate systems, two separate handlers:
  - Prefix commands (commands.Command, including hybrid commands
    invoked with the prefix) raise commands.CommandError and are
    caught via the on_command_error listener.
  - Slash commands (app_commands.Command, including hybrid commands
    invoked with /) raise app_commands.AppCommandError and are NOT
    covered by on_command_error at all — they need a handler attached
    to bot.tree.on_error. That's why this cog wires that up in
    cog_load rather than just using a listener.

Local error handlers vs. this cog:
  Several commands (.welctest, .stick, .unstick, .addmoney) and the
  Moderation cog already have their own local error handler that deals
  with ONE specific case (usually NotOwner / CheckFailure) and does
  `raise error` for anything else, expecting a fallback to catch it.
  discord.py always fires the command_error event afterwards no matter
  what a local handler does with the error, so this cog still sees it —
  the only thing it needs to know is "was this already fully handled,
  or was it re-raised for me?" Local handlers signal that by setting
  `error.handled = True` right before returning (and NOT setting it
  before a `raise`). This cog checks that flag instead of blindly
  skipping every command/cog that merely has *a* local handler — the
  old approach silently ate any error those handlers didn't recognize.
  See the bottom of this file for the exact one-line additions those
  local handlers need.

View/Select button callbacks:
  discord.ui.View items don't raise into command_error or the app
  command tree at all — they have their own separate error path
  (View.on_error), which by default also just prints to console. This
  file exports ErrorAwareView, a drop-in replacement for
  discord.ui.View that routes callback errors through the same
  logging + user-facing-message treatment as everything else. Any cog
  with buttons/selects should subclass this instead of
  discord.ui.View (tickets.py and help.py both need this — see below).

Drop-in:
  Just put this file in cogs/ like any other — main.py's
  load_extensions() picks it up automatically. No config changes
  required, though it uses config.EMBED_COLOR_DARK / config.BOT_NAME /
  config.OWNER_IDS if present and degrades gracefully if they aren't.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

import config

log = logging.getLogger("rosarium.errors")

# Falls back to a plain red if the bot's dark brand color isn't defined.
ERROR_COLOR = getattr(config, "EMBED_COLOR_DARK", discord.Color.red())
FOOTER_TEXT = getattr(config, "FOOTER_TEXT", None)


def _error_embed(description: str) -> discord.Embed:
    embed = discord.Embed(description=description, color=ERROR_COLOR)
    if FOOTER_TEXT:
        embed.set_footer(text=FOOTER_TEXT)
    return embed


async def _respond_with_error(interaction: discord.Interaction, message: str) -> None:
    embed = _error_embed(message)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        log.warning("Could not deliver error response to %s.", interaction.user)


class ErrorAwareView(discord.ui.View):
    """Drop-in replacement for discord.ui.View that logs and reports
    errors from button/select callbacks instead of failing silently.

    Usage: change `class MyView(discord.ui.View):` to
    `class MyView(ErrorAwareView):` — everything else stays the same.
    Needed by tickets.py (TicketPanelView, TicketCloseView,
    ConfirmCloseView) and help.py (HelpView), since their button
    callbacks aren't covered by on_command_error or the app command
    tree handler at all.
    """

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        log.exception(
            "Unhandled error in view item %r (invoked by %s)",
            item,
            interaction.user,
            exc_info=error,
        )
        await _respond_with_error(interaction, "Something went wrong with that.")


class Errors(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # Keep whatever error handler the tree already had (if any)
        # so we don't clobber something set up elsewhere.
        self._old_tree_error = bot.tree.on_error
        bot.tree.on_error = self.on_app_command_error

    async def cog_unload(self):
        # Restore the previous handler if this cog is ever unloaded.
        self.bot.tree.on_error = self._old_tree_error

    # ---------------------------------------------------------------
    # Prefix / hybrid (prefix-invoked) commands
    # ---------------------------------------------------------------

    @commands.Cog.listener()
    async def on_command_error(self, ctx: commands.Context, error: commands.CommandError):
        # Unwrap the "actual" error out of CommandInvokeError/HybridCommandError
        error = getattr(error, "original", error)

        # A local handler (@command.error or cog_command_error) already
        # fully handled this one and marked it — don't double-respond.
        # See module docstring for how local handlers set this flag.
        if getattr(error, "handled", False):
            return

        if isinstance(error, commands.CommandNotFound):
            # Deliberately silent: this fires on every mistyped prefix
            # message (e.g. "!!" from someone spamming), which would
            # otherwise spam the channel. Remove this branch (or add a
            # reply) if you'd rather it speak up.
            return

        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.send(embed=_error_embed(
                f"Missing a required argument: `{error.param.name}`.\n"
                f"Try `{ctx.prefix}help {ctx.command}` to see how it's used."
            ))
            return

        if isinstance(error, commands.TooManyArguments):
            await ctx.send(embed=_error_embed(
                f"Too many arguments for that command.\n"
                f"Try `{ctx.prefix}help {ctx.command}` to see how it's used."
            ))
            return

        if isinstance(error, commands.BadArgument):
            # Covers MemberNotFound, RoleNotFound, ChannelNotFound,
            # BadColorArgument, and similar conversion failures.
            await ctx.send(embed=_error_embed(
                f"That didn't look right: {error}\n"
                f"Try `{ctx.prefix}help {ctx.command}` to see the expected format."
            ))
            return

        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(embed=_error_embed(
                f"That command is on cooldown — try again in "
                f"**{error.retry_after:.1f}s**."
            ))
            return

        if isinstance(error, commands.NotOwner):
            await ctx.send(embed=_error_embed("This command is owner-only."))
            return

        if isinstance(error, commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            await ctx.send(embed=_error_embed(f"You need the **{perms}** permission for that."))
            return

        if isinstance(error, commands.BotMissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            await ctx.send(embed=_error_embed(f"I need the **{perms}** permission to do that."))
            return

        if isinstance(error, commands.NoPrivateMessage):
            await ctx.send(embed=_error_embed("That command only works in a server."))
            return

        if isinstance(error, commands.CheckFailure):
            # Catches the guild_only global check in main.py and any
            # other bare check that doesn't have a more specific
            # exception type above.
            await ctx.send(embed=_error_embed("You can't use that here."))
            return

        # Unhandled/unexpected — log the full traceback and tell the
        # user something broke, without leaking internals.
        log.exception(
            "Unhandled command error in '%s' (invoked by %s)",
            ctx.command,
            ctx.author,
            exc_info=error,
        )
        await ctx.send(embed=_error_embed("Something went wrong running that command."))

    # ---------------------------------------------------------------
    # Slash / hybrid (slash-invoked) commands
    # ---------------------------------------------------------------

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        error = getattr(error, "original", error)

        if getattr(error, "handled", False):
            return

        command = interaction.command

        if isinstance(error, app_commands.CommandOnCooldown):
            message = f"That command is on cooldown — try again in **{error.retry_after:.1f}s**."
        elif isinstance(error, app_commands.MissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            message = f"You need the **{perms}** permission for that."
        elif isinstance(error, app_commands.BotMissingPermissions):
            perms = ", ".join(p.replace("_", " ") for p in error.missing_permissions)
            message = f"I need the **{perms}** permission to do that."
        elif isinstance(error, app_commands.NoPrivateMessage):
            message = "That command only works in a server."
        elif isinstance(error, app_commands.TransformerError):
            message = f"That didn't look right: {error}"
        elif isinstance(error, app_commands.CheckFailure):
            message = "You can't use that here."
        else:
            log.exception(
                "Unhandled app command error in '%s' (invoked by %s)",
                getattr(command, "qualified_name", "unknown"),
                interaction.user,
                exc_info=error,
            )
            message = "Something went wrong running that command."

        await _respond_with_error(interaction, message)


async def setup(bot: commands.Bot):
    await bot.add_cog(Errors(bot))