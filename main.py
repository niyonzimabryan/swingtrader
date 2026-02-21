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
from utils.logger import setup_logging, get_logger


async def main():
    # Setup logging
    setup_logging("INFO")
    log = get_logger("main")
    log.info("swing_trader_starting")

    # Load settings
    settings = Settings()

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

    # Initialize scheduler
    scheduler = PipelineScheduler(pipeline, settings)
    scheduler.start()
    log.info("scheduler_ready")

    # Start bot
    log.info("starting_telegram_bot")
    print("\n✅ Swing Trader is running!")
    print(f"   Telegram bot active — send /help to your bot")
    print(f"   Scheduler: 3 daily scans at {settings.pre_market_hour}:00, {settings.midday_hour}:00, {settings.post_market_hour}:00 ET")
    print(f"   Order monitor: polling every 30s")
    from config.tickers import UNIVERSE
    print(f"   Universe: {len(UNIVERSE)} tickers")
    print(f"   Press Ctrl+C to stop\n")

    try:
        await bot.start()

        # Start order monitor (runs as async background task)
        await order_monitor.start()
        log.info("order_monitor_started")

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
        await order_monitor.stop()
        scheduler.stop()
        await bot.stop()
        log.info("swing_trader_stopped")


if __name__ == "__main__":
    asyncio.run(main())
