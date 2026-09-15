"""The Robinhood protective-exit probe, as an enforced gate rather than a claim.

``docs/EXECUTION_LIFECYCLE.md`` §6 and Spec L §5.1 both say "until this probe
passes, live entries stay closed". Before this module nothing said it to the
code, and these are the rows that make the sentence testable:

* a live Robinhood entry is **refused** when no probe is on record;
* it is **allowed** when one is, for that broker and that account;
* a record for a **different account** does not authorise this one;
* **paper is unaffected** in both states, and so is a Strategy Lab paper arm;
* the refusal carries a **distinct code** and a message naming the remedy;
* absence *and invalidity* mean not probed (Spec Q §12 invariant 1);
* the migration applies and rolls back cleanly;
* the recording script has **no placement path at all** — it verifies and
  records what the owner did by hand (non-negotiable 1).
"""

from __future__ import annotations

import inspect as pyinspect
import unittest
from datetime import datetime

from alembic import command
from sqlalchemy import create_engine, inspect

from database.db import get_session
from database.models import BrokerStopProbe
from database.schema import alembic_config, ensure_schema
from execution.brokers.fake import FakeExecutionBroker
from execution.lifecycle import ExecutionRefused, ExecutionService, live_gate_refusal
from portfolio import proposals, stop_probe
from portfolio.paging import RecordingPager
from scripts import robinhood_stop_probe as probe_script
from tests import proposalfixture as pf
from tests.dbfixture import TestDatabase

#: Fake account numbers. Never a real one, and never the owner's (AGENTS.md §8).
PROBED_ACCOUNT = "TESTACCT0001"
OTHER_ACCOUNT = "TESTACCT0002"


def live_settings(**overrides):
    """Every live flag on, Robinhood primary — the configuration under test."""
    base = dict(
        execution_mode="live",
        allow_live_trading=True,
        broker_primary="robinhood",
        robinhood_account_number=PROBED_ACCOUNT,
    )
    base.update(overrides)
    return pf.settings(**base)


def record_probe(account_number: str = PROBED_ACCOUNT, **overrides):
    with get_session() as session:
        params = dict(
            broker=stop_probe.ROBINHOOD,
            account_number=account_number,
            observed_order_id="rh-stop-0001",
            observed_at=datetime(2026, 9, 15, 13, 30, 0),
            observed_symbol="F",
            observed_stop_price=9.5,
            recorded_by="owner",
            note="placed by hand, survived overnight",
        )
        params.update(overrides)
        stop_probe.record(session, **params)
        session.commit()


# --------------------------------------------------------------------------- #
# 1. The gate itself, on the one function that decides what makes live legal
# --------------------------------------------------------------------------- #


class GateTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("stop_probe_gate")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)

    def refusal(self, settings, session=...):
        if session is ...:
            with get_session() as opened:
                return live_gate_refusal(settings, opened)
        return live_gate_refusal(settings, session)

    def test_live_robinhood_is_refused_with_no_probe_on_record(self):
        code, message = self.refusal(live_settings())
        self.assertEqual(code, stop_probe.NOT_RECORDED)
        self.assertIn("scripts/robinhood_stop_probe.py", message)

    def test_live_robinhood_is_allowed_once_the_probe_is_recorded(self):
        record_probe()
        self.assertIsNone(self.refusal(live_settings()))

    def test_a_probe_on_another_account_does_not_authorise_this_one(self):
        record_probe(account_number=OTHER_ACCOUNT)
        code, message = self.refusal(live_settings())
        self.assertEqual(code, stop_probe.NOT_RECORDED)
        self.assertIn("different account", message)

    def test_a_row_with_no_observed_order_id_is_not_a_probe(self):
        """Spec Q §12 invariant 1: *invalidity* means not probed, like absence."""
        record_probe()
        with get_session() as session:
            row = session.query(BrokerStopProbe).one()
            row.observed_order_id = ""
            session.commit()
        self.assertEqual(self.refusal(live_settings())[0], stop_probe.NOT_RECORDED)

    def test_a_row_with_no_observation_timestamp_is_not_a_probe(self):
        record_probe()
        with get_session() as session:
            row = session.query(BrokerStopProbe).one()
            row.observed_at = None
            session.commit()
        self.assertEqual(self.refusal(live_settings())[0], stop_probe.NOT_RECORDED)

    def test_a_gate_with_no_session_refuses_rather_than_passes(self):
        """A caller that cannot read the record cannot establish the fact."""
        record_probe()
        code, message = self.refusal(live_settings(), session=None)
        self.assertEqual(code, stop_probe.UNVERIFIABLE)
        self.assertIn("not permission", message)

    def test_no_configured_account_refuses_with_its_own_code(self):
        code, _ = self.refusal(live_settings(robinhood_account_number=""))
        self.assertEqual(code, stop_probe.ACCOUNT_UNKNOWN)

    def test_the_flags_are_still_checked_first(self):
        """The probe is added to the conjunction, it does not replace it."""
        self.assertEqual(
            self.refusal(live_settings(allow_live_trading=False))[0],
            "live_trading_disabled",
        )
        self.assertEqual(
            self.refusal(live_settings(execution_mode="paper"))[0],
            "execution_mode_not_live",
        )

    # -- paper is untouched, in both states ---------------------------------

    def test_the_probe_gate_ignores_a_settings_object_with_no_robinhood(self):
        """The default configuration — paper, Alpaca — is not gated at all.

        ``live_gate_refusal`` still refuses these settings on the *flags*, as it
        always has; what this asserts is that the new condition adds nothing to
        a non-Robinhood deployment, so a paper-only install is untouched.
        """
        with get_session() as session:
            self.assertIsNone(stop_probe.refusal(pf.settings(), session))

    def test_a_live_alpaca_placement_is_not_gated_on_a_robinhood_probe(self):
        """The probe is about Robinhood. Alpaca needs no Robinhood stop."""
        self.assertIsNone(self.refusal(live_settings(broker_primary="alpaca")))
        record_probe()
        self.assertIsNone(self.refusal(live_settings(broker_primary="alpaca")))


# --------------------------------------------------------------------------- #
# 2. The gate where it bites: approval to placement
# --------------------------------------------------------------------------- #


class ApprovalPathTests(unittest.TestCase):
    """The same rows, driven through the real ExecutionService.

    A live proposal against a fake broker, so nothing here touches Robinhood —
    only ``BROKER_PRIMARY`` says Robinhood, which is what the gate reads and
    what decides where a real placement would have gone.
    """

    def setUp(self):
        self.db = TestDatabase("stop_probe_approval")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.now = pf.NOW
        self.settings = live_settings()
        with get_session() as session:
            pf.synced_session(session, now=self.now)
            session.commit()

    def _proposal(self, settings):
        with get_session() as session:
            row = proposals.create_proposal(
                session,
                ticker="AMD",
                entry=100.0,
                stop=95.0,
                risk_fraction=0.005,
                settings=settings,
                owner_id="99887766",
                now=self.now,
                resolver=pf.resolver_for({}),
            )
            out = (row.id, row.approval_signature)
            session.commit()
        return out

    def _approve(self, proposal_id, signature, settings):
        service = ExecutionService(
            session_factory=get_session,
            broker=FakeExecutionBroker(fill_price=100.0),
            settings=settings,
            pager=RecordingPager(),
        )
        return service.on_approval(
            proposal_id=proposal_id,
            presented_signature=signature,
            owner_id="99887766",
            now=self.now,
        )

    def test_a_live_approval_is_refused_and_places_nothing(self):
        proposal_id, signature = self._proposal(self.settings)
        broker = FakeExecutionBroker(fill_price=100.0)
        service = ExecutionService(
            session_factory=get_session,
            broker=broker,
            settings=self.settings,
            pager=RecordingPager(),
        )
        with self.assertRaises(ExecutionRefused) as ctx:
            service.on_approval(
                proposal_id=proposal_id,
                presented_signature=signature,
                owner_id="99887766",
                now=self.now,
            )
        self.assertEqual(ctx.exception.code, stop_probe.NOT_RECORDED)
        self.assertEqual(broker.order_calls, 0, broker.calls)
        with get_session() as session:
            from database.models import Proposal

            self.assertEqual(session.get(Proposal, proposal_id).status, "proposed")

    def test_the_same_approval_goes_through_once_the_probe_is_recorded(self):
        proposal_id, signature = self._proposal(self.settings)
        record_probe()
        result = self._approve(proposal_id, signature, self.settings)
        self.assertEqual(result.status, "protected", result.message)

    def test_a_paper_proposal_is_untouched_by_the_missing_probe(self):
        paper_settings = live_settings(execution_mode="paper", allow_live_trading=False)
        proposal_id, signature = self._proposal(paper_settings)
        result = self._approve(proposal_id, signature, paper_settings)
        self.assertEqual(result.status, "protected", result.message)


