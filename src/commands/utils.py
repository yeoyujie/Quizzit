"""Shared utilities for command handlers."""

import asyncio
import logging
from functools import wraps
from typing import Callable, Coroutine, Any

from telegram import Update
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


def require_group(func: Callable[..., Coroutine[Any, Any, None]]) -> Callable[..., Coroutine[Any, Any, None]]:
    """Decorator to ensure command is only used in group chats."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        chat = update.effective_chat
        if chat.type not in {"group", "supergroup"}:
            await update.effective_message.reply_text(
                "This command can only be used in a group chat."
            )
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


async def require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if the user is the configured admin. Returns True if allowed."""
    admin_id = context.bot_data.get("ADMIN_USER_ID")
    user = update.effective_user

    if admin_id is None:
        return True

    if not user or user.id != admin_id:
        logger.info(
            f"Non-admin attempted restricted command in chat {update.effective_chat.id}"
        )

        if user:
            try:
                await context.bot.send_message(
                    chat_id=user.id,
                    text=f"You do not have permission to run this command in *{update.effective_chat.title}*.",
                    parse_mode="Markdown"
                )
                logger.info(f"Sent permission denial DM to user {user.id}")
            except Exception as e:
                logger.info(f"Failed to DM user {user.id}: {e}")
        return False
    return True


def record_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep track of seen users in this chat for team assignment."""
    user = update.effective_user
    if not user:
        return
    players = context.chat_data.setdefault("players", {})
    players[user.id] = user.full_name or "Player"


async def seen_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Async wrapper to record any seen user message in group chats.

    Register this as a catch-all MessageHandler for group chats so the
    bot builds a `chat_data['players']` mapping even when no quiz is
    running.
    """
    try:
        record_user(update, context)
        logger.debug(
            f"Recorded seen user for chat {update.effective_chat.id if update.effective_chat else 'unknown'}")
    except Exception:
        logger.exception("Failed to record seen user")


async def countdown_timer(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    seconds: int,
    start_text: str = "🚀 Next question in",
    end_text: str = "🎯 GO!",
    update_interval: int = 1,
    fancy_animation: bool = False
) -> None:
    if seconds <= 0:
        return

    if not fancy_animation:
        await asyncio.sleep(5)
        return

    try:
        countdown_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"{start_text} {seconds}..."
        )

        for remaining in range(seconds - 1, 0, -1):
            await asyncio.sleep(update_interval)
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=countdown_msg.message_id,
                text=f"{start_text} {remaining}..."
            )

        await asyncio.sleep(update_interval)
        await context.bot.edit_message_text(
            chat_id=chat_id,
            message_id=countdown_msg.message_id,
            text=f"*{end_text}*",
            parse_mode="Markdown"
        )

    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.error(f"Countdown failed: {e}")
