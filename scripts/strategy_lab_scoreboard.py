"""The Strategy Lab scorecard CLI: JSON and Markdown, byte-identical per input.

```bash
python -m scripts.strategy_lab_scoreboard \
    --database-url sqlite:///swing_trader.db \
    --experiment q1_2026_roster \
    --cutoff 2026-06-30T21:00:00 \
    --json artifacts/scoreboard.json \
    --markdown artifacts/scoreboard.md
```

Two things live here and nowhere else.

**The bridge to Spec N's inference layer.** ``strategy_lab`` may not import
``comparables`` (``tests/test_strategy_lab_import_graph.py``), and Spec N's
``comparables/inference.py`` is this repository's one implementation of the
stationary block bootstrap, the effective sample size and Romano–Wolf step-M.
So ``strategy_lab/metrics.py`` declares two protocols and this module implements
them over ``comparables.inference``. Nothing is reimplemented: every interval,
every ``n_eff`` and every family-wise decision on the card is a call into that
module, seeded, with the seed printed.

**Determinism.** The same database, cutoff and options produce byte-identical
files. Every collection is sorted, every float goes through one formatter, the
bootstrap seed is an explicit option, and the only clock the artifact contains
is the evaluation cutoff the caller passed. A scorecard that changed because it
was generated twice would be worthless as evidence.

The card separates clean replay from ``archival_reconstructed`` results into two
sections that are never combined, prints every floor and gate beside the
numbers, and refuses to name a winner unless every gate in
``metrics.RankingGate`` clears. Today
``strategy_lab.snapshot_builder.REPLAY_ELIGIBLE_PRICE_SOURCES`` is empty, so
snapshots built from stored bars are ``archival_reconstructed`` and land in the
exploratory section — the header says so rather than the reader having to
notice.

This CLI reads. It places no order, writes no experiment row, and performs no
promotion: promotion is owner-only (Spec Q §3, §8) and this is the evidence a
promotion would be read against.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from typing import Mapping, Sequence

from comparables import config as comparables_config
from comparables import inference
from strategy_lab import metrics, registry, runner
from strategy_lab.domain import ExecutionState
from strategy_lab.replay import EVIDENCE_CLEAN, EVIDENCE_EXPLORATORY, CostAssumptions

SCHEMA = "strategy_lab.scoreboard.v1"

#: How many decimal places every float in the artifact is written to. One
#: formatter, applied once, is what makes two runs byte-identical rather than
#: identical-looking.
PRECISION = 6


# --------------------------------------------------------------------------- #
# The inference backends (Spec N §6, §7)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ComparablesUncertainty:
    """``metrics.UncertaintyBackend`` over ``comparables.inference``.

    The block length is Politis–White's estimate floored at the horizon, the
    interval is the Politis–Romano stationary bootstrap, and ``n_eff`` is the
    design-effect correction for events that cluster on the same date. All three
    are Spec N §6's, called rather than copied.
    """

    reps: int = comparables_config.BOOTSTRAP_REPS
    seed: int = comparables_config.DEFAULT_SEED

    def interval(
        self,
        series: Sequence[float],
        event_dates: Sequence[date],
        *,
        horizon: int,
        level: float,
    ) -> metrics.Interval:
        values = list(series)
        block = inference.optimal_block_length_for(values, horizon)
        ci = inference.stationary_bootstrap_ci(
            values, block.used, reps=self.reps, seed=self.seed, level=level
        )
        ess = inference.effective_sample_size(values, list(event_dates))
        return metrics.Interval(
            estimate=ci.estimate,
            lower=ci.lower,
            upper=ci.upper,
            level=ci.level,
            method=ci.method,
            block_length=ci.block_length,
            reps=ci.reps,
            seed=ci.seed,
            n_eff=ess.n_eff,
        )


@dataclass(frozen=True)
class ComparablesMultiplicity:
    """``metrics.MultiplicityBackend`` over ``comparables.inference``.

    :meth:`family_level` is the Šidák correction written for a *confidence
    level* rather than a p-value: to hold family-wise coverage at ``level``
    across ``m`` intervals, each interval is taken at ``level ** (1 / m)``. That
    is the exact inverse of ``inference.sidak_adjusted``, and
    ``tests/test_strategy_lab_metrics.py`` asserts the round trip against that
    function rather than trusting the algebra here.

    :meth:`stepm` is ``inference.romano_wolf_stepm``: the family-wise control
    across every arm's series, which is the diagnostic Spec Q §10 asks for when
    the sample supports it. It returns ``(False, note)`` — not an error — when
    the family is too small or its series have different lengths, and the note
    is printed on the card.
    """

    size: float = 0.05
    reps: int = 1000
    seed: int = comparables_config.DEFAULT_SEED

    def family_level(self, level: float, n_trials: int) -> float:
        if not 0 < level < 1:
            raise ValueError("level is a probability in (0, 1)")
        if n_trials < 1:
            raise ValueError("n_trials must be >= 1")
        return float(level ** (1.0 / n_trials))

    def stepm(
        self, family_series: Mapping[str, Sequence[float]], target: str
    ) -> tuple[bool, str]:
        return inference.romano_wolf_stepm(
            {key: list(value) for key, value in family_series.items()},
            target,
            size=self.size,
            reps=self.reps,
            seed=self.seed,
        )


# --------------------------------------------------------------------------- #
# Reading the experiment back out of the database
# --------------------------------------------------------------------------- #


#: A closed execution is a matured observation; a protected one is still open.
_CLOSED = ExecutionState.CLOSED.value
_OPEN_STATES = frozenset({
    ExecutionState.PROTECTED.value,
    ExecutionState.PROTECTION_PENDING.value,
    ExecutionState.FILLED.value,
    ExecutionState.CLOSING.value,
})

WARN_ZERO_NOTIONAL = "zero_notional_execution_unevaluable"


def observations_for_arm(
    session, arm_id: int, arm_label: str
) -> tuple[tuple[metrics.TradeObservation, ...], tuple[str, ...]]:
    """Rebuild one arm's observations from its ``strategy_trades`` rows.

    Percentages come back out of the stored dollars exactly:
    ``net_pct = 100 x realized_pnl / notional`` and
    ``gross_pct = 100 x (realized_pnl + costs) / notional``, which is the
    identity ``shadow.settle`` wrote them with. A row with no notional carries
    no percentage and is reported as unevaluable rather than counted as zero.
    """
    observations: list[metrics.TradeObservation] = []
    warnings: set[str] = set()
    for row in registry.executions_for_arm(session, arm_id):
        if row.status not in _OPEN_STATES and row.status != _CLOSED:
            continue
        decision = registry.decision_row(session, row.decision_id)
        snapshot = registry.snapshot_row(session, decision.snapshot_id)
        quality = json.loads(snapshot.data_quality_json or "{}")
        evidence = (
            EVIDENCE_CLEAN if quality.get("replay_eligible") else EVIDENCE_EXPLORATORY
        )
        notional = float(row.notional or 0.0)
        if notional <= 0:
            warnings.add(WARN_ZERO_NOTIONAL)
            continue
        matured = row.status == _CLOSED
        entry_day = (row.filled_at or row.created_at).date()
        exit_day = (row.closed_at or row.filled_at or row.created_at).date()
        net_pct = gross_pct = None
        if matured and row.realized_pnl is not None and row.costs is not None:
            net_pct = 100.0 * float(row.realized_pnl) / notional
            gross_pct = 100.0 * (float(row.realized_pnl) + float(row.costs)) / notional
        risk_pct = _risk_pct(row)
        observations.append(metrics.TradeObservation(
            arm=arm_label,
            ticker=decision.ticker,
            entry_date=entry_day,
            exit_date=exit_day,
            holding_days=max(0, (exit_day - entry_day).days),
            gross_pct=gross_pct if gross_pct is not None else 0.0,
            matured=matured,
            evidence_class=evidence,
            rule_fired=row.exit_reason or "",
            net_pct=net_pct,
            r_multiple=(
                net_pct / risk_pct if net_pct is not None and risk_pct else None
            ),
            costs=float(row.costs) if row.costs is not None else None,
            notional=notional,
        ))
    observations.sort(key=lambda o: (o.entry_date, o.ticker))
    return tuple(observations), tuple(sorted(warnings))


def _risk_pct(row) -> float | None:
    entry = float(row.intended_entry_price or 0.0)
    stop = float(row.stop_price or 0.0)
    if entry <= 0 or stop <= 0 or stop >= entry:
        return None
    return (entry - stop) / entry * 100.0


# --------------------------------------------------------------------------- #
# Assembling the card
# --------------------------------------------------------------------------- #


def _round(value):
    """One formatter for every float in the artifact."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float):
        return round(value, PRECISION)
    if isinstance(value, dict):
        return {key: _round(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_round(item) for item in value]
    return value


@dataclass(frozen=True)
class ScoreboardInputs:
    """Everything a card is a pure function of."""

    experiment: str
    cutoff_utc: datetime
    floors: metrics.EvidenceFloors
    gate: metrics.RankingGate
    costs: CostAssumptions | None
    primary_metric: str = "mean_net_pct"
    horizon_days: int = 1

    def canonical(self) -> dict:
        return {
            "experiment": self.experiment,
            "cutoff_utc": self.cutoff_utc.isoformat(),
            "primary_metric": self.primary_metric,
            "horizon_days": self.horizon_days,
            "floors": self.floors.canonical(),
            "gate": self.gate.canonical(),
            "costs": self.costs.canonical() if self.costs else None,
        }


def build_scorecard(
    inputs: ScoreboardInputs,
    by_arm: Mapping[str, Sequence[metrics.TradeObservation]],
    ledger: runner.VariantLedger,
    *,
    benchmarks: Mapping[str, metrics.Benchmark] | None = None,
    uncertainty: metrics.UncertaintyBackend | None = None,
    multiplicity: metrics.MultiplicityBackend | None = None,
    refusals: Sequence[tuple[str, str]] = (),
    read_warnings: Sequence[str] = (),
) -> dict:
    """The card as a plain dict. Pure: no database, no clock, no filesystem.

    Clean and exploratory observations are split first and scored separately, so
    the two sections cannot share a statistic however the caller assembled the
    input (Spec Q §10).
    """
    benchmarks = dict(benchmarks or {})
    sections: dict[str, dict] = {}

    for evidence in (EVIDENCE_CLEAN, EVIDENCE_EXPLORATORY):
        subset = {
            arm: tuple(o for o in obs if o.evidence_class == evidence)
            for arm, obs in sorted(by_arm.items())
        }
        subset = {arm: obs for arm, obs in subset.items() if obs}
        if not subset:
            continue
        rows = [
            metrics.evaluate_arm(
                arm,
                obs,
                floors=inputs.floors,
                costs=inputs.costs,
                benchmark=benchmarks.get(arm) or benchmarks.get("*"),
                uncertainty=uncertainty,
                multiplicity=multiplicity,
                n_trials=ledger.n_trials,
                level=inputs.gate.confidence_level,
                horizon_days=inputs.horizon_days,
            )
            for arm, obs in sorted(subset.items())
        ]
        board = metrics.rank_arms(
            rows,
            by_arm=subset,
            primary_metric=inputs.primary_metric,
            gate=inputs.gate,
            floors=inputs.floors,
            n_trials=ledger.n_trials,
            variants_declared=ledger.planned_variants,
            multiplicity=multiplicity,
        )
        sections[evidence] = board.canonical()

    warnings = set(read_warnings)
    warnings.update(metrics.variant_warnings(ledger.planned_variants, ledger.n_tried))
    if EVIDENCE_EXPLORATORY in sections:
        warnings.add(metrics.WARN_EXPLORATORY)

    payload = {
        "schema": SCHEMA,
        "inputs": inputs.canonical(),
        "variants": ledger.canonical(),
        "sections": sections,
        "refusals": [
            {"arm": arm, "reason": reason} for arm, reason in sorted(refusals)
        ],
        "warnings": sorted(warnings),
        "notes": [
            "Every number here is computed by code (comparables/inference.py for "
            "the bootstrap, the effective sample size and the family-wise test); "
            "no model produced, adjusted or characterised any of them.",
            "archival_reconstructed results are exploratory. They can never enter "
            "clean metrics, rank a winner, or satisfy a promotion gate (Spec Q §10).",
            "Promotion is owner-only. A cleared gate is a recommendation to look, "
            "not an authorisation (Spec Q §3, §8).",
        ],
    }
    return _round(payload)


def to_json(payload: Mapping) -> str:
    """Sorted keys, fixed indent, trailing newline. Byte-stable by construction."""
    return json.dumps(payload, sort_keys=True, indent=2, default=str) + "\n"


# --------------------------------------------------------------------------- #
# Markdown
# --------------------------------------------------------------------------- #


def _cell(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


_COLUMNS = (
    ("arm", "Arm"),
    ("status", "Status"),
    ("n_matured", "n matured"),
    ("n_open", "n open"),
    ("n_distinct_dates", "distinct dates"),
    ("mean_net_pct", "mean net %"),
    ("median_net_pct", "median net %"),
    ("mean_r", "mean R"),
    ("win_rate", "win rate"),
    ("profit_factor", "profit factor"),
    ("max_drawdown_pct", "max DD %"),
    ("time_under_water_days", "TUW days"),
    ("exposure_positions_per_day", "exposure"),
    ("turnover_trades_per_year", "turnover/yr"),
    ("benchmark_relative_pct", "vs benchmark pp"),
)


def render_markdown(payload: Mapping) -> str:
    """The same card as prose. Pure, and derived only from ``payload``."""
    inputs = payload["inputs"]
    lines: list[str] = [
        f"# Strategy Lab scoreboard — {inputs['experiment']}",
        "",
        f"Evaluation cutoff (UTC): `{inputs['cutoff_utc']}`  ",
        f"Primary metric: `{inputs['primary_metric']}`  ",
        f"Floors: `{json.dumps(inputs['floors'], sort_keys=True)}`  ",
        f"Cost assumptions: `{json.dumps(inputs['costs'], sort_keys=True)}`",
        "",
    ]

    variants = payload["variants"]
    lines += [
        "## Variants tried",
        "",
        f"Pre-registered: **{variants['planned_variants']}**. "
        f"Actually run: **{variants['n_tried']}**. "
        f"Multiple-testing denominator: **{variants['n_trials']}**.",
        "",
    ]
    for identity, mode in variants["variants_tried"]:
        lines.append(f"- `{identity}` / `{mode}`")
    lines.append("")

    for evidence, title in (
        (EVIDENCE_CLEAN, "Clean replay / forward shadow"),
        (EVIDENCE_EXPLORATORY, "Exploratory — archival_reconstructed"),
    ):
        section = payload["sections"].get(evidence)
        if not section:
            continue
        lines += [f"## {title}", ""]
        if evidence == EVIDENCE_EXPLORATORY:
            lines += [
                "> These results are reconstructed from data with no availability "
                "or revision provenance. They are shown because hiding them would "
                "be worse, and they can never enter clean metrics, rank a winner, "
                "or satisfy a promotion gate (Spec Q §10).",
                "",
            ]
        lines.append("| " + " | ".join(label for _, label in _COLUMNS) + " |")
        lines.append("|" + "---|" * len(_COLUMNS))
        for row in section["arms"]:
            lines.append(
                "| " + " | ".join(_cell(row.get(key)) for key, _ in _COLUMNS) + " |"
            )
        lines.append("")

        lines += ["### Uncertainty", ""]
        for row in section["arms"]:
            interval = row.get("uncertainty")
            if not interval:
                lines.append(
                    f"- `{row['arm']}`: no interval "
                    f"({', '.join(row['warnings']) or 'not computed'})"
                )
                continue
            lines.append(
                f"- `{row['arm']}`: {interval['estimate']:.4f} "
                f"[{interval['lower']:.4f}, {interval['upper']:.4f}] at "
                f"{interval['level']:.2f} ({interval['method']}, block="
                f"{interval['block_length']}, reps={interval['reps']}, "
                f"seed={interval['seed']}, n_eff={interval['n_eff']:.2f}); "
                f"family-adjusted lower {_cell(row.get('adjusted_lower'))} at "
                f"level {_cell(row.get('adjusted_level'))} over "
                f"{row['n_trials']} trial(s); step-M "
                f"{_cell(row.get('stepm_rejected'))} — {row.get('stepm_note') or '—'}"
            )
        lines.append("")

        if section["overlaps"]:
            lines += ["### Overlap and correlation", ""]
            lines.append("| Pair | shared | Jaccard | shared days | correlation | note |")
            lines.append("|---|---|---|---|---|---|")
            for pair in section["overlaps"]:
                lines.append(
                    f"| `{pair['left']}` vs `{pair['right']}` | {pair['n_shared']} "
                    f"| {_cell(pair['jaccard'])} | {pair['shared_exposure_days']} "
                    f"| {_cell(pair['correlation'])} | {pair['correlation_note']} |"
                )
            lines.append("")

        lines += [
            "### Verdict",
            "",
            f"**{section['label']}**"
            + (f" — winner: `{section['winner']}`" if section["winner"] else ""),
            "",
        ]
        for reason in section["reasons"]:
            lines.append(f"- {reason}")
        lines += [
            "",
            f"Ranking gate: `{json.dumps(section['gate'], sort_keys=True)}`",
            "",
        ]

    if payload["refusals"]:
        lines += ["## Arms that produced nothing", ""]
        for refusal in payload["refusals"]:
            lines.append(f"- `{refusal['arm']}`: {refusal['reason']}")
        lines.append("")

    if payload["warnings"]:
        lines += ["## Warnings", ""]
        for warning in payload["warnings"]:
            lines.append(f"- `{warning}`")
        lines.append("")

    lines += ["## Notes", ""]
    for note in payload["notes"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def collect(
    session, experiment: str
) -> tuple[dict[str, tuple[metrics.TradeObservation, ...]], list[str]]:
    """Every arm of an experiment, keyed by its label."""
    by_arm: dict[str, tuple[metrics.TradeObservation, ...]] = {}
    warnings: set[str] = set()
    for arm in registry.arms_for_experiment(session, experiment):
        version = registry.strategy_version_for_arm(session, arm.id)
        label = f"{version.slug}@{version.version}/{arm.mode}"
        observations, arm_warnings = observations_for_arm(session, arm.id, label)
        warnings.update(arm_warnings)
        by_arm[label] = observations
    return by_arm, sorted(warnings)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="strategy_lab_scoreboard",
        description=(
            "Render a Strategy Lab experiment's scoreboard as JSON and Markdown. "
            "Read-only: it places no order and performs no promotion."
        ),
    )
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--experiment", required=True)
    parser.add_argument(
        "--cutoff", required=True,
        help="Evaluation cutoff, ISO-8601 UTC (e.g. 2026-06-30T21:00:00).",
    )
    parser.add_argument("--json", dest="json_path", default="")
    parser.add_argument("--markdown", dest="markdown_path", default="")
    parser.add_argument("--slippage-bps", type=float, default=10.0)
    parser.add_argument("--half-spread-bps", type=float, default=5.0)
    parser.add_argument("--commission-bps", type=float, default=0.0)
    parser.add_argument("--floor-matured", type=int, default=100)
    parser.add_argument("--floor-distinct-dates", type=int, default=20)
    parser.add_argument("--floor-closed", type=int, default=30)
    parser.add_argument("--confidence-level", type=float, default=0.90)
    parser.add_argument("--horizon-days", type=int, default=1)
    parser.add_argument(
        "--reps", type=int, default=comparables_config.BOOTSTRAP_REPS,
        help="Bootstrap replications. Printed on the card.",
    )
    parser.add_argument(
        "--seed", type=int, default=comparables_config.DEFAULT_SEED,
        help="Bootstrap seed. Printed on the card; the same seed reproduces it.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    from database.db import get_session, init_db

    init_db(args.database_url)

    inputs = ScoreboardInputs(
        experiment=args.experiment,
        cutoff_utc=datetime.fromisoformat(args.cutoff),
        floors=metrics.EvidenceFloors(
            matured=args.floor_matured,
            distinct_dates=args.floor_distinct_dates,
            closed=args.floor_closed,
        ),
        gate=metrics.RankingGate(confidence_level=args.confidence_level),
        costs=CostAssumptions(
            slippage_bps=args.slippage_bps,
            half_spread_bps=args.half_spread_bps,
            commission_bps=args.commission_bps,
        ),
        horizon_days=args.horizon_days,
    )

    with get_session() as session:
        by_arm, warnings = collect(session, args.experiment)
        ledger = runner.variant_ledger(session, args.experiment)

    payload = build_scorecard(
        inputs,
        by_arm,
        ledger,
        uncertainty=ComparablesUncertainty(reps=args.reps, seed=args.seed),
        multiplicity=ComparablesMultiplicity(seed=args.seed),
        read_warnings=warnings,
    )
    document = to_json(payload)
    markdown = render_markdown(payload)

    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as handle:
            handle.write(document)
    if args.markdown_path:
        with open(args.markdown_path, "w", encoding="utf-8") as handle:
            handle.write(markdown)
    if not args.json_path and not args.markdown_path:
        sys.stdout.write(markdown)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
