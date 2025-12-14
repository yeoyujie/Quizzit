import asyncio
import json
import logging
import math
import random
from pathlib import Path
from time import monotonic

from config import load_config
from typing import Optional
from telegram.error import BadRequest
from telegram import Update, Message, ReactionTypeEmoji
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

QUESTIONS_FILE = Path(__file__).resolve().parent.parent / "questions.json"
HINT_POINT_STEPS = [
    {"time": 6, "ratio": 0.0, "points": 5},
    {"time": 12, "ratio": 0.2, "points": 4},
    {"time": 18, "ratio": 0.4, "points": 3},
    {"time": 24, "ratio": 0.6, "points": 2},
]
FINAL_REVEAL_TIME = 30
MIN_POINTS = 1


def _cancel_hint_tasks(chat_data: dict) -> None:
    tasks: list[asyncio.Task] = chat_data.pop("hint_tasks", [])
    for task in tasks:
        if not task.done():
            task.cancel()
    chat_data.pop("hint_state", None)


def _schedule_hints(update: Update, answer: str, chat_id: int, context: ContextTypes.DEFAULT_TYPE, chat_data: dict) -> None:
    """Schedule timed hint reveals while the question is active."""
    _cancel_hint_tasks(chat_data)
    qid = chat_data.get("current_qid", 0)
    indices = [i for i, ch in enumerate(answer) if not ch.isspace()]
    chat_data["hint_state"] = {
        "qid": qid,
        "revealed": set(),
        "indices": indices,
        "answer": answer,
    }
    chat_data["current_points"] = HINT_POINT_STEPS[0]["points"] if HINT_POINT_STEPS else MIN_POINTS

    async def _reveal(delay: int, ratio: float) -> None:
        try:
            await asyncio.sleep(delay)
            state = chat_data.get("hint_state")
            if not state or chat_data.get("answered") or state.get("qid") != qid:
                return

            indices_local: list[int] = state["indices"]
            revealed: set[int] = state["revealed"]
            target = math.ceil(len(indices_local) * ratio)
            if target > len(revealed):
                remaining = [i for i in indices_local if i not in revealed]
                add_count = min(len(remaining), target - len(revealed))
                if add_count > 0:
                    revealed.update(random.sample(remaining, k=add_count))

            hint = _build_progressive_hint(answer, revealed)
            await context.bot.send_message(chat_id=chat_id, text=f"Hint: {hint}")
            # Drop points in lockstep with this hint
            chat_data["current_points"] = next(
                (step["points"]
                 for step in HINT_POINT_STEPS if step["time"] == delay),
                chat_data.get("current_points", MIN_POINTS),
            )
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"Failed to send hint: {e}")

    async def _auto_reveal(delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            state = chat_data.get("hint_state")
            if not state or chat_data.get("answered") or state.get("qid") != qid:
                return
            chat_data["answered"] = True
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"No one guessed! The correct answer is {answer}",
            )
            wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
            if wait_seconds > 0:
                try:
                    msg = await context.bot.send_message(chat_id=chat_id, text="Next question loading...")
                    await run_countdown(message=msg, seconds=wait_seconds)
                except Exception:
                    pass
            chat_data["index"] = chat_data.get("index", 0) + 1
            await _send_question(update, context)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"Auto reveal failed: {e}")

    chat_data["hint_tasks"] = [
        *[
            asyncio.create_task(_reveal(step["time"], step["ratio"]))
            for step in HINT_POINT_STEPS
        ],
        asyncio.create_task(_auto_reveal(FINAL_REVEAL_TIME)),
    ]


def _normalize(text: str) -> str:
    """Lowercase and trim for lenient answer matching."""
    return text.strip().lower()


def _points_for_elapsed(seconds: float) -> int:
    """Return points based on elapsed time thresholds."""
    for step in HINT_POINT_STEPS:
        if seconds <= step["time"]:
            return step["points"]
    return MIN_POINTS


def _build_progressive_hint(answer: str, revealed: set[int]) -> str:
    """Reveal letters at given indices and space separate hidden characters."""
    masked = []
    for idx, ch in enumerate(answer):
        if ch.isspace():
            masked.append("  ")
        elif idx in revealed:
            masked.append(ch)
        else:
            masked.append("_ ")
    return "".join(masked).strip()


