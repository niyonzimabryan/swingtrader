import unittest
from types import SimpleNamespace

from bot.formatters import (
    SAFE_LIMIT,
    TELEGRAM_MSG_LIMIT,
    split_memo_message,
    split_message,
    strip_markdown,
)
from bot.handlers._memo_delivery import send_memo_markdown_or_plain


def _build_long_memo() -> str:
    sonnet = "SONNET\n" + ("A" * 2400)
    opus = "═══ *OPUS EVALUATION* ═══\n" + ("B" * 2100)
    return f"{sonnet}\n\n{opus}"


class FakeMessage:
    def __init__(self, fail_markdown_chunk: int | None = None):
        self.chat_id = 12345
        self.fail_markdown_chunk = fail_markdown_chunk
        self._next_message_id = 100
        self.attempts = []
        self.sent = []

    async def reply_text(self, text, parse_mode=None, reply_markup=None):
        md_attempt_index = (
            sum(1 for a in self.attempts if a["parse_mode"] == "MarkdownV2") + 1
            if parse_mode == "MarkdownV2" else None
        )
        attempt = {
            "text": text,
            "parse_mode": parse_mode,
            "reply_markup": reply_markup,
            "markdown_attempt_index": md_attempt_index,
        }
        self.attempts.append(attempt)

        if (
            parse_mode == "MarkdownV2"
            and self.fail_markdown_chunk is not None
            and md_attempt_index == self.fail_markdown_chunk
        ):
            raise Exception(
                "BadRequest: can't parse entities: Can't find end of the entity starting at byte offset 42"
            )

        self._next_message_id += 1
        msg = SimpleNamespace(message_id=self._next_message_id)
        sent = dict(attempt)
        sent["message_id"] = msg.message_id
        self.sent.append(sent)
        return msg


class FakeBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))


class TestMemoFormatting(unittest.TestCase):
    def test_split_message_unsplit_preserves_markdown_with_odd_underscores(self):
        text = "Type: `earnings_preannounce`\nDone"
        self.assertEqual(split_message(text, limit=SAFE_LIMIT), [text])
        self.assertEqual(split_memo_message(text, limit=SAFE_LIMIT), [text])

    def test_split_memo_prefers_two_way_split_before_opus(self):
        text = _build_long_memo()
        chunks = split_memo_message(text, limit=SAFE_LIMIT)
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[1].startswith("═══ *OPUS EVALUATION* ═══"))
        self.assertEqual("".join(chunks), text)

    def test_split_memo_chunks_never_exceed_telegram_limit(self):
        base = _build_long_memo()
        text = base + "\n\n" + ("TAIL " * 1500)
        chunks = split_memo_message(text, limit=SAFE_LIMIT)
        self.assertTrue(chunks)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), TELEGRAM_MSG_LIMIT)
        self.assertEqual("".join(chunks), text)


class TestMemoDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_parse_failure_rolls_back_and_resends_plain_full_memo(self):
        message = FakeMessage(fail_markdown_chunk=2)
        bot = FakeBot()
        keyboard = object()
        memo_text = _build_long_memo()

        sent_ids = await send_memo_markdown_or_plain(
            message=message,
            bot=bot,
            chat_id=message.chat_id,
            memo_text=memo_text,
            keyboard=keyboard,
            source="test_case",
        )

        markdown_attempts = [a for a in message.attempts if a["parse_mode"] == "MarkdownV2"]
        plain_attempts = [a for a in message.attempts if a["parse_mode"] is None]
        self.assertEqual(len(markdown_attempts), 2)
        self.assertGreaterEqual(len(plain_attempts), 1)

        # One markdown chunk should have sent successfully before chunk-2 parse failure.
        self.assertEqual(len(bot.deleted), 1)
        self.assertEqual(bot.deleted[0][0], message.chat_id)

        # Fallback re-sends full plain memo split, with keyboard on final chunk only.
        expected_plain_chunks = split_message(strip_markdown(memo_text))
        self.assertEqual(len(plain_attempts), len(expected_plain_chunks))
        self.assertEqual(plain_attempts[-1]["reply_markup"], keyboard)
        if len(plain_attempts) > 1:
            for attempt in plain_attempts[:-1]:
                self.assertIsNone(attempt["reply_markup"])

        self.assertEqual(len(sent_ids), len(expected_plain_chunks))

    async def test_successful_markdown_send_has_no_plain_fallback(self):
        message = FakeMessage()
        bot = FakeBot()
        keyboard = object()
        memo_text = _build_long_memo()

        sent_ids = await send_memo_markdown_or_plain(
            message=message,
            bot=bot,
            chat_id=message.chat_id,
            memo_text=memo_text,
            keyboard=keyboard,
            source="test_case",
        )

        markdown_attempts = [a for a in message.attempts if a["parse_mode"] == "MarkdownV2"]
        plain_attempts = [a for a in message.attempts if a["parse_mode"] is None]
        expected_md_chunks = split_memo_message(memo_text)

        self.assertEqual(len(markdown_attempts), len(expected_md_chunks))
        self.assertEqual(len(plain_attempts), 0)
        self.assertEqual(len(sent_ids), len(expected_md_chunks))
        self.assertEqual(bot.deleted, [])
