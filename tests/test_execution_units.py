"""Focused unit tests for the Phase 6 building blocks (Spec L §6.3, §6.6).

The proposal and lifecycle tests exercise these through the full path; this file
pins the arithmetic and the signing in isolation, where an off-by-one in the
evidence clip or a weakness in the single-use check is easiest to see.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from portfolio import approvals, evidence
from portfolio.sizing import (
    Caps,
    SizingRefused,
    compute_size,
    validate_risk_fraction,
)
from tests import proposalfixture as pf


class RiskFractionValidationTests(unittest.TestCase):
    def test_the_percentage_floor_is_inclusive(self):
        # 0.05 exactly is refused as a percentage; just below is accepted.
        with self.assertRaises(SizingRefused) as ctx:
            validate_risk_fraction(0.05, hard_cap=0.01)
        self.assertEqual(ctx.exception.code, "percentage_input")

    def test_the_hard_cap_is_a_refusal_not_a_clamp(self):
        with self.assertRaises(SizingRefused) as ctx:
            validate_risk_fraction(0.02, hard_cap=0.01)
        self.assertEqual(ctx.exception.code, "risk_fraction_above_hard_cap")

    def test_a_valid_fraction_passes_through_unchanged(self):
        self.assertEqual(validate_risk_fraction(0.005, hard_cap=0.01), 0.005)

    def test_zero_and_negative_are_refused(self):
        for bad in (0.0, -0.001):
            with self.assertRaises(SizingRefused):
                validate_risk_fraction(bad, hard_cap=0.01)


class SizingTests(unittest.TestCase):
    def _caps(self, **kw):
        base = dict(budget_risk_cap=0.01, hard_cap=0.01, equity=100_000.0)
        base.update(kw)
        return Caps(**base)

    def test_whole_share_floor_and_zero_refusal(self):
        # 0.005 * 100k = $500 risk / $5 per share = 100 shares.
        size = compute_size(entry=100, stop=95, risk_fraction=0.005, multiplier=None, caps=self._caps())
        self.assertEqual(size.quantity, 100)
        self.assertEqual(size.notional, 10_000.0)
        # A tiny fraction rounds to zero shares and is refused.
        with self.assertRaises(SizingRefused) as ctx:
            compute_size(entry=100, stop=95, risk_fraction=0.00001, multiplier=None, caps=self._caps())
        self.assertEqual(ctx.exception.code, "zero_shares")

    def test_multiplier_scales_and_is_never_above_one_here(self):
        # multiplier applies before caps: 0.005 * 0.5 = 0.0025 → $250 → 50 shares.
        size = compute_size(entry=100, stop=95, risk_fraction=0.005, multiplier=0.5, caps=self._caps())
        self.assertEqual(size.quantity, 50)
        self.assertEqual(size.multiplier, 0.5)

    def test_stop_not_below_entry_is_refused(self):
        with self.assertRaises(SizingRefused) as ctx:
            compute_size(entry=100, stop=101, risk_fraction=0.005, multiplier=None, caps=self._caps())
        self.assertEqual(ctx.exception.code, "invalid_stop")

    def test_concentration_cap_binds_and_is_recorded(self):
        caps = self._caps(concentration_pct=0.10, existing_symbol_value=9_000.0)
        # headroom = 0.10*100k - 9000 = 1000 → 10 shares at $100, below the
        # 100-share risk size.
        size = compute_size(entry=100, stop=95, risk_fraction=0.005, multiplier=None, caps=caps)
        self.assertEqual(size.quantity, 10)
        self.assertIn("concentration_cap", size.binding)


class EvidenceClipTests(unittest.TestCase):
    def _assess(self, answer_id, mapping, **skw):
        return evidence.assess(
            ticker="AMD",
            cohort_answer_id=answer_id,
            expected_hold_sessions=5,
            on_date=pf.NOW.date(),
            settings=pf.settings(**skw),
            resolver=pf.resolver_for(mapping),
        )

    def test_m_is_lb_over_pe(self):
        a = pf.cited_answer(policy_net=0.10, policy_lb=0.04)
        result = self._assess("a", {"a": a})
        self.assertTrue(result.is_evidenced)
        self.assertAlmostEqual(result.multiplier, 0.4, places=6)

    def test_m_clips_at_one_when_lb_exceeds_pe(self):
        # A lower bound above the point estimate is degenerate but must not
        # produce a multiplier above 1 (Spec L §6.6: clip(LB/PE, 0, 1)).
        a = pf.cited_answer(policy_net=0.04, policy_lb=0.08)
        result = self._assess("a", {"a": a})
        self.assertEqual(result.multiplier, 1.0)

    def test_no_interval_is_discretionary_with_point_estimate_shown(self):
        # The Phase 3c gap: a real answer today carries net but no interval.
        a = pf.cited_answer(policy_net=0.08, policy_lb=None)
        result = self._assess("a", {"a": a})
        self.assertFalse(result.is_evidenced)
        self.assertEqual(result.reason_code, evidence.NO_POLICY_BOUND)
        self.assertEqual(result.point_estimate, 0.08)
        self.assertIsNone(result.lower_bound)

    def test_uncited_is_discretionary_in_advisory_and_refused_in_strict(self):
        advisory = self._assess("", {})
        self.assertFalse(advisory.is_evidenced)
        self.assertFalse(advisory.refused)
        strict = self._assess("", {}, evidence_gate_mode="strict")
        self.assertTrue(strict.refused)


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.settings = pf.settings()
        self.now = datetime(2026, 9, 9, 14, 0, 0)

    def _proposal(self, ref):
        return SimpleNamespace(
            proposal_uid="uid-1",
            approval_signature=ref.signature,
            approval_nonce=ref.nonce,
            approval_expires_at=ref.expires_at,
            approval_owner_id=ref.owner_id,
            approval_consumed_at=None,
        )

    def test_mint_and_verify_round_trip(self):
        ref = approvals.mint(
            proposal_id=1, proposal_uid="uid-1", owner_id="owner", now=self.now, settings=self.settings
        )
        proposal = self._proposal(ref)
        # No exception: the full signature verifies.
        approvals.verify(
            proposal, presented_signature=ref.prefix, owner_id="owner", now=self.now, settings=self.settings
        )

    def test_a_consumed_reference_is_refused(self):
        ref = approvals.mint(
            proposal_id=1, proposal_uid="uid-1", owner_id="owner", now=self.now, settings=self.settings
        )
        proposal = self._proposal(ref)
        proposal.approval_consumed_at = self.now
        with self.assertRaises(approvals.ApprovalRefused) as ctx:
            approvals.verify(
                proposal, presented_signature=ref.prefix, owner_id="owner", now=self.now, settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "approval_already_used")

    def test_expiry_is_enforced(self):
        ref = approvals.mint(
            proposal_id=1, proposal_uid="uid-1", owner_id="owner", now=self.now, settings=self.settings
        )
        proposal = self._proposal(ref)
        late = self.now + timedelta(seconds=self.settings.approval_ttl_seconds + 1)
        with self.assertRaises(approvals.ApprovalRefused) as ctx:
            approvals.verify(
                proposal, presented_signature=ref.prefix, owner_id="owner", now=late, settings=self.settings
            )
        self.assertEqual(ctx.exception.code, "approval_expired")

    def test_no_secret_means_nothing_can_be_minted(self):
        with self.assertRaises(approvals.ApprovalRefused) as ctx:
            approvals.mint(
                proposal_id=1,
                proposal_uid="uid-1",
                owner_id="owner",
                now=self.now,
                settings=pf.settings(execution_approval_secret=""),
            )
        self.assertEqual(ctx.exception.code, "approval_secret_missing")

    def test_callback_parsing(self):
        action, pid, sig = approvals.parse_callback("p6ok:12:abcdef")
        self.assertEqual((action, pid, sig), ("approve", 12, "abcdef"))
        action, pid, _ = approvals.parse_callback("p6no:7:zzz")
        self.assertEqual((action, pid), ("reject", 7))
        with self.assertRaises(approvals.ApprovalRefused):
            approvals.parse_callback("nonsense")


if __name__ == "__main__":
    unittest.main()
