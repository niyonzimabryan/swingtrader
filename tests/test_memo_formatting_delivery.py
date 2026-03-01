import unittest
from types import SimpleNamespace

from bot.formatters import (
    SAFE_LIMIT,
    TELEGRAM_MSG_LIMIT,
    format_memo,
    split_memo_message,
    split_message,
    strip_markdown,
)
from bot.handlers._memo_delivery import _is_markdown_parse_error, send_memo_markdown_or_plain


def _build_long_memo() -> str:
    sonnet = "SONNET\n" + ("A" * 2400)
    opus = "═══ *OPUS EVALUATION* ═══\n" + ("B" * 2100)
    return f"{sonnet}\n\n{opus}"


def _build_memo_data(*, peer_text: str, web_text: str, delta_clamped: bool = False) -> dict:
    opus_eval = {
        "conviction": "moderate",
        "recommendation": "proceed",
        "final_score": 0.42,
        "reasoning": "Opus reasoning text.",
        "stress_test": "Stress test text.",
        "key_risk": "Key risk text.",
        "position_size_adjustment": 1.0,
    }
    if delta_clamped:
        opus_eval["delta_clamped"] = True
        opus_eval["original_opus_score"] = 1.23

    return {
        "ticker": "AVGO",
        "direction": "long",
        "direction_raw": "bullish",
        "composite_score": 0.38,
        "adjusted_score": 0.22,
        "classification": "no_action",
        "generated_at": "2026-03-01T18:25:00Z",
        "thesis": "Test thesis.",
        "catalyst": {
            "catalyst_type": "earnings_surprise",
            "catalyst_modifiers": ["sector_macro"],
            "catalyst_summary": "Catalyst summary.",
            "materiality": 0.85,
            "direction_confidence": 0.42,
            "expected_impact_pct": {"low": -14.0, "high": 18.0},
            "time_horizon_days": 5,
        },
        "fundamental": {
            "quality_score": 0.86,
            "valuation_score": 0.82,
            "growth_score": 0.75,
            "balance_sheet_score": 0.50,
            "flags": ["accelerating_growth"],
            "peer_comparison": peer_text,
        },
        "pattern": {
            "status": "no_data",
            "reasoning": "No historical instances found.",
        },
        "web_research": {
            "status": "active",
            "key_finding": "Key finding sentence.",
            "catalyst_context": web_text,
            "competitive_dynamics": web_text,
            "management_signals": web_text,
            "bull_bear_debate": web_text,
            "institutional_positioning": web_text,
        },
        "risk_analysis": {"status": "none"},
        "trade_params": {
            "entry_price": 100.1,
            "stop_loss": 96.2,
            "stop_pct": 3.9,
            "target_1": 108.0,
            "target_1_pct": 7.9,
            "target_2": 112.0,
            "target_2_pct": 11.9,
            "risk_reward": 2.0,
            "position_pct": 5.0,
            "dollar_amount": 5000.0,
            "shares": 49,
            "max_hold_days": 20,
        },
        "signal_breakdown": {
            "catalyst": {"direction": "bullish"},
            "fundamental": {"direction": "bullish"},
            "pattern": {"direction": "neutral"},
            "web_research": {"direction": "bullish"},
        },
        "regime": {"regime": "neutral", "position_size_multiplier": 1.0},
        "opus_evaluation": opus_eval,
    }


class FakeMessage:
    def __init__(self, fail_markdown_chunk: int | None = None, fail_markdown_message: str | None = None):
        self.chat_id = 12345
        self.fail_markdown_chunk = fail_markdown_chunk
        self.fail_markdown_message = fail_markdown_message or (
            "BadRequest: can't parse entities: Can't find end of the entity starting at byte offset 42"
        )
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
            raise Exception(self.fail_markdown_message)

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
    def test_parse_detection_matches_reserved_character_variant(self):
        error = "BadRequest: Can't parse entities: character '.' is reserved and must be escaped with the preceding '\\'"
        self.assertTrue(_is_markdown_parse_error(Exception(error)))

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

    def test_format_memo_web_and_peer_sections_not_clipped(self):
        peer_text = ("peercomparison " * 40) + "PEEREND"
        web_text = ("webdetail " * 60) + "WEBEND"
        text = format_memo(_build_memo_data(peer_text=peer_text, web_text=web_text))
        self.assertIn(peer_text, text)
        self.assertIn(web_text, text)

    def test_format_memo_clamped_score_note_code_wraps_decimal(self):
        text = format_memo(_build_memo_data(peer_text="peer", web_text="web", delta_clamped=True))
        self.assertIn("clamped from `1.23`", text)


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

    async def test_reserved_character_parse_failure_also_rolls_back_and_resends_plain(self):
        reserved_char_error = (
            "BadRequest: Can't parse entities: character '.' is reserved and must be escaped with the preceding '\\'"
        )
        message = FakeMessage(fail_markdown_chunk=2, fail_markdown_message=reserved_char_error)
        bot = FakeBot()
        keyboard = object()
        memo_text = _build_long_memo()

        sent_ids = await send_memo_markdown_or_plain(
            message=message,
            bot=bot,
            chat_id=message.chat_id,
            memo_text=memo_text,
            keyboard=keyboard,
            source="test_case_reserved",
        )

        markdown_attempts = [a for a in message.attempts if a["parse_mode"] == "MarkdownV2"]
        plain_attempts = [a for a in message.attempts if a["parse_mode"] is None]
        expected_plain_chunks = split_message(strip_markdown(memo_text))

        self.assertEqual(len(markdown_attempts), 2)
        self.assertEqual(len(bot.deleted), 1)
        self.assertEqual(len(plain_attempts), len(expected_plain_chunks))
        self.assertEqual(plain_attempts[-1]["reply_markup"], keyboard)
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
