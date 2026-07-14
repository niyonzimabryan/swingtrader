"""
Best-effort Telegram paging for billing/credit-exhaustion events (BRY-301).

The Anthropic and Gemini billing classifiers (utils.anthropic_client,
utils.web_search_client) run inside sync client code executed on worker
threads (ThreadPoolExecutor / run_in_executor) — they have no direct handle
on the async Telegram bot loop. main.py registers the bot's running loop and
NotificationManager here once at startup, mirroring how TradingPipeline
stores `bot_loop` for its own asyncio.run_coroutine_threadsafe calls (see
orchestrator/pipeline.py and bot/handlers/test_idea.py's executor bridge).

Before this module existed, a billing classifier firing only logged CRITICAL
and went silent — the scan-failure Telegram path only fires when a scan
raises, but per-ticker exception handling lets scans "complete" with zero
memos instead, so the 2026-07-07 credit exhaustion went unnoticed for four
days.
"""

import asyncio

from utils.logger import get_logger

log = get_logger("billing_alerts")

_notification_manager = None
_bot_loop = None
_paged_providers: set[str] = set()


def register(notification_manager, bot_loop) -> None:
    """Register the live NotificationManager + bot event loop (called once from main.py)."""
    global _notification_manager, _bot_loop
    _notification_manager = notification_manager
    _bot_loop = bot_loop


def page_once(provider: str, message: str) -> None:
    """
    Best-effort operator page — at most once per provider for this process's
    lifetime, since a billing outage would otherwise fire this on every
    single failed LLM call. Never raises: a Telegram failure must not break
    the retry classifier that called this. With no bot loop/notifier
    registered (a script, a test, or a billing error before main.py finishes
    wiring the bot), this is a silent no-op — the caller's CRITICAL log is
    the fallback record.
    """
    if provider in _paged_providers:
        return
    _paged_providers.add(provider)

    if not _notification_manager or not _bot_loop or _bot_loop.is_closed():
        return

    try:
        asyncio.run_coroutine_threadsafe(
            _notification_manager.system_message(message), _bot_loop
        )
    except Exception as e:
        log.error("billing_page_failed", provider=provider, error=str(e))
