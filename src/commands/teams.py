import random

from telegram import Update
from telegram.ext import ContextTypes

from src.commands.utils import require_group, require_admin


def _format_team(display_name: str, members: list[tuple[int, str]]) -> str:
    lines = [f"Team {display_name} ({len(members)}):"]
    for _, name in members:
        lines.append(f"• {name}")
    return "\n".join(lines)


@require_group
async def split_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Split known players into two teams and store the assignment."""
    if not await require_admin(update, context):
        return

    quiz = context.chat_data.setdefault("quiz", {})
    quiz.pop("teams", None)
    players = context.chat_data.get("players", {})

    if len(players) < 2:
        await update.message.reply_text(
            "Need at least 2 known players to form teams. Have everyone send a message first."
        )
        return

    name_a = context.bot_data.get("TEAM_NAME_A", "A")
    name_b = context.bot_data.get("TEAM_NAME_B", "B")

    pairs = list(players.items())
    random.shuffle(pairs)
    mid = len(pairs) // 2
    team_a = pairs[:mid]
    team_b = pairs[mid:]
    quiz["teams"] = {"A": team_a, "B": team_b}

    board = [
        "Teams reshuffled!",
        _format_team(name_a, team_a),
        _format_team(name_b, team_b),
    ]
    await update.message.reply_text("\n\n".join(board))


@require_group
async def show_teams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return

    quiz = context.chat_data.get("quiz", {})
    teams = quiz.get("teams")
    if not teams:
        await update.message.reply_text(
            "No teams yet. Use /group to split the current players."
        )
        return

    name_a = context.bot_data.get("TEAM_NAME_A", "A")
    name_b = context.bot_data.get("TEAM_NAME_B", "B")

    board = [
        "Current teams:",
        _format_team(name_a, teams.get("A", [])),
        _format_team(name_b, teams.get("B", [])),
    ]
    await update.message.reply_text("\n\n".join(board))
