"""`SetupSpec` is typed, frozen, content-hashed, and family-keyed (Spec N §4.0)."""

from __future__ import annotations

import dataclasses
import unittest

from comparables.inference import TrialRegistry
from comparables.setup_spec import Condition, SetupSpec, SetupSpecError


def spec(**overrides) -> SetupSpec:
    base = dict(
        slug="earnings_sue_gap_v1",
        version="1.0.0",
        conditions=(Condition("sue_seasonal", ">", 1.5),
                    Condition("gap_pct", ">", 3.0)),
        universe="liquid_us_equity_v1",
        horizons_sessions=(1, 5, 10),
        execution_policy="event_swing_14cal_v1",
        match_covariates=("market_cap_decile", "liquidity_decile"),
        lookback_years=5,
    )
    base.update(overrides)
    return SetupSpec(**base)


class SetupSpecTests(unittest.TestCase):
    def test_setup_spec_is_hashed_and_immutable(self):
        a, b = spec(), spec()
        self.assertEqual(a, b)
        self.assertEqual(a.content_hash, b.content_hash)
        self.assertEqual(len(a.content_hash), 64)

        # The hash is a cache key: the same spec answers identically.
        cache = {a.content_hash: "answer"}
        self.assertEqual(cache[b.content_hash], "answer")

        # Changing any condition creates a new setup, not a mutated one.
        moved = spec(conditions=(Condition("sue_seasonal", ">", 2.0),
                                 Condition("gap_pct", ">", 3.0)))
        self.assertNotEqual(moved.content_hash, a.content_hash)
        for field, value in (("version", "1.0.1"), ("universe", "other_v1"),
                             ("horizons_sessions", (1, 5)), ("lookback_years", 7),
                             ("execution_policy", None)):
            self.assertNotEqual(spec(**{field: value}).content_hash, a.content_hash,
                                f"{field} must be part of the hash")

        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.slug = "renamed"
        with self.assertRaises(dataclasses.FrozenInstanceError):
            a.conditions[0].value = 9.0

    def test_every_field_is_required_and_typed(self):
        with self.assertRaises(SetupSpecError):
            spec(conditions=())
        with self.assertRaises(SetupSpecError):
            spec(horizons_sessions=())
        with self.assertRaises(SetupSpecError):
            spec(horizons_sessions=(5, 1))          # must be ascending
        with self.assertRaises(SetupSpecError):
            spec(horizons_sessions=(1, 1, 5))       # must not repeat
        with self.assertRaises(SetupSpecError):
            spec(horizons_sessions=(0,))
        with self.assertRaises(SetupSpecError):
            spec(lookback_years=0)
        with self.assertRaises(SetupSpecError):
            Condition("whatever_i_felt_like", ">", 1.0)
        with self.assertRaises(SetupSpecError):
            Condition("gap_pct", "roughly", 1.0)

    def test_horizons_are_labelled_sessions(self):
        self.assertEqual(spec().horizons_sessions, (1, 5, 10))
        self.assertTrue(
            any("sessions" in f.name for f in dataclasses.fields(SetupSpec)),
            "the unit lives in the field name (Spec N §4.0)",
        )

    def test_trial_count_keyed_by_family(self):
        registry = TrialRegistry()
        original = spec()
        self.assertEqual(registry.record(original), 1)

        renamed = original.renamed("earnings_sue_gap_but_prettier")
        self.assertEqual(renamed.family_slug, original.family_slug)
        self.assertNotEqual(renamed.content_hash, original.content_hash)

        self.assertEqual(registry.record(renamed), 2)
        self.assertEqual(registry.trials(original), 2)
        self.assertEqual(registry.trials(renamed), 2)

        # Re-running the same spec is a query, not a new variant.
        self.assertEqual(registry.record(original), 2)
        self.assertEqual(registry.queries(original), 3)

        # A different primary condition is a different family, counted apart.
        other = spec(conditions=(Condition("gap_pct", ">", 5.0),))
        self.assertNotEqual(other.family_slug, original.family_slug)
        self.assertEqual(registry.record(other), 1)
        self.assertEqual(registry.trials(original), 2)

        # The twelfth variant in a family reports twelve.
        family = TrialRegistry()
        for i in range(12):
            count = family.record(spec(conditions=(
                Condition("sue_seasonal", ">", 1.0 + i / 10.0),)))
        self.assertEqual(count, 12)


if __name__ == "__main__":
    unittest.main()
