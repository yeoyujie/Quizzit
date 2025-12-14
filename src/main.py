import asyncio
import json
import logging
import math
import random
from pathlib import Path
from time import monotonic

from config import load_config
from typing import Optional
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

QUESTIONS_FILE = Path(__file__).resolve().parent.parent / "questions.json"

INITIAL_POINTS = 5
HINT_POINT_STEPS = [
    {"time": 8, "ratio": 0.0, "points": 4},
    {"time": 14, "ratio": 0.2, "points": 3},
    {"time": 20, "ratio": 0.4, "points": 2},
    {"time": 25, "ratio": 0.6, "points": 1},
]
FINAL_REVEAL_TIME = 30


def _cancel_pending_tasks(chat_data: dict) -> None:
    """Cancel all scheduled hint/reveal tasks, avoiding self-cancellation."""
    tasks: list[asyncio.Task] = chat_data.pop("hint_tasks", [])
    current_task = asyncio.current_task()
    for task in tasks:
        if task is current_task:
            continue  # Don't cancel the task that's calling us
        if not task.done():
            task.cancel()


def _reset_question_state(chat_data: dict, next_index: int) -> int:
    """
    Reset all per-question state atomically and return the new generation ID.

    This ensures a clean slate before each question:
    - Cancels pending hint tasks
    - Increments generation (used to detect stale async operations)
    - Resets answered flag, points, timestamps, and hint state

    Returns:
        The new generation ID for this question session.
    """
    _cancel_pending_tasks(chat_data)

    # Increment generation - used to invalidate any stale async callbacks
    generation = chat_data.get("generation", 0) + 1

    chat_data.update({
        "index": next_index,
        "generation": generation,
        "answered": False,
        "accepting_answers": False,  # Only True after question is sent
        "question_start_ts": None,
        "current_points": INITIAL_POINTS,
        "hint_tasks": [],
        "revealed_indices": set(),
    })

    return generation


def _is_stale(chat_data: dict, generation: int) -> bool:
    """Check if this generation is outdated (question has changed)."""
    return chat_data.get("generation") != generation or chat_data.get("answered", False)


def _is_accepting_answers(chat_data: dict) -> bool:
    """Check if answers are currently being accepted."""
    return chat_data.get("accepting_answers", False) and not chat_data.get("answered", False)


def _schedule_hints(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_data: dict,
    chat_id: int,
    answer: str,
    generation: int,
) -> None:
    """
    Schedule timed hint reveals and auto-reveal for the current question.

    Args:
        generation: The question generation ID - used to detect if callbacks are stale.
    """
    if not answer:
        return

    # Prepare randomized reveal order for progressive hints
    revealable_indices = [i for i, ch in enumerate(answer) if not ch.isspace()]
    reveal_order = revealable_indices.copy()
    random.shuffle(reveal_order)
    chat_data["reveal_order"] = reveal_order

    async def _send_hint(delay: int, ratio: float, points: int) -> None:
        """Send a progressive hint after delay, if question is still active."""
        try:
            await asyncio.sleep(delay)
            if _is_stale(chat_data, generation):
                return

            # Calculate which indices to reveal based on ratio
            target_count = math.ceil(len(reveal_order) * ratio)
            revealed = set(reveal_order[:target_count])
            chat_data["revealed_indices"] = revealed
            chat_data["current_points"] = points

            hint_text = _build_progressive_hint(answer, revealed)
            await context.bot.send_message(chat_id=chat_id, text=f"Hint: {hint_text}")
        except asyncio.CancelledError:
            pass  # Task was cancelled, exit gracefully
        except Exception as e:
            logger.warning(f"Failed to send hint: {e}")

    async def _timeout_reveal(delay: int) -> None:
        """Reveal answer after timeout and advance to next question."""
        try:
            await asyncio.sleep(delay)
            if _is_stale(chat_data, generation):
                return

            # Mark as answered to prevent race conditions
            chat_data["answered"] = True

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"*❌ No one guessed!*\n\nThe correct answer was: *{answer}*",
                parse_mode="Markdown"
            )

            # Countdown before next question
            wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
            if wait_seconds > 0:
                try:
                    await countdown_timer(context=context, chat_id=chat_id, seconds=wait_seconds)
                except Exception:
                    logger.warning(
                        "Countdown after timeout failed; continuing anyway")

            # Advance to next question
            logger.info("Proceeding to next question after timeout")
            next_index = chat_data.get("index", 0) + 1
            await _send_question(update, context, next_index)

        except asyncio.CancelledError:
            pass  # Task was cancelled, exit gracefully
        except Exception as e:
            logger.warning(f"Timeout reveal failed: {e}")

    # Schedule all hint and timeout tasks
    chat_data["hint_tasks"] = [
        asyncio.create_task(_send_hint(
            step["time"], step["ratio"], step["points"]))
        for step in HINT_POINT_STEPS
    ] + [
        asyncio.create_task(_timeout_reveal(FINAL_REVEAL_TIME)),
    ]


def _normalize(text: str) -> str:
    """Lowercase and trim for lenient answer matching."""
    return text.strip().lower()


