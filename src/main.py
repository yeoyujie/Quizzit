import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters

from src.config import load_config
from src.commands import (
    start,
    hint,
    handle_answer,
    show_scores,
    split_groups,
    show_teams,
    add_points,
    mute,
    givemute,
    removemute
)
from src.commands.utils import seen_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


def main() -> None:
    """Initialize and run the bot."""
    cfg = load_config()

    app = Application.builder().token(cfg["TELEGRAM_BOT_TOKEN"]).build()

    # Store config in bot_data for access in handlers
    app.bot_data["QUIZ_DELAY_SECONDS"] = cfg.get("QUIZ_DELAY_SECONDS", 0)
    app.bot_data["ADMIN_USER_ID"] = cfg.get("ADMIN_USER_ID")
    app.bot_data["TEAM_NAME_A"] = cfg.get("TEAM_NAME_A", "A")
    app.bot_data["TEAM_NAME_B"] = cfg.get("TEAM_NAME_B", "B")

    # Register command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("add", add_points))
    app.add_handler(CommandHandler("scores", show_scores))
    app.add_handler(CommandHandler("hint", hint))
    app.add_handler(CommandHandler("group", split_groups))
    app.add_handler(CommandHandler("team", show_teams))
    app.add_handler(CommandHandler("mute", mute))
    app.add_handler(CommandHandler("givemute", givemute))
    app.add_handler(CommandHandler("removemute", removemute))

    # Register message handler for answers
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND,
            handle_answer
        )
    )

    # Then register a catch-all group message handler to record seen users
    # (exclude commands so it doesn't run for /start, /group, etc.)
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND,
            seen_message
        )
    )

    logger.info("Bot starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
