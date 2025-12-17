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
    {"time": 7, "ratio": 0.0, "points": 4},
    {"time": 14, "ratio": 0.2, "points": 3},
    {"time": 21, "ratio": 0.4, "points": 2},
    {"time": 28, "ratio": 0.6, "points": 1},
]
FINAL_REVEAL_TIME = 35


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
    hints = question.get("hints") or []
    if hints:
        hint_text = random.choice(hints)
    else:
        hint_text = f"There are no hints available for this question.\n\n" \

    text = f"Hint for Question {idx + 1}: {hint_text}"
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
        tags = question_data.get("tags") or []
        if tags:
            tags_text = ", ".join(tags)
            full_message = f"*QUESTION {idx + 1}*\n\n{question_text}\n\n_Genre: {tags_text}_"
        else:
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

    # Preserve any pre-existing teams (e.g. created via /group before /start)
    existing_quiz = context.chat_data.get("quiz", {}) or {}
    preserved_teams = existing_quiz.get("teams")
    preserved_mute_enabled = existing_quiz.get("mute_enabled")
    preserved_mute_uses = existing_quiz.get("mute_uses")
    preserved_muted_team = existing_quiz.get("muted_team")
    preserved_muted_until = existing_quiz.get("muted_until")
    preserved_double_tags = existing_quiz.get("double_tags")

    # Store all quiz state under a single `quiz` namespace
    new_quiz = {
        "questions": questions,
        "index": 0,
        "scores": {},
        "answered": False,
        "last_winning_team": None,
        "winning_streak": 0,
    }
    if preserved_teams is not None:
        new_quiz["teams"] = preserved_teams
    if preserved_mute_enabled is not None:
        new_quiz["mute_enabled"] = preserved_mute_enabled
    if preserved_mute_uses is not None:
        new_quiz["mute_uses"] = preserved_mute_uses
    if preserved_muted_team is not None:
        new_quiz["muted_team"] = preserved_muted_team
    if preserved_muted_until is not None:
        new_quiz["muted_until"] = preserved_muted_until
    if preserved_double_tags is not None:
        new_quiz["double_tags"] = preserved_double_tags

    context.chat_data["quiz"] = new_quiz

    start_message = (
        "🎊 *QUIZ TIME!* 🎊\n\n"
        "Get ready to test your knowledge! 🧠\n"
        "💡 *Type your answers quickly!*"
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=start_message, parse_mode="Markdown")

    try:
        name_a = context.bot_data.get("TEAM_NAME_A", "A")
        name_b = context.bot_data.get("TEAM_NAME_B", "B")

        mute_enabled = new_quiz.get("mute_enabled", {})
        mute_uses = new_quiz.get("mute_uses", {})
        double_tags = new_quiz.get("double_tags", {})

        a_mute_enabled = mute_enabled.get("A", False)
        b_mute_enabled = mute_enabled.get("B", False)
        a_mute_uses = mute_uses.get("A", 0)
        b_mute_uses = mute_uses.get("B", 0)

        a_tags = sorted(list(double_tags.get("A", set()))
                        ) if double_tags else []
        b_tags = sorted(list(double_tags.get("B", set()))
                        ) if double_tags else []

        status_text = (
            "*📊 Current Team Settings*\n\n"
            f"*{name_a}*\n"
            f"• 🔇 Mute: _{'Enabled' if a_mute_enabled else 'Disabled'}_\n"
            f"• 🔢 Uses left: `{a_mute_uses}`\n"
            f"• 🏷️ Tags: _{', '.join(a_tags) if a_tags else 'none'}_\n\n"
            f"*{name_b}*\n"
            f"• 🔇 Mute: _{'Enabled' if b_mute_enabled else 'Disabled'}_\n"
            f"• 🔢 Uses left: `{b_mute_uses}`\n"
            f"• 🏷️ Tags: _{', '.join(b_tags) if b_tags else 'none'}_"
        )

        await context.bot.send_message(chat_id=update.effective_chat.id, text=status_text, parse_mode="Markdown")
    except Exception:
        logger.info("Failed to send team status on start")

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
    current_question = questions[idx]
    question_hints = current_question.get("hints") or []
    if not question_hints:
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"No hint available for Question {idx + 1}."
            )
        except Exception:
            logger.info(
                f"Could not DM {user.full_name} about missing hint.")
        return

    # Track per-chat per-user hint usage. Structure:
    # context.chat_data['hint_usage'] = { user_id: {'count': int, 'questions': set(int)} }
    usage = context.chat_data.setdefault("hint_usage", {})
    entry = usage.setdefault(user.id, {"count": 0, "questions": set()})

    MAX_HINTS = 3

    if entry["count"] >= MAX_HINTS:
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"No more hints available. You've used all {MAX_HINTS} hints."
            )
        except Exception:
            logger.info(
                f"Could not DM {user.full_name} about hint limit.")
        return

    if idx in entry["questions"]:
        try:
            await context.bot.send_message(
                chat_id=user.id,
                text=f"You've already received a hint for Question {idx + 1}."
            )
        except Exception:
            logger.info(
                f"Could not DM {user.full_name} about hint limit.")
        return

    try:
        await _send_hint_dm(user.id, idx, questions[idx], context)
    except Exception:
        await update.message.reply_text(
            f"Could not DM {user.full_name}. Please make sure you've started a chat with me."
        )
        return

    entry["count"] += 1
    entry["questions"].add(idx)


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
        return

    idx = quiz.get("index", 0) % len(questions)
    if not _is_accepting_answers(quiz):
        logger.info(
            f"Ignoring answer. Not accepting answers in chat {update.effective_chat.id}")
        return

    # Ignore answers from members of that team if the team is muted
    user = update.effective_user
    user_id = user.id if user else None
    teams = quiz.get("teams", {})
    if teams and user_id is not None:
        user_label = None
        for lab, members in teams.items():
            if any(uid == user_id for uid, _ in members):
                user_label = lab
                break

        muted_label = quiz.get("muted_team")
        muted_until = quiz.get("muted_until", 0)
        if user_label and muted_label and user_label == muted_label:
            from time import monotonic as _mon
            if _mon() < muted_until:
                logger.info(
                    f"Ignoring answer from muted team {user_label} (user {user_id})")
                return
            else:
                quiz.pop("muted_team", None)
                quiz.pop("muted_until", None)
                try:
                    chat_id = update.effective_chat.id
                    display = context.bot_data.get(
                        f"TEAM_NAME_{muted_label}", muted_label)
                    await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"{display} are no longer muted. You may answer now."
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to announce unmute in handle_answer: {e}")

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

    teams = quiz.get("teams", {})
    double_tags = quiz.get("double_tags", {})
    try:
        user_label = None
        if teams:
            for lab, members in teams.items():
                if any(uid == user_id for uid, _ in members):
                    user_label = lab
                    break

        team_double_set = set()
        if user_label and double_tags:
            team_double_set = set(double_tags.get(user_label, set()))

        question_tags = set(current.get("tags", [])) if current else set()
        matched = question_tags & team_double_set
        if matched:
            points = points * 2
            logger.info(
                f"Doubling points for user {user_id} (team {user_label}) for tags {matched}")
    except Exception:
        logger.exception("Error applying double-tags multiplier")

    context.bot_data[user_id] = name
    scores = quiz.setdefault("scores", {})
    scores[user_id] = scores.get(user_id, 0) + points
    logger.info(
        f"Correct answer by user {user_id} in chat {update.effective_chat.id}; "
        f"score now {scores[user_id]}"
    )

    # Determine the answering user's team (if teams exist) and update streak
    teams = quiz.get("teams", {})
    label = "?"
    if teams:
        for lab, members in teams.items():
            if any(uid == user_id for uid, _ in members):
                label = lab
                break

    last_label = quiz.get("last_winning_team")
    if label == last_label:
        quiz["winning_streak"] = quiz.get("winning_streak", 0) + 1
    else:
        quiz["winning_streak"] = 1
        quiz["last_winning_team"] = label

    display_name = context.bot_data.get(
        f"TEAM_NAME_{label}", label) if label and label != "?" else "?"
    streak = quiz.get("winning_streak", 1)

    if label and label != "?" and streak >= 1:
        if streak >= 5:
            team_line = f"*{display_name} IS UNSTOPPABLE! {streak} WINS!* 💥"
        elif streak >= 3:
            team_line = f"*{display_name} IS ON FIRE! {streak} 🔥 STREAK!* "
        else:
            team_line = f"{display_name} is on a {streak} 🔥 streak!"
    else:
        team_line = ""

    reply_lines = [
        f"*{answer}* is correct!",
        "",
        f"{name} *+{points}*",
        f"_answered in {elapsed:.1f}s_",
    ]
    if team_line:
        reply_lines.extend(["", team_line])

    await message.reply_text("\n".join(reply_lines), parse_mode="Markdown")

    try:
        # Only send taunts on exact streak milestones: 3 or 5
        if label and label != "?" and streak in (3, 5):
            other_label = "A" if label == "B" else "B"
            display_other = context.bot_data.get(
                f"TEAM_NAME_{other_label}", other_label)
            if streak == 5:
                taunt = f"{display_other}, you guys are getting COOKED! 🥵"
            else:
                taunt = f"Uh oh {display_other}, {display_name} is finding their rhythm! 🕺"

            await context.bot.send_message(chat_id=update.effective_chat.id, text=taunt)
    except Exception:
        logger.exception("Failed sending taunt to opposing team")

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
