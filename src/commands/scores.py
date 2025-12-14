from telegram import Update
from telegram.ext import ContextTypes

from src.commands.utils import require_group, require_admin


@require_group
async def show_scores(update: Update, context: ContextTypes.DEFAULT_TYPE, force: bool = False) -> None:
    """Display the current leaderboard with team and individual scores.

    Args:
        force: If True, bypass the admin check
    """
    if not force and not await require_admin(update, context):
        return

    quiz = context.chat_data.get("quiz", {})
    scores: dict = quiz.get("scores", {})
    if not scores:
        await update.message.reply_text(
            "No scores yet. Answer a question to get on the board."
        )
        return

    teams = quiz.get("teams", {})

    name_map = {
        "A": context.bot_data.get("TEAM_NAME_A", "A"),
        "B": context.bot_data.get("TEAM_NAME_B", "B"),
    }

    def _team_for(user_id: int | None) -> str:
        if not user_id:
            return "?"
        for label, members in teams.items():
            if any(uid == user_id for uid, _ in members):
                return label
        return "?"

    # Calculate team scores
    team_scores: dict[str, int] = {}
    for user_id, pts in scores.items():
        label = _team_for(user_id)
        team_scores[label] = team_scores.get(label, 0) + pts

    team_lines = ["👥 Team Scores"]
    for label, pts in sorted(team_scores.items(), key=lambda item: item[1], reverse=True):
        team_lines.append(f"Team {name_map.get(label, label)}: {pts} pts")

    # Calculate individual scores
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    entries = []
    for idx, (user_id, points) in enumerate(
        sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ):
        user_name = context.bot_data.get(user_id, "Player")
        badge = medals.get(idx, f"#{idx + 1}")
        team_label = _team_for(user_id)
        display = name_map.get(team_label, team_label)
        entries.append(f"{badge}  {user_name} [Team {display}] — {points} pts")

    board = ["🏆 Leaderboard 🏆", "\n".join(team_lines), "\n".join(entries)]
    await update.message.reply_text("\n\n".join(board))
