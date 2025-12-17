import random
import logging
import asyncio
import re
from time import monotonic

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

    quiz.setdefault("mute_enabled", {"A": False, "B": False})
    quiz.setdefault("mute_uses", {})
    quiz.pop("muted_team", None)
    quiz.pop("muted_until", None)

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

    logger.info(f"Adding points: team={team_token}, points={pts_token}")

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


@require_group
async def givemute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return

    message = update.message
    if not message or not message.text:
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        await message.reply_text("Usage: /givemute <team> <count>  e.g. /givemute a 3")
        return

    token = parts[1].strip()
    raw = parts[2].strip()

    try:
        count = int(raw)
    except ValueError:
        await message.reply_text(f"Invalid count value: {raw}. Provide a positive integer.")
        return

    if count <= 0:
        await message.reply_text(f"Count must be positive: {raw}.")
        return

    quiz = context.chat_data.setdefault("quiz", {})
    teams = quiz.get("teams")
    if not teams:
        await message.reply_text("No teams yet. Use /group to split the current players.")
        return

    # Resolve token to internal label 'A' or 'B'
    token_norm = token.lower()
    label = None
    if token_norm in ("a", "b"):
        label = token_norm.upper()
    else:
        name_a = context.bot_data.get("TEAM_NAME_A", "A").lower()
        name_b = context.bot_data.get("TEAM_NAME_B", "B").lower()
        if token_norm == name_a:
            label = "A"
        elif token_norm == name_b:
            label = "B"

    if not label:
        await message.reply_text(f"Unknown team: {token}")
        return

    # Ensure label is normalized and store state under 'A' or 'B'
    label = label.upper()
    mute_enabled = quiz.setdefault("mute_enabled", {})
    mute_uses = quiz.setdefault("mute_uses", {})
    mute_enabled[label] = True
    mute_uses[label] = count

    display_name = context.bot_data.get(f"TEAM_NAME_{label}", label)
    logger.info(
        f"/givemute: enabled mute for label={label} (display={display_name})")
    await message.reply_text(
        f"{display_name} can now use /mute ({mute_uses[label]} uses for the team this game)."
    )
    other = "A" if label == "B" else "B"
    mute_enabled.setdefault(other, False)
    mute_uses.setdefault(other, 0)


@require_group
async def removemute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return

    message = update.message
    if not message or not message.text:
        return

    parts = message.text.strip().split()
    if len(parts) < 2:
        await message.reply_text("Usage: /removemute <team>  e.g. /removemute a")
        return

    token = parts[1].strip()

    quiz = context.chat_data.get("quiz", {})
    teams = quiz.get("teams") if quiz else None
    if not teams:
        await message.reply_text("No teams yet. Use /group to split the current players.")
        return
    token_norm = token.strip().lower()
    label = None
    if token_norm in ("a", "b"):
        label = token_norm.upper()
    else:
        name_a = context.bot_data.get("TEAM_NAME_A", "A").lower()
        name_b = context.bot_data.get("TEAM_NAME_B", "B").lower()
        if token_norm == name_a:
            label = "A"
        elif token_norm == name_b:
            label = "B"

    if not label:
        await message.reply_text(f"Unknown team: {token}")
        return

    label = label.upper()
    mute_enabled = quiz.setdefault("mute_enabled", {})
    mute_uses = quiz.setdefault("mute_uses", {})
    mute_enabled[label] = False
    mute_uses.pop(label, None)

    # Cancel any active mute tasks for this quiz and clear muted state if matching
    mute_tasks = quiz.setdefault("mute_tasks", [])
    # Clear muted team if it matches the label being removed
    if quiz.get("muted_team") == label:
        quiz.pop("muted_team", None)
        quiz.pop("muted_until", None)

        for t in list(mute_tasks):
            try:
                if not t.done():
                    t.cancel()
            except Exception:
                pass
        quiz["mute_tasks"] = []

    display_name = context.bot_data.get(f"TEAM_NAME_{label}", label)
    logger.info(
        f"/removemute: disabled mute for label={label} (display={display_name})")
    await message.reply_text(f"{display_name} can no longer use /mute.")