# --------------------------------------------------------------------------- #
# 3. A Strategy Lab paper arm, in both states
# --------------------------------------------------------------------------- #


class StrategyLabPaperTests(unittest.TestCase):
    """Spec Q §11: a paper arm reaches only Alpaca paper, so it is never gated.

    Subclassed off the real paper fixture rather than re-stubbed, so this is the
    genuine dispatch path under the most adversarial global configuration:
    ``EXECUTION_MODE=live``, ``ALLOW_LIVE_TRADING=true``,
    ``BROKER_PRIMARY=robinhood``, and no probe on record.
    """

    def setUp(self):
        from tests.test_strategy_lab_paper import PaperFixture

        self.fixture = PaperFixture("run")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def _dispatch(self):
        return self.fixture.dispatch(
            settings=self.fixture.settings_for(
                execution_mode="live",
                allow_live_trading=True,
                broker_primary="robinhood",
                robinhood_account_number=PROBED_ACCOUNT,
            )
        )

    def test_a_paper_arm_dispatches_with_no_probe_on_record(self):
        summary = self._dispatch()
        self.assertEqual(summary.proposed, 2, summary.skipped)
        self.fixture.assertNoOrders()

    def test_a_paper_arm_dispatches_the_same_way_with_one_recorded(self):
        record_probe()
        summary = self._dispatch()
        self.assertEqual(summary.proposed, 2, summary.skipped)
        self.fixture.assertNoOrders()


# --------------------------------------------------------------------------- #
# 4. The recording script: it records, it never places
# --------------------------------------------------------------------------- #


class ReadOnlyBroker:
    """A broker that answers reads and explodes on anything that could trade."""

    def __init__(self, orders):
        self._orders = list(orders)
        self.reads = 0

    def read_open_orders(self, *, symbol=None):
        self.reads += 1
        wanted = (symbol or "").strip().upper()
        return [o for o in self._orders if not wanted or o.symbol == wanted]

    def __getattr__(self, item):
        raise AssertionError(
            f"the probe script reached {item!r} on the broker. It records an "
            "observation; it never places, modifies or cancels an order "
            "(AGENTS.md non-negotiable 1)."
        )


