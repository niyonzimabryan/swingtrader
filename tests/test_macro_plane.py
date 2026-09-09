"""Phase 4 — the macro plane: ALFRED vintages and ``regime_v1``.

The Spec O section 6 tests this file is responsible for:

``test_macro_vintage_absent_before_release``
``test_macro_vintage_differs_from_current``
``test_usrec_only_via_vintage``
``test_regime_classifier_versioned``
``test_regime_v1_inputs_unrevised``
``test_no_llm_in_regime``

Everything runs offline against ``tests/fixtures/macro/``. FRED is unreachable
from the build environment (see the fixture README), so the vintages are
synthetic — built to ALFRED's shape, with a real release lag and real
restatements, which is the structure the point-in-time rules act on.
"""

from __future__ import annotations

import ast
import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from database.db import get_session_factory
from filings.observations import PRECISION_DAY, day_precision_known_at
from macro import regime_v1
from macro import series as series_registry
from macro.api import macro_state
from macro.vintage import (
    FixtureVintageSource,
    MacroPlaneDisabled,
    first_release_date_for,
    ingest_series,
    release_date_for,
    series_as_of,
    stored_series_as_of,
    vintage_observations,
)
from tests.dbfixture import init_test_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "macro"

REPO_ROOT = Path(__file__).resolve().parent.parent


def load_series(series_id: str) -> list[dict]:
    payload = json.loads((FIXTURES / f"alfred_{series_id}.json").read_text(encoding="utf-8"))
    return payload["rows"]


def fixture_source(*series_ids: str) -> FixtureVintageSource:
    ids = series_ids or ("CPIAUCSL", "USREC", "SP500", "VIXCLS", "DGS10", "DGS3MO", "DGS2")
    return FixtureVintageSource({series_id: load_series(series_id) for series_id in ids})


class EnabledSettings:
    plane_macro_vintage_enabled = True


class DisabledSettings:
    plane_macro_vintage_enabled = False


# ---------------------------------------------------------------------------
# Vintages
# ---------------------------------------------------------------------------


class VintageTests(unittest.TestCase):
    def setUp(self):
        self.source = fixture_source()

    def test_macro_vintage_absent_before_release(self):
        """A series unreleased at ``as_of`` is absent, not back-filled.

        Spec O section 6. This is the failure that makes every historical
        regime label wrong: February CPI does not exist on 2024-03-01, and a
        pipeline that returns *something* for it has invented a number nobody
        could have seen.
        """
        february = date(2024, 2, 1)
        released = release_date_for(
            self.source, "CPIAUCSL", february, as_of=date(2024, 12, 31)
        )
        self.assertIsNotNone(released)
        self.assertGreater(released, february, "a print lags its reference period")

        before = series_as_of(self.source, "CPIAUCSL", date(2024, 3, 1))
        self.assertNotIn(february, before, "February CPI is absent on 2024-03-01")
        self.assertIn(date(2024, 1, 1), before, "January CPI was out by then")

        after = series_as_of(self.source, "CPIAUCSL", released)
        self.assertIn(february, after)

    def test_macro_vintage_differs_from_current(self):
        """A revised series returns different values for a historical ``as_of``.

        Spec O section 6, and the definition-of-done item: "``macro_state(as_of=
        <past date>)`` demonstrably differs from the current print on a revised
        series." January 2024 CPI as it stood in February 2024 is the first
        estimate; today's value carries two revisions.
        """
        january = date(2024, 1, 1)
        first_release = first_release_date_for(self.source, "CPIAUCSL", january)
        self.assertEqual(first_release, date(2024, 2, 13), "a real release lag")

        contemporaneous = series_as_of(self.source, "CPIAUCSL", first_release)[january]
        current = series_as_of(self.source, "CPIAUCSL", date(2026, 9, 1))[january]

        self.assertNotEqual(
            contemporaneous,
            current,
            "the fixture must exercise a restatement or this test proves nothing",
        )
        self.assertGreater(abs(current - contemporaneous), 0.0)

        # And the never-revised control: same value at any vintage.
        day = date(2024, 3, 1)
        self.assertEqual(
            series_as_of(self.source, "DGS10", day)[day],
            series_as_of(self.source, "DGS10", date(2026, 9, 1))[day],
        )

    def test_usrec_only_via_vintage(self):
        """Requesting ``USREC`` without a vintage raises.

        Spec O section 6. NBER dates a turning point long after it happened,
        so the current series marks a recession starting on a date nobody knew
        it started — the single most seductive lookahead in macro data.
        """
        with self.assertRaises(series_registry.VintageRequired) as caught:
            series_registry.require_vintage("USREC")
        self.assertIn("vintage", str(caught.exception).lower())

        # Via a vintage it is answerable, and the answer changes with the date.
        mid_2024 = series_as_of(self.source, "USREC", date(2024, 6, 30))
        self.assertEqual(set(mid_2024.values()), {0.0}, "no recession declared yet")

        later = series_as_of(self.source, "USREC", date(2026, 1, 1))
        self.assertIn(1.0, set(later.values()), "the retrospective declaration")

        # A never-revised series is not vintage-gated.
        series_registry.require_vintage("DGS10")

    def test_unregistered_series_raises_rather_than_defaulting(self):
        with self.assertRaises(series_registry.SeriesNotRegistered):
            series_registry.spec_for("MADEUPSERIES")

    def test_every_observation_is_day_precision_at_the_close(self):
        """ALFRED vintages are dates. Known at the close, never at the open."""
        rows = vintage_observations(self.source, "CPIAUCSL")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(row.precision, PRECISION_DAY)
            self.assertEqual(row.known_at_source, "realtime_start")
            release = date.fromisoformat(row.payload["release_date"])
            self.assertEqual(row.known_at_utc, day_precision_known_at(release))
            self.assertTrue(row.replay_eligible)

    def test_a_restatement_is_a_new_row(self):
        rows = vintage_observations(self.source, "CPIAUCSL")
        january = [r for r in rows if r.payload["reference_date"] == "2024-01-01"]
        self.assertEqual(len(january), 3, "three releases, three rows")
        self.assertEqual(
            len({r.payload_hash() for r in january}), 3, "and three distinct hashes"
        )


class VintageLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("macro_plane")
        self.session = get_session_factory()()
        self.addCleanup(self.db.cleanup)
        self.addCleanup(self.session.close)
        self.source = fixture_source()

    def test_plane_flag_defaults_off(self):
        with self.assertRaises(MacroPlaneDisabled):
            ingest_series(self.session, self.source, ["DGS10"], settings=DisabledSettings())

    def test_ledger_read_reproduces_the_vintage_collapse(self):
        """Stored rows answer the same as the source, through one cutoff filter."""
        ingest_series(
            self.session, self.source, ["CPIAUCSL"], settings=EnabledSettings()
        )
        january = date(2024, 1, 1)
        first_release = first_release_date_for(self.source, "CPIAUCSL", january)
        cutoff = day_precision_known_at(first_release)

        stored = stored_series_as_of(self.session, "CPIAUCSL", as_of=cutoff)
        direct = series_as_of(self.source, "CPIAUCSL", first_release)
        self.assertEqual(stored[january], direct[january])

        later = stored_series_as_of(
            self.session, "CPIAUCSL", as_of=datetime(2026, 9, 1, tzinfo=timezone.utc)
        )
        self.assertNotEqual(later[january], stored[january])

    def test_ingest_is_idempotent(self):
        first = ingest_series(
            self.session, self.source, ["DGS10"], settings=EnabledSettings()
        )
        second = ingest_series(
            self.session, self.source, ["DGS10"], settings=EnabledSettings()
        )
        self.assertGreater(first.written, 0)
        self.assertEqual(second.written, 0)

    def test_ingest_logs_which_inputs_are_vintage_clean(self):
        """The checkpoint requirement: say which regime inputs are unrevised."""
        coverage = ingest_series(
            self.session, self.source, ["DGS10", "VIXCLS", "CPIAUCSL"],
            settings=EnabledSettings(),
        )
        self.assertEqual(sorted(coverage.vintage_clean_inputs), ["DGS10", "VIXCLS"])
        self.assertEqual(coverage.revised_inputs, ["CPIAUCSL"])

    def test_macro_state_absent_series_is_listed_not_zeroed(self):
        ingest_series(
            self.session, self.source, ["SP500", "VIXCLS", "DGS10", "DGS3MO"],
            settings=EnabledSettings(),
        )
        state = macro_state(self.session, as_of=date(2024, 6, 3))
        self.assertIn("DGS2", state["missing"])
        self.assertNotIn("DGS2", state["series"])
        self.assertIn("SP500", state["series"])

    def test_macro_state_at_a_past_date_differs_from_the_current_print(self):
        """The definition-of-done demonstration, through the public API."""
        ingest_series(
            self.session, self.source, ["CPIAUCSL"], settings=EnabledSettings()
        )
        january = date(2024, 1, 1)
        first_release = first_release_date_for(self.source, "CPIAUCSL", january)
        past = macro_state(
            self.session, as_of=first_release, series_ids=["CPIAUCSL"], include_regime=False
        )
        now = macro_state(
            self.session, as_of=date(2026, 9, 1), series_ids=["CPIAUCSL"],
            include_regime=False,
        )
        self.assertEqual(past["series"]["CPIAUCSL"]["reference_date"], january.isoformat())
        self.assertNotEqual(
            past["series"]["CPIAUCSL"]["value"],
            [
                # The current print for the *same reference period*.
                stored_series_as_of(
                    self.session, "CPIAUCSL", as_of=datetime(2026, 9, 1, tzinfo=timezone.utc)
                )[january]
            ][0],
        )
        self.assertIn("provenance", now)
        self.assertIn("data_quality", now["provenance"])


