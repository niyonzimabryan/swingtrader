"""
Telegram bot — main entry point.
Registers all command handlers and starts polling.
"""

import asyncio
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from telegram.error import Conflict
from bot.auth import init_auth
from bot.handlers.commands import (
    help_command, status_command, positions_command, regime_command,
    agents_command, exposure_command, risk_command,
    watchlist_command, upcoming_command, pause_command, resume_command, config_command,
    scan_command,
)
from bot.handlers.test_idea import test_command, score_command
from bot.handlers.callbacks import handle_callback
from bot.handlers.trade_mgmt import close_command, adjust_command
from bot.handlers.performance import performance_command, history_command, memo_command
from bot.handlers.ask import ask_command
from utils.logger import get_logger

log = get_logger("telegram_bot")


class SwingTraderBot:
    def __init__(self, settings, pipeline=None):
        self.settings = settings
        self.pipeline = pipeline
        self.app = None

    def build(self) -> Application:
        """Build the Telegram application with all handlers."""
        init_auth(self.settings.telegram_chat_id)

        self.app = Application.builder().token(self.settings.telegram_bot_token).build()

        # Store pipeline in bot_data for handler access
        self.app.bot_data["pipeline"] = self.pipeline

        # Register handlers
        self.app.add_handler(CommandHandler("start", help_command))
        self.app.add_handler(CommandHandler("help", help_command))
        self.app.add_handler(CommandHandler("status", status_command))
        self.app.add_handler(CommandHandler("positions", positions_command))
        self.app.add_handler(CommandHandler("regime", regime_command))
        self.app.add_handler(CommandHandler("test", test_command))
        self.app.add_handler(CommandHandler("score", score_command))
        self.app.add_handler(CommandHandler("agents", agents_command))
        self.app.add_handler(CommandHandler("exposure", exposure_command))
        self.app.add_handler(CommandHandler("risk", risk_command))
        self.app.add_handler(CommandHandler("close", close_command))
        self.app.add_handler(CommandHandler("adjust", adjust_command))
        self.app.add_handler(CommandHandler("performance", performance_command))
        self.app.add_handler(CommandHandler("history", history_command))
        self.app.add_handler(CommandHandler("memo", memo_command))
        self.app.add_handler(CommandHandler("ask", ask_command))
        self.app.add_handler(CommandHandler("watchlist", watchlist_command))
        self.app.add_handler(CommandHandler("upcoming", upcoming_command))
        self.app.add_handler(CommandHandler("pause", pause_command))
        self.app.add_handler(CommandHandler("resume", resume_command))
        self.app.add_handler(CommandHandler("config", config_command))
        self.app.add_handler(CommandHandler("scan", scan_command))

        # Inline keyboard callbacks
        self.app.add_handler(CallbackQueryHandler(handle_callback))

        log.info("telegram_bot_built", commands=21)
        return self.app

    async def start(self):
        """Start the bot in polling mode with retry on conflict."""
        if not self.app:
            self.build()
        log.info("telegram_bot_starting")
        await self.app.initialize()
        await self.app.start()

        # Retry polling start — handles deploy overlap where old instance is still polling
        for attempt in range(10):
            try:
                await self.app.updater.start_polling(drop_pending_updates=True)
                return
            except Conflict:
                wait = min(5 * (attempt + 1), 30)
                log.warning("telegram_polling_conflict", attempt=attempt + 1, retry_in=wait)
                await asyncio.sleep(wait)

        # Final attempt — let it raise if still conflicting
        await self.app.updater.start_polling(drop_pending_updates=True)

    async def stop(self):
        """Stop the bot."""
        if self.app:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