def _record_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Keep track of seen users in this chat for team assignment."""
    user = update.effective_user
    if not user:
        return
    players = context.chat_data.setdefault("players", {})
    players[user.id] = user.full_name or "Player"


async def _require_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    admin_id = context.bot_data.get("ADMIN_USER_ID")
    user = update.effective_user
    if admin_id is None:
        return True
    if not user or user.id != admin_id:
        logger.info(
            f"Non-admin attempted restricted command in chat {update.effective_chat.id}")
        if update.message:
            try:
                await update.message.reply_text("Only the admin can run this command.")
            except Exception:
                pass
        return False
    return True


async def _send_hint_dm(user_id: int, idx: int, question: dict, context: ContextTypes.DEFAULT_TYPE) -> None:
    answer = question.get("answer", "")
    if not answer:
        return
    first_char = answer[0]
    length = len(answer)
    text = f"Hint for Question {idx + 1}: starts with '{first_char}' and has {length} characters."
    await context.bot.send_message(chat_id=user_id, text=text)


def _load_questions() -> list[dict]:
    with QUESTIONS_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


async def _send_question(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_data = context.chat_data
    _cancel_hint_tasks(chat_data)
    questions: list[dict] = chat_data["questions"]
    idx = chat_data.get("index", 0) % len(questions)
    chat_data["index"] = idx
    chat_data["answered"] = False
    chat_data["current_qid"] = chat_data.get("current_qid", 0) + 1

    qdata = questions[idx]
    question = qdata.get("question", "")
    q_type = qdata.get("type", "text")
    logger.info(
        f"Asking question {idx + 1} to chat {update.effective_chat.id}: {question}")
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=f"📢 *QUESTION {idx + 1}* 📢",
        parse_mode="Markdown"
    )

    file_path = qdata.get("file")
    if file_path and q_type in {"image", "audio", "video"}:
        asset_path = Path(file_path)
        try:
            with asset_path.open("rb") as fh:
                if q_type == "image":
                    await context.bot.send_photo(chat_id=update.effective_chat.id, photo=fh)
                elif q_type == "audio":
                    await context.bot.send_audio(chat_id=update.effective_chat.id, audio=fh)
                elif q_type == "video":
                    await context.bot.send_video(chat_id=update.effective_chat.id, video=fh)
        except Exception as e:
            logger.warning(f"Failed to send asset {asset_path}: {e}")

    await context.bot.send_message(chat_id=update.effective_chat.id, text=f"{question}")

    chat_data["question_start_ts"] = monotonic()
    _schedule_hints(update, qdata.get("answer", ""),
                    update.effective_chat.id, context, chat_data)


async def run_countdown(
    message: Message,
    seconds: int,
    start_text: str = "🚀 Next question in",
    end_text: str = "🎯 GO!",
    update_interval: int = 1
) -> Optional[Message]:
    if seconds <= 0:
        return await message.reply_text(f"*{end_text}*", parse_mode="Markdown")

    countdown_msg = None

    try:
        countdown_msg = await message.reply_text(f"{start_text} {seconds}...")

        for remaining in range(seconds - 1, 0, -1):
            await asyncio.sleep(update_interval)

            try:
                await countdown_msg.edit_text(f"{start_text} {remaining}...")
            except BadRequest:
                return None
            except Exception as e:
                logger.warning(f"Failed to edit message: {e}")
                continue

        await asyncio.sleep(update_interval)
        await countdown_msg.edit_text(f"*{end_text}*", parse_mode="Markdown")

        return countdown_msg

    except asyncio.CancelledError:
        if countdown_msg:
            try:
                await countdown_msg.delete()
            except Exception:
                pass
        raise
    except Exception as e:
        logger.error(f"Countdown failed: {e}")
        return None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    message = update.effective_message

    if chat.type not in {"group", "supergroup"}:
        await message.reply_text("/start can only be used in a group chat.")
        return

    if not await _require_admin(update, context):
        return

    questions = _load_questions()
    logger.info(f"Loaded {len(questions)} questions from {QUESTIONS_FILE}")

    context.chat_data["questions"] = questions
    context.chat_data["index"] = 0
    context.chat_data["scores"] = {}
    context.chat_data["answered"] = False

    start_message = (
        "🎊 *QUIZ TIME!* 🎊\n\n"
        "Get ready to test your knowledge! 🧠\n"
        "💡 *Type your answers quickly!*"
    )
    await message.reply_text(start_message, parse_mode="Markdown")
    wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
    await run_countdown(
        message=update.effective_message,
        seconds=wait_seconds,
    )
    await _send_question(update, context)


async def show_scores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    scores: dict = context.chat_data.get("scores", {})
    if not scores:
        await update.message.reply_text("No scores yet. Answer a question to get on the board.")
        return

    teams = context.chat_data.get("teams", {})

    def _team_for(user_id: int | None) -> str:
        if not user_id:
            return "?"
        for label, members in teams.items():
            if any(uid == user_id for uid, _ in members):
                return label
        return "?"

    # Team scores
    team_scores: dict[str, int] = {}
    for user_id, pts in scores.items():
        label = _team_for(user_id)
        team_scores[label] = team_scores.get(label, 0) + pts

    team_lines = ["👥 Team Scores"]
    for label, pts in sorted(team_scores.items(), key=lambda item: item[1], reverse=True):
        team_lines.append(f"Team {label}: {pts} pts")

    # Individual scores
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    entries = []
    for idx, (user_id, points) in enumerate(
        sorted(scores.items(), key=lambda item: item[1], reverse=True)
    ):
        user_name = context.bot_data.get(user_id, "Player")
        badge = medals.get(idx, f"#{idx + 1}")
        team_label = _team_for(user_id)
        entries.append(
            f"{badge}  {user_name} [Team {team_label}] — {points} pts")

    board = ["🏆 Leaderboard 🏆", "\n".join(team_lines), "\n".join(entries)]
    await update.message.reply_text("\n\n".join(board))


async def send_hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM the user a hint for the current question."""
    user = update.effective_user
    if not user:
        return
    chat_data = context.chat_data
    questions: list[dict] = chat_data.get("questions")
    if not questions:
        await update.message.reply_text("Quiz not started here. Send /start to begin.")
        return
    idx = chat_data.get("index", 0) % len(questions)
    try:
        await _send_hint_dm(user.id, idx, questions[idx], context)
    except Exception:
        expect_text = f"Could not DM {user.full_name}. Please make sure you've started a chat with me."
        await update.message.reply_text(expect_text)


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    _record_user(update, context)

    chat_data = context.chat_data
    questions: list[dict] = chat_data.get("questions")
    if not questions:
        logger.info(
            f"Received answer but no quiz state for chat {update.effective_chat.id}")
        await message.reply_text("Quiz not started here. Send /start to begin.")
        return

    idx = chat_data.get("index", 0) % len(questions)
    if chat_data.get("answered"):
        logger.info(
            f"Ignoring answer. Question already answered in chat {update.effective_chat.id}"
        )
        return

    current = questions[idx]
    answer = current.get("answer", "")
    submitted = _normalize(message.text)
    expected = _normalize(answer)
    alternatives = [_normalize(item)
                    for item in current.get("alternative", [])]
    expected_values = {expected, *alternatives}

    user_info = f"user {update.effective_user.id}" if update.effective_user else "anonymous user"
    logger.info(
        f"Answer attempt by {user_info} in chat {update.effective_chat.id}: submitted='{submitted}' expected='{expected}'"
    )

    if submitted not in expected_values:
        logger.info(
            f"Incorrect answer by {user_info} in chat {update.effective_chat.id}")

    user = update.effective_user
    user_id = user.id if user else None
    name = user.full_name if user else "Player"
    _cancel_hint_tasks(chat_data)
    start_ts = chat_data.get("question_start_ts")
    now_ts = monotonic()
    elapsed = max(0.0, now_ts - start_ts) if start_ts else 0.0
    points = chat_data.get("current_points")

    if points is None:
        points = _points_for_elapsed(elapsed)

    context.bot_data[user_id] = name
    scores = chat_data.setdefault("scores", {})
    scores[user_id] = scores.get(user_id, 0) + points
    chat_data["answered"] = True
    logger.info(
        f"Correct answer by user {user_id} in chat {update.effective_chat.id}; score now {scores[user_id]}"
    )

    wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
    await message.reply_text(
        f"*{answer}* is correct!\n\n{name} *+{points}* (answered in {elapsed:.1f}s)",
        parse_mode="Markdown"
    )
    await run_countdown(
        message=update.effective_message,
        seconds=wait_seconds,
    )

    next_idx = idx + 1
    chat_data["index"] = next_idx
    await _send_question(update, context)