def _points_for_elapsed(seconds: float) -> int:
    """Return points based on elapsed time thresholds."""

    if not HINT_POINT_STEPS or seconds < HINT_POINT_STEPS[0]["time"]:
        return INITIAL_POINTS

    points = HINT_POINT_STEPS[-1]["points"]
    for step in HINT_POINT_STEPS:
        if seconds < step["time"]:
            break
        points = step["points"]
    return points


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


async def _send_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    next_index: Optional[int] = None,
) -> None:
    """
    Send the next question to the chat.

    This function handles the complete question lifecycle:
    1. Resets all per-question state (cancels old tasks, clears flags)
    2. Sends the question (with optional media)
    3. Schedules hint reveals and timeout

    Args:
        next_index: The question index to send. If None, uses current index from chat_data.
    """
    chat_data = context.chat_data
    chat_id = update.effective_chat.id
    questions: list[dict] = chat_data["questions"]

    # Determine which question to send
    if next_index is None:
        next_index = chat_data.get("index", 0)
    idx = next_index % len(questions)

    # Reset state atomically - this cancels old tasks and returns new generation
    generation = _reset_question_state(chat_data, idx)

    logger.info(
        f"Sending question {idx + 1} (gen={generation}) to chat {chat_id}")

    question_data = questions[idx]
    question_text = question_data.get("question", "")
    question_type = question_data.get("type", "text")
    answer = question_data.get("answer", "")
    file_path = question_data.get("file")

    try:
        logger.info(f"Question {idx + 1} content: {question_text}")
        full_message = f"*QUESTION {idx + 1}*\n\n{question_text}"

        # Send media with caption
        if file_path and question_type in {"image", "audio", "video"}:
            await _send_question_media_with_caption(
                context, chat_id, Path(file_path), question_type, full_message
            )
        else:
            # Just plain text question
            await context.bot.send_message(
                chat_id=chat_id,
                text=full_message,
                parse_mode="Markdown"
            )

        # Record start time, open for answers, and schedule hints
        chat_data["question_start_ts"] = monotonic()
        chat_data["accepting_answers"] = True
        _schedule_hints(update, context, chat_data,
                        chat_id, answer, generation)

        logger.info(f"Question {idx + 1} sent successfully to chat {chat_id}")

    except Exception as e:
        logger.warning(f"Failed to send question {idx + 1}: {e}")
        chat_data["answered"] = True  # Prevent answers for unsent question


async def _send_question_media_with_caption(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    asset_path: Path,
    media_type: str,
    caption: str
) -> None:
    try:
        with asset_path.open("rb") as fh:
            if media_type == "image":
                await context.bot.send_photo(
                    chat_id=chat_id,
                    photo=fh,
                    caption=caption,
                    parse_mode="Markdown"
                )
            elif media_type == "audio":
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=fh,
                    caption=caption,
                    parse_mode="Markdown"
                )
            elif media_type == "video":
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=fh,
                    caption=caption,
                    parse_mode="Markdown"
                )
    except Exception as e:
        logger.warning(f"Failed to send {media_type} asset {asset_path}: {e}")


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
        seconds = 0

    if not fancy_animation:
        await asyncio.sleep(5)
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"*{end_text}*",
            parse_mode="Markdown"
        )
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
    await countdown_timer(
        context=context,
        chat_id=update.effective_chat.id,
        seconds=wait_seconds,
    )
    try:
        await _send_question(update, context)
    except Exception as err:
        logger.warning(f"Failed to send initial question: {err}")


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
    if not _is_accepting_answers(chat_data):
        logger.info(
            f"Ignoring answer. Not accepting answers in chat {update.effective_chat.id}"
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
        return

    user = update.effective_user
    user_id = user.id if user else None
    name = user.full_name if user else "Player"

    # Mark answered immediately to prevent race conditions
    chat_data["answered"] = True
    _cancel_pending_tasks(chat_data)

    # Calculate score
    start_ts = chat_data.get("question_start_ts")
    now_ts = monotonic()
    elapsed = max(0.0, now_ts - start_ts) if start_ts else 0.0
    points = chat_data.get("current_points") or _points_for_elapsed(elapsed)

    context.bot_data[user_id] = name
    scores = chat_data.setdefault("scores", {})
    scores[user_id] = scores.get(user_id, 0) + points
    logger.info(
        f"Correct answer by user {user_id} in chat {update.effective_chat.id}; score now {scores[user_id]}"
    )

    # Announce correct answer and wait before next question
    wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
    await message.reply_text(
        f"*{answer}* is correct!\n\n{name} *+{points}*\n_answered in {elapsed:.1f}s_",
        parse_mode="Markdown"
    )
    await countdown_timer(
        context=context,
        chat_id=update.effective_chat.id,
        seconds=wait_seconds,
    )

    # Advance to next question (pass next_index explicitly)
    next_idx = idx + 1
    try:
        await _send_question(update, context, next_idx)
    except Exception as err:
        logger.warning(
            f"Failed to send next question after correct answer: {err}")


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
