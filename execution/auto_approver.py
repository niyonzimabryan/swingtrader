"""Paper autonomy sandbox (Spec I2).

After a scan completes, auto-submit the SAME order the human approve button
would — but ONLY in the Alpaca PAPER account, and only for candidates that meet
cohort rules and caps. This removes the human gate where it controls nothing
(zero-risk paper execution) and yields execution data for every cohort.

Two cohorts:
- memo:        final_score >= auto_approve_min_score (a memo already exists)
- exploration: [exploration_min_score, auto_approve_min_score) — no operator
               memo is sent; a lightweight memo is synthesized purely to reuse
               the order path, at a reduced position size.

HARD SAFETY GUARD: auto-approval is structurally impossible outside the Alpaca
PAPER adapter. `is_paper_safe()` is checked before any order is placed; if live
mode or Robinhood is active it logs, notifies once, and places nothing.
"""

from __future__ import annotations

import asyncio

from database.db import get_session
from database.models import Memo, Trade
from tracking.shadow_ledger import mark_paper_traded
from utils.timeutils import utcnow_naive
from utils.logger import get_logger

log = get_logger("auto_approver")


class AutoApprover:
    def __init__(self, settings, order_manager, memo_generator, broker, notifier=None):
        """`notifier` is a callable(str) -> awaitable for the ONE-time non-paper
        alert, or None (tests). `broker` is the runtime BrokerRouter."""
        self.settings = settings
        self.order_manager = order_manager
        self.memo_generator = memo_generator
        self.broker = broker
        self._notify = notifier
        self._non_paper_notified = False

    # ── Hard safety guard ────────────────────────────────────────────────────

    def is_paper_safe(self) -> bool:
        """True only when the active broker is the Alpaca PAPER adapter.

        Fails closed: live execution mode, ALLOW_LIVE_TRADING, a non-Alpaca
        primary (Robinhood), or a broker flagged live_trading all return False.
        """
        s = self.settings
        mode = str(getattr(s, "execution_mode", "paper")).lower()
        if mode != "paper":
            return False
        if bool(getattr(s, "allow_live_trading", False)):
            return False
        if not bool(getattr(s, "alpaca_paper_only", True)):
            return False
        active = getattr(self.broker, "active", self.broker)
        if getattr(active, "name", "") != "alpaca":
            return False
        if bool(getattr(active, "live_trading", False)):
            return False
        return True

    # ── Main entry ───────────────────────────────────────────────────────────

    async def run(self, candidates: list) -> dict:
        """Place paper orders for eligible candidates. Returns a summary dict.

        `candidates` are objects exposing: ticker, final_score, cohort,
        scoring_result, regime, memo_id (or None), ledger_id (or None).
        Mutates each placed candidate's `auto_executed` flag for the scan summary.
        """
        result = {"enabled": False, "refused": False, "placed": 0, "memo": 0, "exploration": 0}

        if not bool(getattr(self.settings, "auto_approve_paper", False)):
            return result
        result["enabled"] = True

        if not self.is_paper_safe():
            result["refused"] = True
            log.warning(
                "auto_approve_refused_non_paper",
                execution_mode=str(getattr(self.settings, "execution_mode", "")),
                allow_live_trading=bool(getattr(self.settings, "allow_live_trading", False)),
                broker=getattr(getattr(self.broker, "active", self.broker), "name", "?"),
            )
            await self._notify_non_paper_once()
            return result

        # Current holdings → concurrency budget + dedupe.
        try:
            positions = await asyncio.to_thread(self.broker.get_positions_detail)
        except Exception as e:
            log.error("auto_approve_positions_failed", error=str(e))
            positions = []
        held = {str(p.get("ticker") or p.get("symbol") or "").upper() for p in positions}
        slots = max(0, int(getattr(self.settings, "auto_max_concurrent_positions", 8)) - len(positions))

        memo_min = float(getattr(self.settings, "auto_approve_min_score", 0.55))
        expl_min = float(getattr(self.settings, "exploration_min_score", 0.45))
        expl_enabled = bool(getattr(self.settings, "exploration_band_enabled", True))
        max_memo = int(getattr(self.settings, "auto_max_new_positions_per_scan", 4))
        max_expl = int(getattr(self.settings, "exploration_max_new_per_scan", 2))
        factor = float(getattr(self.settings, "exploration_position_pct_factor", 0.5))

        memo_cands = sorted(
            [c for c in candidates if c.final_score >= memo_min and c.ticker.upper() not in held],
            key=lambda c: c.final_score, reverse=True,
        )
        expl_cands = sorted(
            [c for c in candidates
             if expl_min <= c.final_score < memo_min and c.ticker.upper() not in held],
            key=lambda c: c.final_score, reverse=True,
        ) if expl_enabled else []

        # Memo cohort first (higher priority for scarce concurrency slots).
        for cand in memo_cands:
            if slots <= 0 or result["memo"] >= max_memo:
                break
            if await self._place(cand, "memo", factor=1.0):
                held.add(cand.ticker.upper())
                slots -= 1
                result["memo"] += 1
                result["placed"] += 1

        for cand in expl_cands:
            if slots <= 0 or result["exploration"] >= max_expl:
                break
            if await self._place(cand, "exploration", factor=factor):
                held.add(cand.ticker.upper())
                slots -= 1
                result["exploration"] += 1
                result["placed"] += 1

        log.info("auto_approve_complete", **{k: result[k] for k in ("placed", "memo", "exploration")})
        return result

    # ── Placement ────────────────────────────────────────────────────────────

    async def _place(self, cand, cohort: str, factor: float) -> bool:
        memo_id = getattr(cand, "memo_id", None)
        if cohort == "exploration":
            memo = self.memo_generator.create_exploration_memo(
                cand.ticker, cand.scoring_result, cand.regime or {}, position_pct_factor=factor,
            )
            memo_id = memo.get("memo_id") if memo else None
        if not memo_id:
            log.warning("auto_approve_no_memo", ticker=cand.ticker, cohort=cohort)
            return False

        try:
            outcome = await self.order_manager.execute_approved_trade(memo_id)
        except Exception as e:
            log.error("auto_approve_place_error", ticker=cand.ticker, cohort=cohort, error=str(e))
            return False

        if not outcome.get("success"):
            log.warning(
                "auto_approve_place_rejected",
                ticker=cand.ticker, cohort=cohort, error=outcome.get("error", ""),
            )
            return False

        tag = "AUTO:memo" if cohort == "memo" else "AUTO:exploration"
        self._tag_trade(outcome.get("trade_id"), tag)
        if cohort == "memo":
            self._mark_memo_approved(memo_id)
        mark_paper_traded(getattr(cand, "ledger_id", None), cohort)
        cand.auto_executed = True
        log.info(
            "auto_approve_order_placed",
            ticker=cand.ticker, cohort=cohort,
            score=round(cand.final_score, 3), trade_id=outcome.get("trade_id"),
        )
        return True

    def _tag_trade(self, trade_id, tag: str) -> None:
        if not trade_id:
            return
        try:
            with get_session() as session:
                trade = session.query(Trade).filter_by(id=trade_id).first()
                if trade:
                    existing = trade.operator_notes or ""
                    trade.operator_notes = f"{existing}|{tag}" if existing else tag
        except Exception as e:
            log.error("auto_approve_tag_failed", trade_id=trade_id, error=str(e))

    def _mark_memo_approved(self, memo_id: int) -> None:
        try:
            with get_session() as session:
                memo = session.query(Memo).filter_by(id=memo_id).first()
                if memo and memo.status == "pending":
                    memo.status = "approved"
                    memo.responded_at = utcnow_naive()
        except Exception as e:
            log.error("auto_approve_memo_mark_failed", memo_id=memo_id, error=str(e))

    async def _notify_non_paper_once(self) -> None:
        if self._non_paper_notified or not self._notify:
            return
        self._non_paper_notified = True
        try:
            await self._notify(
                "🛑 Auto-approve (paper) is enabled but the active broker is NOT the "
                "Alpaca paper adapter — refusing to auto-place any order. "
                "Auto-trading is paper-only by design."
            )
        except Exception as e:
            log.error("auto_approve_notify_failed", error=str(e))
