import asyncio
import contextlib
import logging
import random
import re
from functools import partial
from io import BytesIO
from typing import List, Optional, Sequence, Union

import discord
from discord.errors import HTTPException
from discord.ext.commands import Context

import bot
from bot.constants import Emojis, MODERATION_ROLES, NEGATIVE_REPLIES
from bot.utils import scheduling

log = logging.getLogger(__name__)


def reaction_check(
    reaction: discord.Reaction,
    user: discord.abc.User,
    *,
    message_id: int,
    allowed_emoji: Sequence[str],
    allowed_users: Sequence[int],
    allow_mods: bool = True,
) -> bool:
    """
    Check if a reaction's emoji and author are allowed and the message is `message_id`.

    If the user is not allowed, remove the reaction. Ignore reactions made by the bot.
    If `allow_mods` is True, allow users with moderator roles even if they're not in `allowed_users`.
    """
    right_reaction = (
        user != bot.instance.user
        and reaction.message.id == message_id
        and str(reaction.emoji) in allowed_emoji
    )
    if not right_reaction:
        return False

    is_moderator = (
        allow_mods
        and any(role.id in MODERATION_ROLES for role in getattr(user, "roles", []))
    )

    if user.id in allowed_users or is_moderator:
        log.trace(f"Allowed reaction {reaction} by {user} on {reaction.message.id}.")
        return True
    else:
        log.trace(f"Removing reaction {reaction} by {user} on {reaction.message.id}: disallowed user.")
        scheduling.create_task(
            reaction.message.remove_reaction(reaction.emoji, user),
            HTTPException,  # Suppress the HTTPException if adding the reaction fails
            name=f"remove_reaction-{reaction}-{reaction.message.id}-{user}"
        )
        return False


async def wait_for_deletion(
    message: discord.Message,
    user_ids: Sequence[int],
    deletion_emojis: Sequence[str] = (Emojis.trashcan,),
    timeout: float = 60 * 5,
    attach_emojis: bool = True,
    allow_mods: bool = True
) -> None:
    """
    Wait for up to `timeout` seconds for a reaction by any of the specified `user_ids` to delete the message.

    An `attach_emojis` bool may be specified to determine whether to attach the given
    `deletion_emojis` to the message in the given `context`.
    An `allow_mods` bool may also be specified to allow anyone with a role in `MODERATION_ROLES` to delete
    the message.
    """
    if message.guild is None:
        raise ValueError("Message must be sent on a guild")

    if attach_emojis:
        for emoji in deletion_emojis:
            try:
                await message.add_reaction(emoji)
            except discord.NotFound:
                log.trace(f"Aborting wait_for_deletion: message {message.id} deleted prematurely.")
                return

    check = partial(
        reaction_check,
        message_id=message.id,
        allowed_emoji=deletion_emojis,
        allowed_users=user_ids,
        allow_mods=allow_mods,
    )

    with contextlib.suppress(asyncio.TimeoutError):
        await bot.instance.wait_for('reaction_add', check=check, timeout=timeout)
        await message.delete()


async def send_attachments(
    message: discord.Message,
    destination: Union[discord.TextChannel, discord.Webhook],
    link_large: bool = True,
    use_cached: bool = False,
    **kwargs
) -> List[str]:
    """
    Re-upload the message's attachments to the destination and return a list of their new URLs.

    Each attachment is sent as a separate message to more easily comply with the request/file size
    limit. If link_large is True, attachments which are too large are instead grouped into a single
    embed which links to them. Extra kwargs will be passed to send() when sending the attachment.
    """
    webhook_send_kwargs = {
        'username': message.author.display_name,
        'avatar_url': message.author.avatar_url,
    }
    webhook_send_kwargs.update(kwargs)
    webhook_send_kwargs['username'] = sub_clyde(webhook_send_kwargs['username'])

    large = []
    urls = []
    for attachment in message.attachments:
        failure_msg = (
            f"Failed to re-upload attachment {attachment.filename} from message {message.id}"
        )

        try:
            # Allow 512 bytes of leeway for the rest of the request.
            # This should avoid most files that are too large,
            # but some may get through hence the try-catch.
            if attachment.size <= destination.guild.filesize_limit - 512:
                with BytesIO() as file:
                    await attachment.save(file, use_cached=use_cached)
                    attachment_file = discord.File(file, filename=attachment.filename)

                    if isinstance(destination, discord.TextChannel):
                        msg = await destination.send(file=attachment_file, **kwargs)
                        urls.append(msg.attachments[0].url)
                    else:
                        await destination.send(file=attachment_file, **webhook_send_kwargs)
            elif link_large:
                large.append(attachment)
            else:
                log.info(f"{failure_msg} because it's too large.")
        except HTTPException as e:
            if link_large and e.status == 413:
                large.append(attachment)
            else:
                log.warning(f"{failure_msg} with status {e.status}.", exc_info=e)

    if link_large and large:
        desc = "\n".join(f"[{attachment.filename}]({attachment.url})" for attachment in large)
        embed = discord.Embed(description=desc)
        embed.set_footer(text="Attachments exceed upload size limit.")

        if isinstance(destination, discord.TextChannel):
            await destination.send(embed=embed, **kwargs)
        else:
            await destination.send(embed=embed, **webhook_send_kwargs)

    return urls


def sub_clyde(username: Optional[str]) -> Optional[str]:
    """
    Replace "e"/"E" in any "clyde" in `username` with a Cyrillic "е"/"E" and return the new string.

    Discord disallows "clyde" anywhere in the username for webhooks. It will return a 400.
    Return None only if `username` is None.
    """
    def replace_e(match: re.Match) -> str:
        char = "е" if match[2] == "e" else "Е"
        return match[1] + char

    if username:
        return re.sub(r"(clyd)(e)", replace_e, username, flags=re.I)
    else:
        return username  # Empty string or None


async def send_denial(ctx: Context, reason: str) -> discord.Message:
    """Send an embed denying the user with the given reason."""
    embed = discord.Embed()
    embed.colour = discord.Colour.red()
    embed.title = random.choice(NEGATIVE_REPLIES)
    embed.description = reason

    return await ctx.send(embed=embed)


def format_user(user: discord.abc.User) -> str:
    """Return a string for `user` which has their mention and ID."""
    return f"{user.mention} (`{user.id}`)"
