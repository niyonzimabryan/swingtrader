"""
Shared memo delivery helper for Telegram.
Ensures all-or-nothing formatting fallback behavior.
"""

from typing import Any

from bot.formatters import split_memo_message, split_message, strip_markdown
from utils.logger import get_logger

log = get_logger("memo_delivery")


def _is_markdown_parse_error(exc: Exception) -> bool:
    error = str(exc).lower()
    return "parse" in error and (
        "entity" in error or
        "markdown" in error or
        "can't find end" in error
    )


async def _delete_sent_messages(bot: Any, chat_id: int | str, message_ids: list[int], source: str) -> None:
    for message_id in message_ids:
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception as e:
            log.warning(
                "memo_rollback_delete_failed",
                source=source,
                message_id=message_id,
                error=str(e)[:200],
            )


async def _send_plain_chunks(
    message: Any,
    text: str,
    keyboard: Any | None,
    source: str,
) -> list[int]:
    plain_text = strip_markdown(text)
    plain_chunks = split_message(plain_text)
    sent_ids: list[int] = []
    for i, chunk in enumerate(plain_chunks):
        is_last = i == len(plain_chunks) - 1
        msg = await message.reply_text(
            chunk,
            parse_mode=None,
            reply_markup=keyboard if is_last else None,
        )
        sent_ids.append(msg.message_id)

    log.info(
        "memo_plain_fallback_sent",
        source=source,
        chunks=len(plain_chunks),
    )
    return sent_ids


async def send_memo_markdown_or_plain(
    *,
    message: Any,
    bot: Any,
    chat_id: int | str,
    memo_text: str,
    keyboard: Any | None = None,
    source: str = "memo",
) -> list[int]:
    """
    Send memo with MarkdownV2 first.
    If any Markdown parse error occurs, delete prior markdown chunks and resend
    the full memo as plain text chunks.
    """
    md_chunks = split_memo_message(memo_text)
    sent_markdown_ids: list[int] = []

    for i, chunk in enumerate(md_chunks):
        is_last = i == len(md_chunks) - 1
        try:
            msg = await message.reply_text(
                chunk,
                parse_mode="MarkdownV2",
                reply_markup=keyboard if is_last else None,
            )
            sent_markdown_ids.append(msg.message_id)
        except Exception as e:
            parse_error = _is_markdown_parse_error(e)
            log.warning(
                "memo_markdown_chunk_failed",
                source=source,
                chunk_index=i,
                chunks_total=len(md_chunks),
                parse_error=parse_error,
                error=str(e)[:200],
                fallback_path="plain_full_resend" if parse_error else "raise",
            )
            if not parse_error:
                raise

            if sent_markdown_ids:
                await _delete_sent_messages(
                    bot=bot,
                    chat_id=chat_id,
                    message_ids=sent_markdown_ids,
                    source=source,
                )

            return await _send_plain_chunks(
                message=message,
                text=memo_text,
                keyboard=keyboard,
                source=source,
            )

    log.info(
        "memo_markdown_sent",
        source=source,
        chunks=len(md_chunks),
    )
    return sent_markdown_ids
