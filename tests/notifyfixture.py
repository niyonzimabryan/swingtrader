"""Fakes and payload builders for the ``notify/`` tests.

No network anywhere: both transports are injected callables that record what
they were handed and return whatever the test asked them to.

The name is deliberately not ``test_*``: ``unittest discover -p "test_*.py"``
would otherwise import it as a test module.
"""

from __future__ import annotations

from notify.channel import Notification

#: A key shaped like Resend's, so a test asserting the Authorization header is
#: asserting something realistic. Not a credential: it is 16 bytes of `f` and
#: matches nothing.
FAKE_RESEND_KEY = "re_" + "f" * 24
FAKE_TELEGRAM_TOKEN = "1234567890:" + "T" * 20
FAKE_CHAT_ID = "987654321"

#: A card-link signing secret for the tests. Not a credential.
FAKE_CARD_SECRET = "card-link-secret-for-tests-only"


class RecordingTransport:
    """A stand-in HTTP transport. Records every call; returns a scripted reply."""

    def __init__(self, status: int = 200, body: dict | None = None, raises: Exception | None = None):
        self.status = status
        self.body = body if body is not None else {"id": "resend-message-1"}
        self.raises = raises
        self.calls: list[dict] = []

    # Resend's signature is (url, headers, payload, timeout); Telegram's is
    # (url, payload, timeout). One class serves both by keying on arity.
    def __call__(self, *args):
        if len(args) == 4:
            url, headers, payload, timeout = args
        else:
            url, payload, timeout = args
            headers = {}
        self.calls.append(
            {"url": url, "headers": dict(headers), "payload": payload, "timeout": timeout}
        )
        if self.raises is not None:
            raise self.raises
        return self.status, self.body

    @property
    def last(self) -> dict | None:
        return self.calls[-1] if self.calls else None


class RecordingChannel:
    """A ``Channel`` that keeps what it was sent."""

    def __init__(self, name: str = "recording", result: bool = True):
        self.name = name
        self.result = result
        self.sent: list[Notification] = []

    def send(self, notification: Notification) -> bool:
        self.sent.append(notification)
        return self.result

    @property
    def last(self) -> Notification | None:
        return self.sent[-1] if self.sent else None


class ExplodingChannel:
    """A channel that breaks its contract, to prove ``broadcast`` survives one."""

    name = "exploding"

    def send(self, notification: Notification) -> bool:
        raise RuntimeError("this channel is broken")


class FakeSettings:
    """The subset of ``Settings`` the delivery layer reads.

    A plain object rather than a real ``Settings``: ``config.settings`` calls
    ``load_dotenv(override=True)``, so a gitignored ``.env`` on a developer's
    laptop would otherwise decide what these tests assert.
    """

    def __init__(self, **overrides):
        self.notify_email_enabled = False
        self.resend_api_key = ""
        self.pager_email_from = ""
        self.pager_email_to = ""
        self.telegram_bot_token = ""
        self.telegram_chat_id = ""
        self.card_link_secret = ""
        self.execution_approval_secret = ""
        self.workspace_base_url = ""
        self.card_chart_sessions = 60
        self.phase6_execution_enabled = False
        for key, value in overrides.items():
            setattr(self, key, value)


def email_settings(**overrides) -> FakeSettings:
    """Settings with the email channel fully configured."""
    base = dict(
        notify_email_enabled=True,
        resend_api_key=FAKE_RESEND_KEY,
        pager_email_from="swingtrader@example.invalid",
        pager_email_to="owner@example.invalid",
        card_link_secret=FAKE_CARD_SECRET,
        workspace_base_url="https://workspace.example.invalid",
    )
    base.update(overrides)
    return FakeSettings(**base)


def telegram_settings(**overrides) -> FakeSettings:
    base = dict(telegram_bot_token=FAKE_TELEGRAM_TOKEN, telegram_chat_id=FAKE_CHAT_ID)
    base.update(overrides)
    return FakeSettings(**base)


# --------------------------------------------------------------------------- #
# Payloads
# --------------------------------------------------------------------------- #

#: Twelve sessions of made-up closes. Enough for a chart, small enough to read.
BARS = [
    {"date": f"2026-09-{day:02d}", "close": close}
    for day, close in zip(
        range(1, 13),
        [100.0, 101.5, 99.25, 98.0, 102.75, 104.0, 103.5, 105.25, 107.0, 106.25, 108.5, 109.0],
    )
]


def chart(**overrides) -> dict:
    base = {
        "ticker": "AMD",
        "title": "AMD — last 12 sessions",
        "bars": list(BARS),
        "levels": {"entry": 108.0, "stop": 99.0, "target": 120.0},
        "as_of_note": "split-adjusted closes through 2026-09-12, from the stored price plane",
        "caption": "Split-adjusted closes, 2026-09-01 to 2026-09-12.",
    }
    base.update(overrides)
    return base


