"""Spec Q §14, PR 4: the scheduled job and the weekly section, both default-off.

Two invariants that are easy to break and expensive to notice:

* **No job appears with the flags off, and no existing job moves.** The three
  scans, the digest, the weekly report, the shadow-returns fill, the pattern
  drain and the daily restart keep the ids and the cron times they have today.
  The standing rule for this repository is that a PR does not change scheduled
  timings that already exist, so this test pins them.
* **The weekly report gains a section only when the lab is on**, and gains
  nothing at all — not an empty header, not a "disabled" line — when it is off.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from orchestrator.scheduler import PipelineScheduler

#: Every job the scheduler registers today, with the trigger the deploy is
#: already running. A change here is a production change and must be a
#: deliberate edit to this table, not a side effect of another PR.
EXISTING_JOBS = {
    "pre_market": "hour='7'",
    "midday": "hour='12'",
    "post_market": "hour='17'",
    "daily_digest": "hour='17', minute='0'",
    "weekly_report": "hour='18', minute='0'",
    "shadow_returns": "hour='3', minute='30'",
    "daily_restart": "hour='8', minute='57'",
}


def _settings(**overrides):
    base = dict(
        pre_market_hour=7,
        midday_hour=12,
        post_market_hour=17,
        daily_restart_time_et="08:57",
        pattern_analog_engine_enabled=False,
        portfolio_sync_enabled=False,
        strategy_lab_enabled=False,
        strategy_lab_shadow_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _scheduler(settings) -> PipelineScheduler:
    scheduler = PipelineScheduler(
        pipeline=SimpleNamespace(notification_manager=None), settings=settings
    )
    scheduler.set_daily_digest(SimpleNamespace())
    scheduler.set_weekly_report(SimpleNamespace())
    return scheduler


class SchedulerJobTests(unittest.IsolatedAsyncioTestCase):
    """`AsyncIOScheduler.start()` needs a running loop, so these are async."""

    def _start(self, settings):
        scheduler = _scheduler(settings)
        scheduler.start(enable_scans=True)
        self.addCleanup(scheduler.scheduler.shutdown, False)
        return {job.id: str(job.trigger) for job in scheduler.scheduler.get_jobs()}

    async def test_the_flags_off_register_no_strategy_lab_job(self):
        jobs = self._start(_settings())
        self.assertNotIn("strategy_lab_maturation", jobs)

    async def test_the_master_switch_alone_registers_no_job(self):
        jobs = self._start(_settings(strategy_lab_enabled=True))
        self.assertNotIn("strategy_lab_maturation", jobs)

    async def test_both_flags_register_exactly_one_job_at_four_fifteen(self):
        jobs = self._start(
            _settings(strategy_lab_enabled=True, strategy_lab_shadow_enabled=True)
        )
        self.assertIn("strategy_lab_maturation", jobs)
        self.assertIn("hour='4', minute='15'", jobs["strategy_lab_maturation"])

    async def test_no_existing_job_id_or_time_changes_either_way(self):
        for extra in ({}, {"strategy_lab_enabled": True, "strategy_lab_shadow_enabled": True}):
            jobs = self._start(_settings(**extra))
            for job_id, fragment in EXISTING_JOBS.items():
                self.assertIn(job_id, jobs, f"{job_id} disappeared")
                self.assertIn(
                    fragment, jobs[job_id],
                    f"{job_id} moved: {jobs[job_id]} does not contain {fragment}",
                )

    async def test_the_maturation_job_no_ops_defensively_when_a_flag_is_flipped_off(self):
        """Registered at boot, disabled at runtime: the job must still do nothing."""
        settings = _settings(
            strategy_lab_enabled=True, strategy_lab_shadow_enabled=True
        )
        scheduler = _scheduler(settings)
        settings.strategy_lab_shadow_enabled = False

        called: list[str] = []
        import orchestrator.strategy_lab_shadow as lab

        original = lab.mature_shadow_decisions
        lab.mature_shadow_decisions = lambda *a, **k: called.append("ran")
        self.addCleanup(lambda: setattr(lab, "mature_shadow_decisions", original))

        await scheduler._run_strategy_lab_maturation()
        self.assertEqual(called, [])


class WeeklyReportSectionTests(unittest.TestCase):
    """The report gains a section only when the lab is on."""

    def _report(self, settings):
        from bot.weekly_report import WeeklyReport

        return WeeklyReport(
            alpaca=None, notification_manager=None, settings=settings
        )

    def test_the_section_is_absent_with_the_flag_off(self):
        report = self._report(_settings())
        self.assertIsNone(report._strategy_lab_scoreboard())

        text = report._format_message(_report_data(strategy_lab=None), "WHAT WORKED\nfine")
        self.assertNotIn("SCOREBOARD", text)
        self.assertNotIn("Strategy Lab", text)

    def test_a_scoreboard_failure_costs_the_section_and_not_the_report(self):
        import orchestrator.strategy_lab_shadow as lab

        original = lab.scoreboard

        def _boom(settings, **kwargs):
            raise RuntimeError("bootstrap exploded")

        lab.scoreboard = _boom
        self.addCleanup(lambda: setattr(lab, "scoreboard", original))

        report = self._report(_settings(strategy_lab_enabled=True))
        self.assertIsNone(report._strategy_lab_scoreboard())

    def test_the_section_appears_when_a_payload_exists(self):
        report = self._report(_settings(strategy_lab_enabled=True))
        from tests.test_strategy_lab_bot import PAYLOAD

        text = report._format_message(
            _report_data(strategy_lab=PAYLOAD), "WHAT WORKED\nfine"
        )
        self.assertIn("SCOREBOARD", text)
        self.assertIn("Recommendations are informational", text)
        # And the sections the report already had are still there, above it.
        self.assertLess(text.index("RESULTS"), text.index("SCOREBOARD"))
        self.assertLess(text.index("SIGNAL QUALITY"), text.index("SCOREBOARD"))


def _report_data(*, strategy_lab):
    return {
        "week_start": "Jun 22",
        "week_end": "Jun 26, 2026",
        "equity": 100_000.0,
        "trades_opened": 2,
        "trades_closed": 1,
        "win_rate": 100.0,
        "wins": 1,
        "losses": 0,
        "realized_pnl": 120.0,
        "total_memos": 3,
        "approved": 1,
        "passed": 2,
        "watchlisted": 0,
        "avg_approved_score": 0.7,
        "avg_passed_score": 0.4,
        "open_positions": 0,
        "positions": [],
        "closed_details": [],
        "profitable_positions": 0,
        "calibration": {},
        "cohort_pnl": {},
        "strategy_lab": strategy_lab,
    }


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
