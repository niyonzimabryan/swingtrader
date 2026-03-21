#!/usr/bin/env python3
"""
Swing Trader — Main Entry Point
Starts the Telegram bot + scheduled pipeline.
"""

import asyncio
import signal
import sys

from config.settings import Settings
from database.db import init_db
from orchestrator.pipeline import TradingPipeline
from orchestrator.scheduler import PipelineScheduler
from orchestrator.universe import seed_universe
from bot.telegram_bot import SwingTraderBot
from bot.message_queue import MessageQueue
from bot.notifications import NotificationManager
from execution.order_monitor import OrderMonitor
from execution.position_monitor import PositionMonitor
from bot.daily_digest import DailyDigest
from bot.weekly_report import WeeklyReport
from utils.logger import setup_logging, get_logger


def _init_langfuse(settings):
    """Initialize Langfuse OTEL auto-instrumentation if keys are configured."""
    if not settings.langfuse_public_key or not settings.langfuse_secret_key:
        return None
    try:
        import os
        import base64

        # Langfuse SDK client env vars
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
        os.environ["LANGFUSE_BASE_URL"] = settings.langfuse_base_url

        # OTEL exporter env vars (required separately for span export)
        base_url = settings.langfuse_base_url.rstrip("/")
        os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"] = f"{base_url}/api/public/otel"
        auth_string = base64.b64encode(
            f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
        ).decode()
        os.environ["OTEL_EXPORTER_OTLP_HEADERS"] = f"Authorization=Basic {auth_string}"

        # CRITICAL: get_client() MUST be called BEFORE instrument()
        # get_client() sets up the TracerProvider with the correct HTTP exporter.
        # If instrument() runs first, spans go to the default gRPC exporter
        # which Langfuse rejects with 401.
        from langfuse import get_client
        client = get_client()
        client.auth_check()  # Fail fast if creds are wrong

        from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
        AnthropicInstrumentor().instrument()
        return client
    except ImportError as e:
        print(f"[langfuse] ImportError: {e}")
        return None
    except Exception as e:
        print(f"[langfuse] Init failed: {type(e).__name__}: {e}")
        return None


async def main():
    # Setup logging
    setup_logging("INFO")
    log = get_logger("main")
    log.info("swing_trader_starting")

    # Load settings
    settings = Settings()

    # Initialize Langfuse observability (no-op if keys not set)
    langfuse_client = _init_langfuse(settings)
    if langfuse_client:
        log.info("langfuse_initialized", host=settings.langfuse_base_url)

    # Validate critical keys
    missing = []
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if not settings.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not settings.telegram_chat_id:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        log.error("missing_api_keys", keys=missing)
        print(f"\n❌ Missing required API keys: {', '.join(missing)}")
        print("Copy .env.example to .env and fill in your keys.")
        print("See .env.example for registration links.\n")
        sys.exit(1)

    # Initialize database
    init_db(settings.database_url)
    log.info("database_initialized")

    # Seed ticker universe
    seed_universe()

    # Initialize pipeline
    pipeline = TradingPipeline(settings)
    log.info("pipeline_ready")

    # Initialize Telegram bot
    bot = SwingTraderBot(settings, pipeline)
    app = bot.build()

    # Initialize message queue and notifications
    mq = MessageQueue(app.bot)
    notifications = NotificationManager(mq, settings.telegram_chat_id)
    pipeline.notification_manager = notifications
    pipeline.bot_loop = asyncio.get_running_loop()  # For deep research async scheduling

    # Initialize order monitor
    order_monitor = OrderMonitor(pipeline.alpaca, notifications, settings)

    # Initialize position monitor (60-sec live price checks during market hours)
    position_monitor = PositionMonitor(pipeline.alpaca, notifications, settings)

    # Initialize daily digest (5 PM ET, math only — no AI)
    daily_digest = DailyDigest(pipeline.alpaca, notifications, settings)

    # Initialize weekly report (Sunday 6 PM ET, Sonnet narrative — ~$0.03/week)
    weekly_report = WeeklyReport(pipeline.alpaca, notifications, settings)

    # Initialize scheduler (skip if SCHEDULER_ENABLED=false to save API credits)
    import os
    scheduler_enabled = os.getenv("SCHEDULER_ENABLED", "true").lower() not in ("false", "0", "no")
    scheduler = PipelineScheduler(pipeline, settings)
    if scheduler_enabled:
        scheduler.set_daily_digest(daily_digest)
        scheduler.set_weekly_report(weekly_report)
        scheduler.start()
        log.info("scheduler_ready")
    else:
        log.info("scheduler_disabled", reason="SCHEDULER_ENABLED=false")

    # Start bot
    log.info("starting_telegram_bot")
    print("\n✅ Swing Trader is running!")
    print(f"   Telegram bot active — send /help to your bot")
    if scheduler_enabled:
        print(f"   Scheduler: 3 daily scans at {settings.pre_market_hour}:00, {settings.midday_hour}:00, {settings.post_market_hour}:00 ET")
        print(f"   Daily digest: 5:00 PM ET (weekdays)")
        print(f"   Weekly report: Sunday 6:00 PM ET (Sonnet)")
    else:
        print(f"   ⏸ Scheduler PAUSED (set SCHEDULER_ENABLED=true to resume)")
    print(f"   Order monitor: polling every 30s")
    print(f"   Position monitor: polling every 60s (market hours only)")
    from config.tickers import UNIVERSE
    print(f"   Universe: {len(UNIVERSE)} tickers")
    print(f"   Press Ctrl+C to stop\n")

    try:
        await bot.start()

        # Start order monitor (runs as async background task)
        await order_monitor.start()
        log.info("order_monitor_started")

        # Start position monitor (60-sec price checks during market hours)
        await position_monitor.start()
        log.info("position_monitor_started")

        # Keep running
        stop_event = asyncio.Event()

        def handle_signal(sig, frame):
            stop_event.set()

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        await stop_event.wait()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting_down")
        if langfuse_client:
            langfuse_client.flush()
        await position_monitor.stop()
        await order_monitor.stop()
        scheduler.stop()
        await bot.stop()
        log.info("swing_trader_stopped")


if __name__ == "__main__":
    asyncio.run(main())
