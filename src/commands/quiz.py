import asyncio
import json
import logging
import math
import random
from pathlib import Path
from time import monotonic
from typing import Optional

from telegram import Update
from telegram.ext import ContextTypes

from src.commands.utils import (
    require_group,
    require_admin,
    record_user,
    countdown_timer,
)
from src.commands.scores import show_scores

logger = logging.getLogger(__name__)

QUESTIONS_FILE = Path(__file__).resolve(
).parent.parent.parent / "questions.json"
INITIAL_POINTS = 5
HINT_POINT_STEPS = [
    {"time": 8, "ratio": 0.0, "points": 4},
    {"time": 14, "ratio": 0.2, "points": 3},
    {"time": 20, "ratio": 0.4, "points": 2},
    {"time": 25, "ratio": 0.6, "points": 1},
]
FINAL_REVEAL_TIME = 30


def _load_questions() -> list[dict]:
    """Load questions from JSON file."""
    with QUESTIONS_FILE.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _get_quiz_data(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.chat_data.setdefault("quiz", {})


def _clear_quiz_state(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data.pop("quiz", None)


def _normalize(text: str) -> str:
    """Lowercase and trim for lenient answer matching."""
    return text.strip().lower()


def _cancel_pending_tasks(chat_data: dict) -> None:
    """Cancel all scheduled hint/reveal tasks, avoiding self-cancellation."""
    tasks: list[asyncio.Task] = chat_data.pop("hint_tasks", [])
    current_task = asyncio.current_task()
    for task in tasks:
        if task is current_task:
            continue
        if not task.done():
            task.cancel()


def _reset_question_state(chat_data: dict, next_index: int) -> int:
    """Reset all per-question state atomically and return the new generation ID."""
    _cancel_pending_tasks(chat_data)
    generation = chat_data.get("generation", 0) + 1

    chat_data.update({
        "index": next_index,
        "generation": generation,
        "answered": False,
        "accepting_answers": False,
        "question_start_ts": None,
        "current_points": INITIAL_POINTS,
        "hint_tasks": [],
        "revealed_indices": set(),
    })

    return generation


def _is_stale(chat_data: dict, generation: int) -> bool:
    """Check if the current generation is stale."""
    return chat_data.get("generation") != generation or chat_data.get("answered", False)


def _is_accepting_answers(chat_data: dict) -> bool:
    return chat_data.get("accepting_answers", False) and not chat_data.get("answered", False)


def _points_for_elapsed(seconds: float) -> int:
    if not HINT_POINT_STEPS or seconds < HINT_POINT_STEPS[0]["time"]:
        return INITIAL_POINTS

    points = HINT_POINT_STEPS[-1]["points"]
    for step in HINT_POINT_STEPS:
        if seconds < step["time"]:
            break
        points = step["points"]
    return points


def _build_progressive_hint(answer: str, revealed: set[int]) -> str:
    masked = []
    for idx, ch in enumerate(answer):
        if ch.isspace():
            masked.append("  ")
        elif idx in revealed:
            masked.append(ch)
        else:
            masked.append("_ ")
    return "".join(masked).strip()


async def _send_hint_dm(
    user_id: int,
    idx: int,
    question: dict,
    context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Send a hint via DM to the user."""
    answer = question.get("answer", "")
    if not answer:
        return
    first_char = answer[0]
    length = len(answer)
    text = f"Hint for Question {idx + 1}: starts with '{first_char}' and has {length} characters."
    await context.bot.send_message(chat_id=user_id, text=text)


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
                    chat_id=chat_id, photo=fh, caption=caption, parse_mode="Markdown"
                )
            elif media_type == "audio":
                await context.bot.send_audio(
                    chat_id=chat_id, audio=fh, caption=caption, parse_mode="Markdown"
                )
            elif media_type == "video":
                await context.bot.send_video(
                    chat_id=chat_id, video=fh, caption=caption, parse_mode="Markdown"
                )
    except Exception as e:
        logger.warning(f"Failed to send {media_type} asset {asset_path}: {e}")


def _schedule_hints(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    chat_data: dict,
    chat_id: int,
    answer: str,
    generation: int,
) -> None:
    """Schedule timed hint reveals and auto-reveal for the current question."""
    if not answer:
        return

    revealable_indices = [i for i, ch in enumerate(answer) if not ch.isspace()]
    reveal_order = revealable_indices.copy()
    random.shuffle(reveal_order)
    chat_data["reveal_order"] = reveal_order

    async def _send_hint(delay: int, ratio: float, points: int) -> None:
        try:
            await asyncio.sleep(delay)
            if _is_stale(chat_data, generation):
                return

            target_count = math.ceil(len(reveal_order) * ratio)
            revealed = set(reveal_order[:target_count])
            chat_data["revealed_indices"] = revealed
            chat_data["current_points"] = points

            hint_text = _build_progressive_hint(answer, revealed)
            await context.bot.send_message(chat_id=chat_id, text=f"Hint: {hint_text}")
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Failed to send hint: {e}")

    async def _timeout_reveal(delay: int) -> None:
        try:
            await asyncio.sleep(delay)
            if _is_stale(chat_data, generation):
                return

            chat_data["answered"] = True

            await context.bot.send_message(
                chat_id=chat_id,
                text=f"*❌ No one guessed!*\n\nThe correct answer was: *{answer}*",
                parse_mode="Markdown"
            )

            wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
            if wait_seconds > 0:
                try:
                    await countdown_timer(context=context, chat_id=chat_id, seconds=wait_seconds)
                except Exception:
                    logger.warning(
                        "Countdown after timeout failed; continuing anyway")

            logger.info("Proceeding to next question after timeout")
            next_index = chat_data.get("index", 0) + 1
            await send_question(update, context, next_index)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning(f"Timeout reveal failed: {e}")

    chat_data["hint_tasks"] = [
        asyncio.create_task(_send_hint(
            step["time"], step["ratio"], step["points"]))
        for step in HINT_POINT_STEPS
    ] + [
        asyncio.create_task(_timeout_reveal(FINAL_REVEAL_TIME)),
    ]


async def send_question(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    next_index: Optional[int] = None,
) -> None:
    """Send the next question to the chat."""
    chat_id = update.effective_chat.id
    quiz = _get_quiz_data(context)
    questions: list[dict] = quiz["questions"]

    if next_index is None:
        next_index = quiz.get("index", 0)

    if next_index >= len(questions):
        await show_scores(update, context, force=True)
        _clear_quiz_state(context)
        return

    idx = next_index

    generation = _reset_question_state(quiz, idx)

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

        if file_path and question_type in {"image", "audio", "video"}:
            await _send_question_media_with_caption(
                context, chat_id, Path(file_path), question_type, full_message
            )
        else:
            await context.bot.send_message(
                chat_id=chat_id, text=full_message, parse_mode="Markdown"
            )

        quiz["question_start_ts"] = monotonic()
        quiz["accepting_answers"] = True
        _schedule_hints(update, context, quiz,
                        chat_id, answer, generation)

        logger.info(f"Question {idx + 1} sent successfully to chat {chat_id}")

    except Exception as e:
        logger.warning(f"Failed to send question {idx + 1}: {e}")
        quiz["answered"] = True


# -----------------------------------------------------------------------------
# Command handlers
# -----------------------------------------------------------------------------

@require_group
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Start the quiz in the group chat."""
    if not await require_admin(update, context):
        return

    questions = _load_questions()
    logger.info(f"Loaded {len(questions)} questions from {QUESTIONS_FILE}")

    # Store all quiz state under a single `quiz` namespace
    context.chat_data["quiz"] = {
        "questions": questions,
        "index": 0,
        "scores": {},
        "answered": False,
    }

    start_message = (
        "🎊 *QUIZ TIME!* 🎊\n\n"
        "Get ready to test your knowledge! 🧠\n"
        "💡 *Type your answers quickly!*"
    )
    await update.effective_message.reply_text(start_message, parse_mode="Markdown")

    wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
    await countdown_timer(
        context=context,
        chat_id=update.effective_chat.id,
        seconds=wait_seconds,
    )

    try:
        await send_question(update, context)
    except Exception as err:
        logger.warning(f"Failed to send initial question: {err}")


async def hint(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """DM the user a hint for the current question."""
    user = update.effective_user
    if not user:
        return

    quiz = context.chat_data.get("quiz", {})
    questions: list[dict] = quiz.get("questions")
    if not questions:
        await update.message.reply_text("Quiz not started here. Send /start to begin.")
        return

    idx = quiz.get("index", 0) % len(questions)
    try:
        await _send_hint_dm(user.id, idx, questions[idx], context)
    except Exception:
        await update.message.reply_text(
            f"Could not DM {user.full_name}. Please make sure you've started a chat with me."
        )


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    record_user(update, context)

    quiz = context.chat_data.get("quiz", {})
    questions: list[dict] = quiz.get("questions")
    if not questions:
        logger.info(
            f"Received answer but no quiz state for chat {update.effective_chat.id}")
        await message.reply_text("Quiz not started here. Send /start to begin.")
        return

    idx = quiz.get("index", 0) % len(questions)
    if not _is_accepting_answers(quiz):
        logger.info(
            f"Ignoring answer. Not accepting answers in chat {update.effective_chat.id}")
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
        f"Answer attempt by {user_info} in chat {update.effective_chat.id}: "
        f"submitted='{submitted}' expected='{expected}'"
    )

    if submitted not in expected_values:
        logger.info(
            f"Incorrect answer by {user_info} in chat {update.effective_chat.id}")
        return

    user = update.effective_user
    user_id = user.id if user else None
    name = user.full_name if user else "Player"

    # Mark answered immediately to prevent race conditions
    quiz["answered"] = True
    _cancel_pending_tasks(quiz)

    # Calculate score
    start_ts = quiz.get("question_start_ts")
    now_ts = monotonic()
    elapsed = max(0.0, now_ts - start_ts) if start_ts else 0.0
    points = quiz.get("current_points") or _points_for_elapsed(elapsed)

    context.bot_data[user_id] = name
    scores = quiz.setdefault("scores", {})
    scores[user_id] = scores.get(user_id, 0) + points
    logger.info(
        f"Correct answer by user {user_id} in chat {update.effective_chat.id}; "
        f"score now {scores[user_id]}"
    )

    # Announce correct answer
    await message.reply_text(
        f"*{answer}* is correct!\n\n{name} *+{points}*\n_answered in {elapsed:.1f}s_",
        parse_mode="Markdown"
    )

    wait_seconds = context.bot_data.get("QUIZ_DELAY_SECONDS", 0)
    await countdown_timer(
        context=context,
        chat_id=update.effective_chat.id,
        seconds=wait_seconds,
    )

    # Advance to next question
    next_idx = idx + 1
    try:
        await send_question(update, context, next_idx)
    except Exception as err:
        logger.warning(
            f"Failed to send next question after correct answer: {err}")
