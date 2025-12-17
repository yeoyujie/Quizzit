import random
import logging

from telegram import Update
from telegram.ext import ContextTypes

from src.commands.utils import require_group, require_admin

logger = logging.getLogger(__name__)

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
    logger.info(f"Known players: {players}")

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


@require_group
async def add_points(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return

    message = update.message
    if not message or not message.text:
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        await message.reply_text("Usage: /add <team> <points>  e.g. /add a 10")
        return

    team_token = parts[1].strip()
    pts_token = parts[2].strip()

    try:
        points = int(pts_token)
    except ValueError:
        await message.reply_text(f"Invalid points value: {pts_token}")
        return

    quiz = context.chat_data.get("quiz", {})
    teams = quiz.get("teams") if quiz else None
    if not teams:
        await message.reply_text(
            "No teams yet. Use /group to split the current players."
        )
        return

    label = None
    token_up = team_token.upper()
    if token_up in teams:
        label = token_up
    else:
        name_a = context.bot_data.get("TEAM_NAME_A", "A").lower()
        name_b = context.bot_data.get("TEAM_NAME_B", "B").lower()
        if team_token.lower() == name_a:
            label = "A"
        elif team_token.lower() == name_b:
            label = "B"

    if not label:
        await message.reply_text(f"Unknown team: {team_token}")
        return

    members = teams.get(label, [])
    if not members:
        await message.reply_text(f"Team {label} has no members to score.")
        return

    team_scores = quiz.setdefault("team_scores", {})
    team_scores[label] = team_scores.get(label, 0) + points

    display_name = context.bot_data.get(f"TEAM_NAME_{label}", label)
    sign = "+" if points >= 0 else ""
    await message.reply_text(
        f"{display_name} {sign}{points} pts added to team {label} (team total: {team_scores[label]} pts)."
    )
