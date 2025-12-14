# Quizzit Telegram Bot

A Telegram bot for running interactive quizzes in group chats. Features timed scoring, progressive hints, team support, and media question types (images, audio, video).

## Setup

- Ensure Python 3.14+ is installed.
- Create a bot via [BotFather](https://t.me/botfather) and obtain your `TELEGRAM_BOT_TOKEN`.
- Optionally, create a `.env` file in the project root with:

  ```
  TELEGRAM_BOT_TOKEN=your_token_here
  ```

## Installation

```bash
uv sync
```

## Running Locally

```bash
uv run src/main.py
```

## Docker

These are helper bash scripts for easy testing and deployment.

```bash
./scripts/build.sh
```

```bash
./scripts/run.sh
```

**Note:** Ensure `.env`, `questions.json`, and `assets` directory exist in the project root before running the Docker container, as they are mounted as volumes.