# ---------------------------------------------------------------------------
# regime_v1
# ---------------------------------------------------------------------------


class RegimeTests(unittest.TestCase):
    def test_regime_classifier_versioned(self):
        """Changing a threshold requires a new version; history is not relabelled.

        Spec O section 6. The pinned fingerprint is the mechanism: edit a
        threshold and this fails, with a message saying to write ``regime_v2``
        rather than to edit the literal.
        """
        regime_v1.assert_thresholds_unchanged()
        self.assertEqual(regime_v1.VERSION, "regime_v1")
        self.assertEqual(
            regime_v1.thresholds_fingerprint(), regime_v1.FROZEN_THRESHOLD_FINGERPRINT
        )

        moved = regime_v1.Thresholds(crisis_vix=28.0)
        self.assertNotEqual(
            regime_v1.thresholds_fingerprint(moved),
            regime_v1.FROZEN_THRESHOLD_FINGERPRINT,
            "a changed threshold must change the fingerprint",
        )

        # And the label travels with the version that produced it, so a stored
        # label can never be silently reinterpreted under new thresholds.
        label = regime_v1.classify(
            regime_v1.build_inputs(as_of=date(2024, 6, 3), index_closes=[100.0] * 300,
                                   vix=12.0, dgs10=4.2, dgs3mo=4.0)
        )
        self.assertEqual(label.version, "regime_v1")
        self.assertEqual(
            label.thresholds_fingerprint, regime_v1.FROZEN_THRESHOLD_FINGERPRINT
        )

    def test_regime_v1_inputs_unrevised(self):
        """Every ``regime_v1`` input series is on the never-revised allowlist.

        Spec O section 6. Without this, the classifier's own inputs restate and
        yesterday's label changes — the property the deterministic design was
        chosen for.
        """
        ok, offenders = regime_v1.input_series_are_unrevised()
        self.assertTrue(ok, f"revised series in regime_v1 inputs: {offenders}")
        for series_id in regime_v1.INPUT_SERIES:
            self.assertTrue(series_registry.is_never_revised(series_id), series_id)
            self.assertFalse(series_registry.spec_for(series_id).vintage_only)

        for revised in ("USREC", "CPIAUCSL", "GDPC1", "PAYEMS", "BAMLC0A0CM"):
            self.assertNotIn(revised, regime_v1.INPUT_SERIES)

    def test_no_llm_in_regime(self):
        """Import-graph: ``macro/regime_v1.py`` reaches no model client.

        Spec O section 6. "A model may narrate the regime; it may not assign
        it" is only true if it is structural, so this walks the import graph
        rather than trusting the docstring.
        """
        forbidden_modules = {
            "anthropic", "openai", "google", "langfuse", "firecrawl", "finnhub",
            "alpaca", "alpaca_trade_api", "yfinance", "telegram", "httpx", "requests",
        }
        forbidden_first_party = {
            "agents", "bot", "orchestrator", "tools", "execution", "scanning",
            "screening", "scoring", "memo", "evals", "data", "news",
        }

        reached = _transitive_first_party_imports(REPO_ROOT / "macro" / "regime_v1.py")
        for module, importer in reached.items():
            root = module.split(".")[0]
            self.assertNotIn(
                root, forbidden_modules, f"{importer} imports {module}"
            )
            self.assertNotIn(
                root, forbidden_first_party, f"{importer} imports {module}"
            )
        # Positively: the only first-party package it reaches is macro itself.
        first_party = {
            module.split(".")[0]
            for module in reached
            if (REPO_ROOT / module.split(".")[0]).is_dir()
        }
        self.assertLessEqual(first_party, {"macro"})


def _module_path(module: str) -> Path | None:
    candidate = REPO_ROOT / Path(*module.split(".")).with_suffix(".py")
    if candidate.exists():
        return candidate
    package = REPO_ROOT / Path(*module.split(".")) / "__init__.py"
    return package if package.exists() else None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            out.add(node.module)
    return out


def _transitive_first_party_imports(entry: Path) -> dict:
    """Every module reachable from ``entry``, following first-party files only."""
    reached: dict[str, str] = {}
    stack = [(entry, entry.relative_to(REPO_ROOT).as_posix())]
    seen = {entry}
    while stack:
        path, importer = stack.pop()
        for module in sorted(_imports(path)):
            reached.setdefault(module, importer)
            target = _module_path(module)
            if target is not None and target not in seen:
                seen.add(target)
                stack.append((target, module))
    return reached