def an_order(**overrides):
    from execution.brokers.base import OpenOrder

    params = dict(
        broker="robinhood",
        order_id="rh-stop-0001",
        symbol="F",
        side="sell",
        order_type="stop_market",
        status="queued",
        quantity=1.0,
        stop_price=9.5,
        time_in_force="gtc",
    )
    params.update(overrides)
    return OpenOrder(**params)


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("stop_probe_script")
        self.addCleanup(self.db.cleanup)
        from database.db import init_db

        init_db(self.db.url)
        self.settings = live_settings()

    def test_the_script_module_contains_no_placement_call(self):
        """A source-level assertion, because this is the one that must not drift.

        The runtime test below proves the happy path touches only reads; this
        one proves no *branch* anywhere in the module names a placement tool.
        """
        source = pyinspect.getsource(probe_script)
        for forbidden in (
            "place_equity_order",
            "place_order",
            "place_stop",
            "review_order",
            "review_equity_order",
            "submit_",
            "cancel_",
        ):
            self.assertNotIn(
                forbidden,
                source,
                f"scripts/robinhood_stop_probe.py names {forbidden!r}. It verifies "
                "and records an owner action; it never performs one.",
            )

    def test_recording_reads_the_order_and_writes_the_row(self):
        broker = ReadOnlyBroker([an_order()])
        order = probe_script.find_order(broker, "rh-stop-0001")
        with get_session() as session:
            written = probe_script.record_from(
                session, self.settings, order, recorded_by="owner", note="overnight"
            )
            session.commit()
        self.assertEqual(written.observed_order_id, "rh-stop-0001")
        self.assertEqual(written.account_masked, "****0001")
        with get_session() as session:
            self.assertIsNone(live_gate_refusal(self.settings, session))

    def test_a_gfd_stop_is_refused_and_writes_nothing(self):
        """A day stop proves nothing about the overnight question."""
        broker = ReadOnlyBroker([an_order(time_in_force="gfd")])
        order = probe_script.find_order(broker, "rh-stop-0001")
        with get_session() as session:
            with self.assertRaises(probe_script.ProbeRefused) as ctx:
                probe_script.record_from(
                    session, self.settings, order, recorded_by="owner", note=""
                )
            session.commit()
        self.assertIn("gtc", str(ctx.exception))
        with get_session() as session:
            self.assertEqual(session.query(BrokerStopProbe).count(), 0)

    def test_a_limit_order_is_refused(self):
        broker = ReadOnlyBroker([an_order(order_type="limit", stop_price=None)])
        order = probe_script.find_order(broker, "rh-stop-0001")
        with get_session() as session:
            with self.assertRaises(probe_script.ProbeRefused) as ctx:
                probe_script.record_from(
                    session, self.settings, order, recorded_by="owner", note=""
                )
        self.assertIn("stop_market", str(ctx.exception))

    def test_an_order_that_is_no_longer_at_the_broker_is_refused(self):
        """If the stop vanished, the probe did not pass. Record nothing."""
        broker = ReadOnlyBroker([])
        with self.assertRaises(probe_script.ProbeRefused) as ctx:
            probe_script.find_order(broker, "rh-stop-0001")
        self.assertIn("not found", str(ctx.exception))

    def test_candidates_lists_only_gtc_stop_orders(self):
        broker = ReadOnlyBroker(
            [
                an_order(),
                an_order(order_id="rh-limit", order_type="limit"),
                an_order(order_id="rh-day", time_in_force="gfd"),
            ]
        )
        found = probe_script.candidates(broker)
        self.assertEqual([o.order_id for o in found], ["rh-stop-0001"])

    def test_record_refuses_an_empty_observation(self):
        with get_session() as session:
            with self.assertRaises(ValueError):
                stop_probe.record(
                    session,
                    broker=stop_probe.ROBINHOOD,
                    account_number=PROBED_ACCOUNT,
                    observed_order_id="",
                    observed_at=datetime(2026, 9, 15, 13, 30, 0),
                )


# --------------------------------------------------------------------------- #
# 5. The migration
# --------------------------------------------------------------------------- #


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("stop_probe_migration")
        self.addCleanup(self.db.cleanup)

    def test_it_applies_and_rolls_back(self):
        engine = create_engine(self.db.url)
        self.addCleanup(engine.dispose)
        ensure_schema(engine)
        self.assertIn("broker_stop_probes", inspect(engine).get_table_names())

        with engine.begin() as conn:
            command.downgrade(alembic_config(conn), "0014_securities_asset_class")
        self.assertNotIn("broker_stop_probes", inspect(engine).get_table_names())

        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        self.assertIn("broker_stop_probes", inspect(engine).get_table_names())

    def test_one_account_gets_one_row(self):
        from database.db import init_db

        init_db(self.db.url)
        record_probe(observed_order_id="rh-stop-first")
        record_probe(observed_order_id="rh-stop-second")
        with get_session() as session:
            rows = session.query(BrokerStopProbe).all()
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0].observed_order_id, "rh-stop-second")


if __name__ == "__main__":
    unittest.main()
