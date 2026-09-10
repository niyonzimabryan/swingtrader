"""Builders for Phase 6 proposal, evidence, and execution tests.

Three things the §8 tests need and the rest of the suite does not:

* a **cited answer** that carries a ticker, an ``as_of`` date, and a policy
  interval. The real Spec N engine does not yet emit any of those on a
  ``PolicySummary`` (Phase 3c, tracked in ``portfolio/evidence.py`` and
  ``docs/EXECUTION_LIFECYCLE.md``), so an evidenced-path test has to construct
  an answer of the right *shape*. :func:`cited_answer` builds one that
  ``comparables.report.assert_citable`` accepts — same predicate the production
  code runs — with the four fields the evidence gate reads bolted on. Where the
  test only needs a citability *refusal* (a ``quick`` or ``insufficient``
  answer), it uses the real ``tests.comparablesfixture`` answers instead, so
  those assertions run against the genuine article.
* a **cash Agentic account** with recorded capabilities, holdings, and settled
  cash, synced through the real ``run_sync`` path so the ledger the proposal
  reads is the one the writer produced.
* a **settings stub** with the Phase 6 knobs, defaulting to advisory mode.

The name is deliberately not ``test_*`` so ``unittest discover`` does not import
it as a test module.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from types import SimpleNamespace

from comparables.report import CITATION_REQUIRED_FIELDS
from execution.brokers.fake import FakeAccount, FakeBroker
from portfolio.capabilities import BrokerCapabilities
from portfolio.records import AccountRecord, CashRecord, HoldingRecord, PendingSettlement
from portfolio.sync import run_sync

NOW = datetime(2026, 9, 9, 14, 0, 0)


# --------------------------------------------------------------------------- #
# A cited cohort answer of the right shape
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Interval:
    lower: float
    estimate: float
    level: float = 0.90


@dataclass(frozen=True)
class _Policy:
    policy_slug: str
    net: float
    net_ci: _Interval | None = None


@dataclass(frozen=True)
class _Horizon:
    horizon_sessions: int
    policy: _Policy


@dataclass(frozen=True)
class _Setup:
    slug: str
    content_hash: str
    ticker: str
    as_of: date


@dataclass(frozen=True)
class _Answer:
    """An answer object shaped exactly as the evidence gate reads it.

    It carries every field in :data:`CITATION_REQUIRED_FIELDS` so
    ``assert_citable`` accepts it (that predicate checks presence, not type),
    plus ``setup`` and ``horizons`` for the same-ticker, recency, and
    policy-bound reads. It is *not* a ``comparables.report`` dataclass, and it
    does not pretend to be a full engine answer — it is the minimum an evidenced
    proposal needs to resolve, which is all a fake resolver should stand in for.
    """

    setup: _Setup
    depth: str
    status: str
    horizons: tuple

    def __post_init__(self):
        for name in CITATION_REQUIRED_FIELDS:
            if not hasattr(self, name):
                object.__setattr__(self, name, None)

    refusal_reason: str = ""


def cited_answer(
    *,
    ticker: str = "AMD",
    as_of: date = NOW.date(),
    depth: str = "full",
    status: str = "ok",
    policy_net: float = 0.08,
    policy_lb: float | None = 0.04,
    horizon: int = 5,
    slug: str = "syn_v1",
):
    """A citable-shaped answer. ``policy_lb=None`` means no interval was published."""
    interval = None if policy_lb is None else _Interval(lower=policy_lb, estimate=policy_net)
    return _Answer(
        setup=_Setup(slug=slug, content_hash="abcd1234ef567890", ticker=ticker.upper(), as_of=as_of),
        depth=depth,
        status=status,
        horizons=(_Horizon(horizon_sessions=horizon, policy=_Policy(policy_slug="p", net=policy_net, net_ci=interval)),),
    )


def resolver_for(mapping: dict):
    """A fake ``citations.resolve`` returning answers by id (Spec N §8 seam)."""

    def _resolve(answer_id: str):
        return mapping.get(answer_id)

    return _resolve


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def settings(**overrides):
    """A settings stub carrying the Phase 6 knobs, advisory by default."""
    base = dict(
        phase6_execution_enabled=True,
        allow_live_trading=False,
        execution_mode="paper",
        risk_fraction_percentage_floor=0.05,
        risk_fraction_hard_cap=0.01,
        evidenced_risk_cap=0.01,
        evidenced_daily_notional=0.0,
        discretionary_risk_cap=0.0025,
        discretionary_daily_notional=0.0,
        evidence_gate_mode="advisory",
        citation_max_age_sessions=5,
        protection_window_seconds=1,
        protection_poll_interval_seconds=0.01,
        approval_ttl_seconds=1800,
        execution_approval_secret="test-approval-secret",
        proposal_max_position_pct=0.10,
        proposal_max_sector_pct=0.30,
        portfolio_freshness_budget_minutes=60,
        robinhood_order_type="limit",
        telegram_chat_id="99887766",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# --------------------------------------------------------------------------- #
# A ledger with a cash Agentic account
# --------------------------------------------------------------------------- #


AGENTIC = AccountRecord(
    broker="fake",
    external_account_id="****4021",
    label="Agentic",
    account_type="cash",
    booking_method="STRICT",
    agent_placeable=True,
    enabled=True,
)

PRIMARY = AccountRecord(
    broker="fake",
    external_account_id="****7788",
    label="Primary",
    account_type="margin",
    booking_method="FIFO",
    agent_placeable=False,
    enabled=True,
)

PROTECT_CAPS = BrokerCapabilities(
    can_read_positions=True,
    can_read_orders=True,
    can_read_cash=True,
    can_place_equity_market=True,
    can_place_equity_limit=True,
    can_place_attached_stop=False,
    can_place_standalone_gtc_stop=True,
    supports_fractional=True,
    supports_specified_lot_sale=True,
)


def _holding(symbol, quantity, price, basis=None):
    return HoldingRecord(
        symbol=symbol,
        quantity=quantity,
        instrument_type="equity",
        average_cost=(basis / quantity) if (basis and quantity) else None,
        cost_basis=basis,
        last_price=price,
        market_value=quantity * price,
        notional=quantity * price,
        source="fake",
    )


def broker(
    *,
    as_of=NOW,
    settled_cash=100_000.0,
    unsettled_cash=0.0,
    pending=(),
    agentic_holdings=None,
    primary_holdings=None,
    capabilities=PROTECT_CAPS,
    no_protect=False,
):
    """A two-account fake: one placeable cash Agentic account, one read-only.

    ``no_protect=True`` declares an account that cannot place a standalone stop,
    for the capability-gate refusal test.
    """
    caps = capabilities
    if no_protect:
        caps = BrokerCapabilities(
            can_read_positions=True,
            can_read_orders=True,
            can_read_cash=True,
            can_place_equity_market=True,
            can_place_equity_limit=True,
            can_place_attached_stop=False,
            can_place_standalone_gtc_stop=False,
        )
    agentic = FakeAccount(
        record=AGENTIC,
        holdings=tuple(agentic_holdings if agentic_holdings is not None else ()),
        cash=CashRecord(
            settled_cash=settled_cash,
            unsettled_cash=unsettled_cash,
            buying_power=settled_cash,
            pending_settlements=tuple(pending),
        ),
    )
    primary = FakeAccount(
        record=PRIMARY,
        holdings=tuple(primary_holdings if primary_holdings is not None else ()),
        cash=CashRecord(settled_cash=0.0, unsettled_cash=0.0, buying_power=0.0),
    )
    b = FakeBroker(accounts=[agentic, primary], as_of_utc=as_of)
    b.declared_capabilities = caps
    return b


def pending_settlement(amount, settles_on):
    return PendingSettlement(amount=amount, settles_on=settles_on, source="test")


def synced_session(session, *, now=NOW, **broker_kwargs):
    """Sync a fresh ledger into ``session`` and return the SyncResult."""
    return run_sync(session, broker(as_of=now, **broker_kwargs), now=now)


def holding(symbol, quantity, price, basis=None):
    return _holding(symbol, quantity, price, basis)