@require_group
async def enabledouble(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Enable double-points for a team for questions with a specific tag."""
    if not await require_admin(update, context):
        return

    message = update.message
    if not message or not message.text:
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        await message.reply_text("Usage: /enabledouble <team> <tag>  e.g. /enabledouble a brand")
        return

    token = parts[1].strip()
    tag = parts[2].strip()

    quiz = context.chat_data.setdefault("quiz", {})
    teams = quiz.get("teams")
    if not teams:
        await message.reply_text("No teams yet. Use /group to split the current players.")
        return

    token_norm = token.lower()
    label = None
    if token_norm in ("a", "b"):
        label = token_norm.upper()
    else:
        name_a = context.bot_data.get("TEAM_NAME_A", "A").lower()
        name_b = context.bot_data.get("TEAM_NAME_B", "B").lower()
        if token_norm == name_a:
            label = "A"
        elif token_norm == name_b:
            label = "B"

    if not label:
        await message.reply_text(f"Unknown team: {token}")
        return

    double_tags = quiz.setdefault("double_tags", {"A": set(), "B": set()})
    # ensure sets exist for labels
    double_tags.setdefault("A", set())
    double_tags.setdefault("B", set())

    double_tags[label].add(tag)

    display_name = context.bot_data.get(f"TEAM_NAME_{label}", label)
    logger.info(f"/enabledouble: enabled double for label={label} tag={tag}")
    await message.reply_text(f"{display_name} will now receive double points for questions tagged '{tag}'.")


@require_group
async def disabledouble(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Disable double-points for a team for a specific tag."""
    if not await require_admin(update, context):
        return

    message = update.message
    if not message or not message.text:
        return

    parts = message.text.strip().split()
    if len(parts) < 3:
        await message.reply_text("Usage: /disabledouble <team> <tag>  e.g. /disabledouble a brand")
        return

    token = parts[1].strip()
    tag = parts[2].strip()

    quiz = context.chat_data.get("quiz", {})
    teams = quiz.get("teams") if quiz else None
    if not teams:
        await message.reply_text("No teams yet. Use /group to split the current players.")
        return

    token_norm = token.lower()
    label = None
    if token_norm in ("a", "b"):
        label = token_norm.upper()
    else:
        name_a = context.bot_data.get("TEAM_NAME_A", "A").lower()
        name_b = context.bot_data.get("TEAM_NAME_B", "B").lower()
        if token_norm == name_a:
            label = "A"
        elif token_norm == name_b:
            label = "B"

    if not label:
        await message.reply_text(f"Unknown team: {token}")
        return

    double_tags = quiz.setdefault("double_tags", {"A": set(), "B": set()})
    double_tags.setdefault("A", set())
    double_tags.setdefault("B", set())

    if tag in double_tags.get(label, set()):
        double_tags[label].discard(tag)
        display_name = context.bot_data.get(f"TEAM_NAME_{label}", label)
        logger.info(
            f"/disabledouble: disabled double for label={label} tag={tag}")
        await message.reply_text(f"{display_name} will no longer receive double points for questions tagged '{tag}'.")
    else:
        await message.reply_text(f"Team {label} did not have double-points enabled for tag '{tag}'.")


@require_group
async def showtags(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_admin(update, context):
        return

    user = update.effective_user
    if not user:
        return

    quiz = context.chat_data.get("quiz", {})
    double_tags = quiz.get("double_tags", {})

    a_tags = sorted(list(double_tags.get("A", set())))
    b_tags = sorted(list(double_tags.get("B", set())))

    msg_lines = ["Current tag assignments:",
                 f"Team A: {', '.join(a_tags) if a_tags else '(none)'}", f"Team B: {', '.join(b_tags) if b_tags else '(none)'}"]

    try:
        await context.bot.send_message(chat_id=user.id, text="\n".join(msg_lines))
        logger.info(f"/showtags: sent double-tag mappings to user {user.id}")
    except Exception:
        logger.info(f"Could not DM user {user.id} the tag mappings.")


@require_group
async def mute(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Team members can call /mute while their team has been granted mute ability.

    This command is limited to 3 uses per team per game. It does not perform an actual
    Telegram mute here; it implements the permission and counting logic and returns
    a confirmation. Integrate actual mute behavior separately if desired.
    """
    message = update.message
    if not message or not message.from_user:
        return

    user = update.effective_user
    user_id = user.id

    quiz = context.chat_data.get("quiz", {})
    teams = quiz.get("teams") if quiz else None
    if not teams:
        logging.info("No teams found for /mute command.")
        return

    user_label = None
    for lab, members in teams.items():
        if any(uid == user_id for uid, _ in members):
            user_label = lab
            break

    if not user_label:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"You are not assigned to any team, so you cannot use /mute.",
            )
        except Exception:
            logger.info(
                f"Could not DM {user} about missing hint.")
        return

    mute_enabled = quiz.setdefault("mute_enabled", {})
    mute_uses = quiz.setdefault("mute_uses", {})
    user_label = str(user_label).upper()
    if not mute_enabled.get(user_label, False):
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=(f"Your team is not allowed to use /mute."),
            )
        except Exception:
            logger.info(
                f"Could not DM {user}.")
        return

    remaining = mute_uses.get(user_label, 0)
    if remaining <= 0:
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"Your team has no remaining /mute uses.",
            )
        except Exception:
            logger.info(
                f"Could not DM {user}.")
        return

    mute_uses[user_label] = remaining - 1
    display_name = context.bot_data.get(f"{user_label}", user_label)
    await message.reply_text(
        f"{display_name} used /mute. Remaining team mutes: {mute_uses[user_label]}"
    )

    other_label = "A" if user_label == "B" else "B"
    quiz["muted_team"] = other_label
    quiz["muted_until"] = monotonic() + 20

    logger.info(
        f"Team {other_label} muted for 20 seconds by team {user_label}.")

    chat = update.effective_chat
    display_other = context.bot_data.get(
        f"TEAM_NAME_{other_label}", other_label)
    await context.bot.send_message(
        chat_id=chat.id,
        text=f"{display_other} are muted for 20 seconds. Their answers will be ignored."
    )

    async def _clear_mute_after(delay: int, chat_id: int, muted_label: str) -> None:
        try:
            await asyncio.sleep(delay)
            # Only clear if still muted and the mute has expired
            if quiz.get("muted_team") == muted_label and monotonic() >= quiz.get("muted_until", 0):
                quiz.pop("muted_team", None)
                quiz.pop("muted_until", None)
                display = context.bot_data.get(
                    f"TEAM_NAME_{muted_label}", muted_label)
                logger.info(
                    f"Clearing mute for team {muted_label} after {delay} seconds.")
                await context.bot.send_message(
                    chat_id=chat_id,
                    text=f"{display} are no longer muted. You may answer now."
                )
                logger.info(
                    f"Cleared mute for team {muted_label} after {delay} seconds.")
        except asyncio.CancelledError:
            logger.info(
                f"Mute clear task for team {muted_label} was cancelled.")
            pass
        except Exception as e:
            logger.warning(f"Failed clearing mute: {e}")

    mute_tasks = quiz.setdefault("mute_tasks", [])
    mute_tasks.append(asyncio.create_task(
        _clear_mute_after(20, chat.id, other_label)))
