import os
from dotenv import load_dotenv


def load_config() -> dict:
    load_dotenv()
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError(
            "Missing TELEGRAM_BOT_TOKEN."
        )
    delay = int(os.getenv("QUIZ_DELAY_SECONDS", "10"))
    admin_id = os.getenv("ADMIN_USER_ID")
    admin_id = int(admin_id) if admin_id else None
    return {
        "TELEGRAM_BOT_TOKEN": token,
        "QUIZ_DELAY_SECONDS": delay,
        "ADMIN_USER_ID": admin_id,
    }
