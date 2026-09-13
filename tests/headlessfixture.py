"""Fakes for driving ``main.py``'s startup path without a broker or a network.

``main()`` is the one function in this repo that nothing could test, because it
built a Telegram ``Application`` on line three of its wiring. The headless
runtime makes that conditional, which makes the whole path testable — so this
module exists to hold the fakes rather than to duplicate them in two test files.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.

Everything here records and returns; nothing opens a socket, a database, or a
broker session. ``run_main`` starts ``main.main()`` as a task, waits until the
process has finished coming up, and then calls the SIGTERM handler ``main()``
installed — directly, not by signalling the process, so a test can never leave
the test runner's own handlers replaced.
"""

from __future__ import annotations

import asyncio
import signal
import sys
from contextlib import ExitStack, contextmanager
from unittest import mock


class FakeSettings:
    """Whatever ``main()`` reads, with ``""`` for anything not named.

    A permissive ``__getattr__`` rather than an exhaustive list: ``main()``
    reads two dozen settings for one log line and a banner, and a fake that has
    to be extended every time somebody adds a printed field is a fake that
    rots. The values that *decide* something are explicit below.
    """

    def __init__(self, **overrides):
        self.anthropic_api_key = "sk-ant-fake"
        self.telegram_enabled = True
        self.telegram_bot_token = ""
        self.telegram_chat_id = ""
        self.owner_id = ""
        self.database_url = "sqlite://"
        self.phase6_execution_enabled = False
        self.owner_action_poller_enabled = False
        self.owner_action_poll_seconds = 20
        self.execution_mode = "paper"
        self.notify_email_enabled = False
        self.resend_api_key = ""
        self.pager_email_from = ""
        self.pager_email_to = ""
        self.card_link_secret = ""
        self.execution_approval_secret = ""
        self.workspace_base_url = ""
        self.card_chart_sessions = 60
        self.langfuse_public_key = ""
        self.langfuse_secret_key = ""
        self.strategy_lab_paper_enabled = False
        self.strategy_lab_live_enabled = False
        for key, value in overrides.items():
            setattr(self, key, value)

    def __getattr__(self, name):  # pragma: no cover - only for printed fields
        if name.startswith("_"):
            raise AttributeError(name)
        return ""


class FakeBroker:
    name = "alpaca"
    venue = "paper"
    account_number = "FAKE"

    def get_positions_detail(self):
        return []


class FakeOrderManager:
    async def execute_approved_trade(self, memo_id):  # pragma: no cover - not driven here
        return {"memo_id": memo_id}


class FakePipeline:
    def __init__(self, settings):
        self.settings = settings
        self.broker = FakeBroker()
        self.paper_broker = FakeBroker()
        self.primary_broker = FakeBroker()
        self.order_manager = FakeOrderManager()
        self.notification_manager = None
        self.bot_loop = None


class FakeMonitor:
    """Stands in for OrderMonitor, PositionMonitor, MonitorWatchdog and the poller."""

    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeScheduler:
    def __init__(self, pipeline, settings):
        self.pipeline = pipeline
        self.settings = settings
        self.started = False
        self.enable_scans = None
        self.stopped = False
        self.restart_callback = None
        self.daily_digest = None
        self.weekly_report = None

    def set_restart_callback(self, callback):
        self.restart_callback = callback

    def set_daily_digest(self, digest):
        self.daily_digest = digest

    def set_weekly_report(self, report):
        self.weekly_report = report

    def start(self, enable_scans=True):
        self.started = True
        self.enable_scans = enable_scans

    def stop(self):
        self.stopped = True


class FakeApp:
    def __init__(self):
        self.bot = object()
        self.bot_data: dict = {}


class FakeBot:
    """``SwingTraderBot``. Records that it was built, started and stopped."""

    instances: list = []

    def __init__(self, settings, pipeline=None):
        self.settings = settings
        self.pipeline = pipeline
        self.app = None
        self.started = False
        self.stopped = False
        FakeBot.instances.append(self)

    def build(self):
        self.app = FakeApp()
        self.app.bot_data["pipeline"] = self.pipeline
        return self.app

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


class FakeMessageQueue:
    instances: list = []

    def __init__(self, bot):
        self.bot = bot
        self.sent: list = []
        FakeMessageQueue.instances.append(self)

    async def send(self, chat_id, text, reply_markup=None, parse_mode="MarkdownV2"):
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup})
        return 1

    async def send_plain(self, chat_id, text, reply_markup=None):
        self.sent.append({"chat_id": chat_id, "text": text, "plain": True})
        return 1

    async def send_document(self, chat_id, document_path, caption="", parse_mode=None):
        self.sent.append({"chat_id": chat_id, "document": document_path, "caption": caption})
        return 1