def proposal_row(**overrides) -> dict:
    """A ``portfolio.proposals.proposal_payload``-shaped dict."""
    base = {
        "proposal_id": 42,
        "proposal_uid": "11111111-2222-3333-4444-555555555555",
        "status": "proposed",
        "ticker": "AMD",
        "side": "long",
        "entry": 108.0,
        "stop": 99.0,
        "expected_hold_sessions": 15,
        "budget": "evidenced",
        "risk_fraction": 0.005,
        "risk_fraction_effective": 0.0031,
        "quantity": 34,
        "notional": 3672.0,
        "risk_dollars": 306.0,
        "evidence": {
            "cohort_answer_id": "cohort:3182",
            "lower_bound": 0.0124,
            "point_estimate": 0.0201,
            "horizon_sessions": 15,
            "m": 0.6169,
            "gate_mode": "advisory",
            "reason": "cited_ok: the cited answer is full/ok for AMD, computed 2 sessions ago.",
        },
        "caps": {
            "evidenced_risk_cap": {"bound": True, "limit": 0.01, "shares_allowed": 34},
            "concentration": {"bound": False, "limit_notional": 10000.0, "shares_allowed": 92},
            "settled_cash": {"bound": False, "limit_notional": 8400.0, "shares_allowed": 77},
        },
        "account": "robinhood-agentic",
        "execution_mode": "paper",
        "equity_at_proposal": 100000.0,
        "portfolio_context_hash": "9f2c4b1a77e30ddc5512",
        "ledger_as_of_utc": "2026-09-13T13:45:00",
        "rejection_code": None,
        "rejection_reason": None,
        "approval_expires_at": "2026-09-13T14:15:00",
    }
    base.update(overrides)
    return base


def refused_proposal_row(**overrides) -> dict:
    base = proposal_row(
        status="risk_rejected",
        budget="discretionary",
        quantity=0,
        notional=0.0,
        risk_dollars=0.0,
        risk_fraction_effective=0.0,
        caps={},
        rejection_code="insufficient_settled_cash",
        rejection_reason=(
            "settled cash is $1,204.00; this entry needs $3,672.00. The sale that "
            "funds it settles on 2026-09-16."
        ),
        approval_expires_at=None,
        evidence={
            "cohort_answer_id": "",
            "lower_bound": -0.0088,
            "point_estimate": 0.0042,
            "horizon_sessions": None,
            "m": None,
            "gate_mode": "advisory",
            "reason": "uncited: no cohort answer was cited, so this is discretionary.",
        },
    )
    base.update(overrides)
    return base


def scoreboard(**overrides) -> dict:
    """A ``scripts.strategy_lab_scoreboard.build_scorecard``-shaped dict."""
    base = {
        "schema": "strategy_lab_scoreboard/1",
        "inputs": {
            "experiment": "shadow_roster_v1",
            "cutoff_utc": "2026-09-13T00:00:00",
            "primary_metric": "mean_r",
            "floors": {"matured": 30, "distinct_dates": 20},
            "costs": {"commission_bps": 0.0, "slippage_bps": 5.0},
        },
        "variants": {"planned_variants": 4, "n_tried": 3, "n_trials": 4},
        "sections": {
            "clean": {
                "arms": [
                    {
                        "arm": "momentum_v1@1.0.0/shadow",
                        "status": "insufficient_evidence",
                        "n_matured": 12,
                        "n_open": 3,
                        "mean_net_pct": 0.8123,
                        "median_net_pct": 0.4011,
                        "mean_r": 0.1902,
                        "win_rate": 0.5833,
                        "profit_factor": 1.2201,
                        "max_drawdown_pct": -4.3300,
                        "benchmark_relative_pct": None,
                        "n_trials": 4,
                        "warnings": ["below_matured_floor"],
                        "uncertainty": None,
                        "adjusted_lower": None,
                        "stepm_rejected": None,
                        "stepm_note": "",
                    }
                ],
                "leader": {"arm": "momentum_v1@1.0.0/shadow", "cleared_gate": False},
                "overlaps": [],
            }
        },
        "refusals": [{"arm": "meanrev_v1@0.9.0/shadow", "reason": "no settled trade"}],
        "warnings": ["fewer_variants_run_than_declared", "below_matured_floor"],
        "notes": ["Promotion is owner-only."],
    }
    base.update(overrides)
    return base


def memo_details() -> list[dict]:
    return [
        {
            "ticker": "HIMS",
            "score": 0.81,
            "classification": "catalyst / guidance raise",
            "memo_id": 501,
            "opus_recommendation": "proceed",
        },
        {
            "ticker": "OSCR",
            "score": 0.62,
            "classification": "technical / base breakout",
            "memo_id": 502,
            "opus_recommendation": "watchlist",
        },
    ]
