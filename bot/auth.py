"""
Telegram bot authentication — chat_id whitelist.
Unauthorized users are silently ignored.
"""

from functools import wraps
from telegram import Update
from telegram.ext import ContextTypes
from utils.logger import get_logger

log = get_logger("bot_auth")

_authorized_ids: set[str] = set()


def init_auth(chat_id: str):
    """Initialize the authorized chat IDs from settings."""
    global _authorized_ids
    _authorized_ids = {cid.strip() for cid in chat_id.split(",") if cid.strip()}
    log.info("auth_initialized", authorized_count=len(_authorized_ids))


def is_authorized(chat_id: int) -> bool:
    return str(chat_id) in _authorized_ids


def authorized(func):
    """Decorator to restrict bot commands to authorized users."""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if not update.effective_chat:
            return
        if not is_authorized(update.effective_chat.id):
            log.warning("unauthorized_access", chat_id=update.effective_chat.id)
            return  # Silent ignore — don't reveal bot exists
        return await func(update, context, *args, **kwargs)
    return wrapper