class Harness:
    """What a run produced: the fakes ``main()`` built, by name."""

    def __init__(self):
        self.pipelines: list = []
        self.monitors: list = []
        self.schedulers: list = []
        self.pollers: list = []
        self.execution_services: list = []
        self.strategy_services: list = []
        self.signal_handlers: dict = {}
        self.bot_data: dict = {}

    @property
    def pipeline(self):
        return self.pipelines[-1] if self.pipelines else None

    @property
    def scheduler(self):
        return self.schedulers[-1] if self.schedulers else None

    @property
    def poller(self):
        return self.pollers[-1] if self.pollers else None


@contextmanager
def patched_main(settings, *, patch_telegram: bool = True):
    """Patch everything ``main()`` constructs. Yields a :class:`Harness`.

    ``patch_telegram`` false leaves ``bot.telegram_bot`` and
    ``bot.message_queue`` alone *and unimported*, which is how the headless test
    proves the process never reaches for them: patching a class is an import of
    the module that defines it.
    """
    import main as main_module

    harness = Harness()

    def make_pipeline(*args, **kwargs):
        pipeline = FakePipeline(settings)
        harness.pipelines.append(pipeline)
        return pipeline

    def make_monitor(*args, **kwargs):
        monitor = FakeMonitor(*args, **kwargs)
        harness.monitors.append(monitor)
        return monitor

    def make_scheduler(pipeline, s):
        scheduler = FakeScheduler(pipeline, s)
        harness.schedulers.append(scheduler)
        return scheduler

    def make_poller(*args, **kwargs):
        poller = FakeMonitor(*args, **kwargs)
        harness.pollers.append(poller)
        return poller

    def make_execution_service(*args, **kwargs):
        service = mock.Mock(name="ExecutionService")
        harness.execution_services.append(service)
        return service

    def make_strategy_service(*args, **kwargs):
        service = mock.Mock(name="StrategyExecutionService")
        harness.strategy_services.append(service)
        return service

    def fake_signal(sig, handler):
        harness.signal_handlers[sig] = handler
        return None

    with ExitStack() as stack:
        patch = stack.enter_context
        patch(mock.patch.object(main_module, "Settings", lambda: settings))
        patch(mock.patch.object(main_module, "_init_langfuse", lambda s: None))
        patch(mock.patch.object(main_module, "init_db", lambda url: None))
        patch(mock.patch.object(main_module, "seed_universe", lambda: None))
        patch(mock.patch.object(main_module, "TradingPipeline", make_pipeline))
        patch(mock.patch.object(main_module, "_reconcile_startup_positions", lambda *a: None))
        patch(mock.patch.object(main_module, "OrderMonitor", make_monitor))
        patch(mock.patch.object(main_module, "PositionMonitor", make_monitor))
        patch(mock.patch.object(main_module, "MonitorWatchdog", make_monitor))
        patch(mock.patch.object(main_module, "DailyDigest", lambda *a: mock.Mock()))
        patch(mock.patch.object(main_module, "WeeklyReport", lambda *a: mock.Mock()))
        patch(mock.patch.object(main_module, "PipelineScheduler", make_scheduler))
        patch(mock.patch.object(signal, "signal", fake_signal))

        import execution.lifecycle
        import execution.strategy_lifecycle
        import orchestrator.approval_poller

        patch(
            mock.patch.object(
                execution.lifecycle, "ExecutionService", make_execution_service
            )
        )
        patch(
            mock.patch.object(
                execution.strategy_lifecycle,
                "StrategyExecutionService",
                make_strategy_service,
            )
        )
        patch(
            mock.patch.object(orchestrator.approval_poller, "ApprovalPoller", make_poller)
        )

        if patch_telegram:
            import bot.message_queue
            import bot.telegram_bot

            FakeBot.instances = []
            FakeMessageQueue.instances = []
            patch(mock.patch.object(bot.telegram_bot, "SwingTraderBot", FakeBot))
            patch(mock.patch.object(bot.message_queue, "MessageQueue", FakeMessageQueue))

        yield harness


async def run_main(harness: Harness, *, timeout: float = 10.0) -> None:
    """Run ``main.main()`` to a clean shutdown, driven by its own SIGTERM handler."""
    import main as main_module

    task = asyncio.create_task(main_module.main())
    # A `sys.exit()` inside the task is a BaseException, which asyncio re-raises
    # straight out of the loop — so `run_main` never gets to await the task and
    # Python later complains that its exception was never retrieved. Reading it
    # here marks it retrieved without changing what propagates.
    task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
    deadline = asyncio.get_running_loop().time() + timeout
    while signal.SIGTERM not in harness.signal_handlers:
        if task.done():
            await task  # re-raise whatever stopped it
            return
        if asyncio.get_running_loop().time() > deadline:  # pragma: no cover
            task.cancel()
            raise AssertionError("main() never installed its signal handlers")
        await asyncio.sleep(0.01)
    harness.signal_handlers[signal.SIGTERM](signal.SIGTERM, None)
    await asyncio.wait_for(task, timeout=timeout)


def drop_telegram_modules() -> None:
    """Forget the Telegram bot modules so "was it imported" is answerable."""
    for name in ("bot.telegram_bot", "bot.message_queue", "bot.handlers.proposals"):
        sys.modules.pop(name, None)