class RegimeLabelTests(unittest.TestCase):
    """The label set, exercised through the rules in the order they fire."""

    def inputs(self, **kwargs):
        base = dict(
            as_of=date(2024, 6, 3),
            index_closes=[100.0 + i * 0.05 for i in range(300)],
            vix=13.0,
            dgs10=4.4,
            dgs3mo=4.1,
        )
        base.update(kwargs)
        return regime_v1.build_inputs(**base)

    def test_expansion(self):
        self.assertEqual(regime_v1.classify(self.inputs()).label, regime_v1.LABEL_EXPANSION)

    def test_late_cycle_when_the_curve_inverts(self):
        label = regime_v1.classify(self.inputs(dgs10=4.0, dgs3mo=5.3))
        self.assertEqual(label.label, regime_v1.LABEL_LATE_CYCLE)

    def test_vix_beats_direction(self):
        """A rising market during a volatility shock is not ``expansion``."""
        self.assertEqual(regime_v1.classify(self.inputs(vix=24.0)).label, regime_v1.LABEL_STRESS)
        self.assertEqual(regime_v1.classify(self.inputs(vix=34.0)).label, regime_v1.LABEL_CRISIS)

    def test_drawdown_thresholds(self):
        rising = [100.0 + i * 0.05 for i in range(280)]
        mild = regime_v1.classify(self.inputs(index_closes=rising + [100.0] * 20))
        self.assertIn(mild.label, {regime_v1.LABEL_STRESS, regime_v1.LABEL_CONTRACTION})

        crash = rising + [80.0] * 5
        self.assertEqual(
            regime_v1.classify(self.inputs(index_closes=crash)).label,
            regime_v1.LABEL_CRISIS,
        )

    def test_contraction_below_trend(self):
        falling = [200.0 - i * 0.3 for i in range(260)]
        label = regime_v1.classify(
            self.inputs(index_closes=falling, vix=15.0)
        )
        self.assertIn(
            label.label, {regime_v1.LABEL_CONTRACTION, regime_v1.LABEL_STRESS,
                          regime_v1.LABEL_CRISIS}
        )

    def test_missing_inputs_are_insufficient_not_guessed(self):
        """A regime guessed from a missing VIX is wrong exactly when it matters."""
        label = regime_v1.classify(self.inputs(index_closes=[], vix=None))
        self.assertEqual(label.label, regime_v1.LABEL_INSUFFICIENT)
        self.assertIn("SP500", label.inputs.missing)

        no_curve = regime_v1.classify(self.inputs(dgs10=None, dgs3mo=None))
        self.assertEqual(no_curve.label, regime_v1.LABEL_INSUFFICIENT)

    def test_classification_is_reproducible(self):
        """Same inputs, same label — the property a fitted model cannot offer."""
        inputs = self.inputs()
        first = regime_v1.classify(inputs)
        second = regime_v1.classify(inputs)
        self.assertEqual(first.as_dict(), second.as_dict())

    def test_realized_volatility_is_population_stdev(self):
        closes = [100.0 * (1.01 if i % 2 else 0.99) for i in range(60)]
        value = regime_v1.realized_volatility_pct(
            closes, window=20, sessions_per_year=252
        )
        self.assertIsNotNone(value)
        self.assertGreater(value, 0.0)
        self.assertIsNone(
            regime_v1.realized_volatility_pct(closes[:5], window=20, sessions_per_year=252),
            "too few sessions is None, not a number computed from five days",
        )


class RegimeFromStoredVintagesTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("macro_regime")
        self.session = get_session_factory()()
        self.addCleanup(self.db.cleanup)
        self.addCleanup(self.session.close)
        ingest_series(
            self.session,
            fixture_source(),
            list(regime_v1.INPUT_SERIES),
            settings=EnabledSettings(),
        )

    def test_regime_is_reproducible_from_stored_vintages(self):
        """Spec O section 7: labels reproducible from stored vintages."""
        state = macro_state(self.session, as_of=date(2025, 3, 3))
        self.assertIsNotNone(state["regime"])
        self.assertEqual(state["regime"]["version"], "regime_v1")
        again = macro_state(self.session, as_of=date(2025, 3, 3))
        self.assertEqual(state["regime"], again["regime"])

    def test_the_label_changes_with_the_environment_not_the_run_date(self):
        calm = macro_state(self.session, as_of=date(2025, 3, 3))["regime"]
        stressed = macro_state(self.session, as_of=date(2025, 10, 1))["regime"]
        self.assertNotEqual(calm["label"], stressed["label"])
        self.assertIn(
            stressed["label"], {regime_v1.LABEL_STRESS, regime_v1.LABEL_CRISIS}
        )
        # Re-asking for the earlier date still gives the earlier label: the
        # later data does not reach back.
        self.assertEqual(
            macro_state(self.session, as_of=date(2025, 3, 3))["regime"]["label"],
            calm["label"],
        )


if __name__ == "__main__":
    unittest.main()
