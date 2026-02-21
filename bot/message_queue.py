"""
Outbound message queue with rate limiting and retry.
All outbound Telegram messages go through this module.
"""

import asyncio
from telegram import Bot, InlineKeyboardMarkup
from utils.logger import get_logger

log = get_logger("message_queue")

# Telegram rate limit: ~30 msgs/sec, we target 20/sec for safety
RATE_LIMIT_DELAY = 0.05  # 50ms between messages


class MessageQueue:
    def __init__(self, bot: Bot):
        self.bot = bot
        self._queue: asyncio.Queue = asyncio.Queue()
        self._running = False

    async def start(self):
        """Start processing the message queue."""
        self._running = True
        asyncio.create_task(self._process_queue())

    async def stop(self):
        self._running = False

    async def send(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: InlineKeyboardMarkup = None,
        parse_mode: str = "MarkdownV2",
    ) -> int | None:
        """Send a message, handling splits and retries. Returns message_id."""
        from bot.formatters import split_message
        chunks = split_message(text)
        last_msg_id = None

        for i, chunk in enumerate(chunks):
            msg_id = await self._send_with_retry(
                chat_id, chunk,
                reply_markup=reply_markup if i == len(chunks) - 1 else None,
                parse_mode=parse_mode,
            )
            if msg_id:
                last_msg_id = msg_id
            await asyncio.sleep(RATE_LIMIT_DELAY)

        return last_msg_id

    async def send_plain(self, chat_id: int | str, text: str, reply_markup=None) -> int | None:
        """Send a plain text message (no MarkdownV2)."""
        return await self._send_with_retry(chat_id, text, reply_markup=reply_markup, parse_mode=None)

    async def send_document(
        self,
        chat_id: int | str,
        document_path: str,
        caption: str = "",
        parse_mode: str = None,
    ) -> int | None:
        """Send a document (file) via Telegram. Returns message_id."""
        for attempt in range(3):
            try:
                with open(document_path, "rb") as f:
                    msg = await self.bot.send_document(
                        chat_id=chat_id,
                        document=f,
                        caption=caption[:1024] if caption else None,  # Telegram caption limit
                        parse_mode=parse_mode,
                    )
                    return msg.message_id
            except FileNotFoundError:
                log.error("document_not_found", path=document_path)
                return None
            except Exception as e:
                wait = (2 ** attempt) * 1
                log.warning("send_document_retry", attempt=attempt + 1, wait=wait, error=str(e)[:200])
                await asyncio.sleep(wait)

        log.error("send_document_failed_all_retries", chat_id=chat_id, path=document_path)
        return None

    async def _send_with_retry(
        self, chat_id, text, reply_markup=None, parse_mode="MarkdownV2", max_retries=3
    ) -> int | None:
        """Send with exponential backoff retry."""
        for attempt in range(max_retries):
            try:
                msg = await self.bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
                return msg.message_id
            except Exception as e:
                error_str = str(e)
                # If MarkdownV2 parsing fails, fall back to plain text
                if "parse" in error_str.lower() and parse_mode == "MarkdownV2":
                    log.warning("markdown_parse_failed, falling back to plain text", error=error_str[:200])
                    try:
                        msg = await self.bot.send_message(
                            chat_id=chat_id,
                            text=text,
                            parse_mode=None,
                            reply_markup=reply_markup,
                        )
                        return msg.message_id
                    except Exception as e2:
                        log.error("plain_text_fallback_failed", error=str(e2)[:200])

                wait = (2 ** attempt) * 1
                log.warning(
                    "send_retry", attempt=attempt + 1, wait=wait,
                    error=error_str[:200],
                )
                await asyncio.sleep(wait)

        log.error("send_failed_all_retries", chat_id=chat_id)
        return None

    async def _process_queue(self):
        """Background task to process queued messages."""
        while self._running:
            try:
                if not self._queue.empty():
                    item = await asyncio.wait_for(self._queue.get(), timeout=1)
                    await self._send_with_retry(**item)
                else:
                    await asyncio.sleep(0.1)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error("queue_process_error", error=str(e))
                await asyncio.sleep(1)