async def split_groups(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Split known players into two teams and store the assignment."""
    if not await _require_admin(update, context):
        return
    # Always reset previous teams and reshuffle
    context.chat_data.pop("teams", None)
    players = context.chat_data.get("players", {})
    if len(players) < 2:
        await update.message.reply_text("Need at least 2 known players to form teams. Have everyone send a message first.")
        return

    # TODO: Change teamnames to be configurable
    pairs = list(players.items())
    random.shuffle(pairs)
    mid = len(pairs) // 2
    team_a = pairs[:mid]
    team_b = pairs[mid:]
    context.chat_data["teams"] = {"A": team_a, "B": team_b}

    def _fmt(team_label: str, members: list[tuple[int, str]]) -> str:
        lines = [f"Team {team_label} ({len(members)}):"]
        for _, name in members:
            lines.append(f"• {name}")
        return "\n".join(lines)

    board = ["Teams reshuffled!", _fmt("A", team_a), _fmt("B", team_b)]
    await update.message.reply_text("\n\n".join(board))


async def show_teams(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _require_admin(update, context):
        return
    teams = context.chat_data.get("teams")
    if not teams:
        await update.message.reply_text("No teams yet. Use /group to split the current players.")
        return

    def _fmt(team_label: str, members: list[tuple[int, str]]) -> str:
        lines = [f"Team {team_label} ({len(members)}):"]
        for _, name in members:
            lines.append(f"• {name}")
        return "\n".join(lines)

    board = ["Current teams:", _fmt("A", teams.get(
        "A", [])), _fmt("B", teams.get("B", []))]
    await update.message.reply_text("\n\n".join(board))


def main() -> None:
    cfg = load_config()
    app = Application.builder().token(cfg["TELEGRAM_BOT_TOKEN"]).build()
    app.bot_data["QUIZ_DELAY_SECONDS"] = cfg.get("QUIZ_DELAY_SECONDS", 0)
    app.bot_data["ADMIN_USER_ID"] = cfg.get("ADMIN_USER_ID")

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("scores", show_scores))
    app.add_handler(CommandHandler("hint", send_hint))
    app.add_handler(CommandHandler("group", split_groups))
    app.add_handler(CommandHandler("team", show_teams))
    app.add_handler(MessageHandler(filters.ChatType.GROUPS &
                    filters.TEXT & ~filters.COMMAND, handle_answer))

    logger.info("Bot starting polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
