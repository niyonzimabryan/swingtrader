"""The Strategy Lab scorecard card.

Built from ``scripts/strategy_lab_scoreboard.build_scorecard``'s payload and
from nothing else. That payload is pure — no clock, no database, every number
computed by ``comparables/inference.py`` — and this module's whole job is to
print it, which is why it has no arithmetic in it either (Spec Q §10,
AGENTS.md §1.2).

Three things are held apart because the payload holds them apart, and merging
them is exactly the dishonesty the split exists to prevent:

- **clean** and **exploratory** sections are separate tables, and the
  exploratory one carries its warning above the numbers rather than below.
- **warnings** and **refusals** are printed verbatim. Not summarised, not
  counted.
- a cleared gate is labelled a *recommendation to look*, never an authorisation.
  Promotion is owner-only (Spec Q §3, §8) and nothing in an email changes that.
"""

from __future__ import annotations

from notify.channel import KIND_SCORECARD

EVIDENCE_CLEAN = "clean"
EVIDENCE_EXPLORATORY = "exploratory"

#: The scoreboard's own column order, mirrored so the email, the page and
#: `scripts/strategy_lab_scoreboard.render_markdown` agree on what is shown.
COLUMNS = (
    ("arm", "arm"),
    ("status", "status"),
    ("n_matured", "n matured"),
    ("n_open", "n open"),
    ("mean_net_pct", "mean net %"),
    ("median_net_pct", "median net %"),
    ("mean_r", "mean R"),
    ("win_rate", "win rate"),
    ("profit_factor", "profit factor"),
    ("max_drawdown_pct", "max DD %"),
    ("benchmark_relative_pct", "vs bench pp"),
)


def _cell(value) -> str:
    """Print a value the payload already decided. Never arithmetic."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def _arm_rows(section: dict) -> list[list]:
    return [
        [_cell(arm.get(key)) for key, _ in COLUMNS]
        for arm in (section.get("arms") or [])
    ]


def _uncertainty_items(section: dict) -> list[str]:
    items = []
    for arm in section.get("arms") or []:
        interval = arm.get("uncertainty")
        if not interval:
            reasons = ", ".join(arm.get("warnings") or []) or "not computed"
            items.append(f"{arm.get('arm')}: no interval ({reasons})")
            continue
        items.append(
            f"{arm.get('arm')}: {_cell(interval.get('estimate'))} "
            f"[{_cell(interval.get('lower'))}, {_cell(interval.get('upper'))}] at "
            f"{_cell(interval.get('level'))} ({interval.get('method')}, "
            f"block={interval.get('block_length')}, reps={interval.get('reps')}, "
            f"seed={interval.get('seed')}, n_eff={_cell(interval.get('n_eff'))}); "
            f"family-adjusted lower {_cell(arm.get('adjusted_lower'))} over "
            f"{arm.get('n_trials')} trial(s); step-M "
            f"{_cell(arm.get('stepm_rejected'))} — {arm.get('stepm_note') or '—'}"
        )
    return items


def build_payload(
    *,
    scoreboard: dict,
    uid: str = "",
    ref: str = "",
    created_at_utc: str = "",
) -> dict:
    """The scorecard document. ``scoreboard`` is ``build_scorecard``'s dict."""
    scoreboard = dict(scoreboard or {})
    inputs = dict(scoreboard.get("inputs") or {})
    variants = dict(scoreboard.get("variants") or {})
    sections = dict(scoreboard.get("sections") or {})
    warnings = [str(warning) for warning in (scoreboard.get("warnings") or [])]

    experiment = str(inputs.get("experiment") or "")

    blocks: list[dict] = [
        {
            "type": "rows",
            "rows": [
                {"label": "cutoff (UTC)", "value": inputs.get("cutoff_utc") or "—"},
                {"label": "primary metric", "value": inputs.get("primary_metric") or "—"},
                {
                    "label": "variants",
                    "value": f"{variants.get('n_tried', 0)} run of "
                    f"{variants.get('planned_variants', 0)} declared",
                    "note": f"multiple-testing denominator: {variants.get('n_trials', 0)}",
                    "tone": "warn" if warnings else "",
                },
            ],
        }
    ]

    if not sections:
        blocks.append(
            {
                "type": "text",
                "body": "No scorecard yet: no experiment, or no settled shadow trade.",
                "muted": True,
            }
        )

    for evidence, title in (
        (EVIDENCE_CLEAN, "clean — replay / forward shadow"),
        (EVIDENCE_EXPLORATORY, "exploratory — archival_reconstructed"),
    ):
        section = sections.get(evidence)
        if not section:
            continue
        if evidence == EVIDENCE_EXPLORATORY:
            blocks.append(
                {
                    "type": "quote",
                    "title": title,
                    "body": (
                        "Reconstructed from data with no availability or revision "
                        "provenance. Shown because hiding it would be worse. It can "
                        "never enter clean metrics, rank a winner, or satisfy a "
                        "promotion gate (Spec Q §10)."
                    ),
                }
            )
            table_title = ""
        else:
            table_title = title
        blocks.append(
            {
                "type": "table",
                "title": table_title,
                "columns": [label for _, label in COLUMNS],
                "rows": _arm_rows(section),
            }
        )
        uncertainty = _uncertainty_items(section)
        if uncertainty:
            blocks.append(
                {
                    "type": "list",
                    "title": f"{evidence} — uncertainty",
                    "items": uncertainty,
                    "page_only": True,
                }
            )
        leader = section.get("leader") or {}
        if leader:
            blocks.append(
                {
                    "type": "rows",
                    "title": f"{evidence} — leader",
                    "rows": [
                        {"label": key, "value": _cell(value)}
                        for key, value in sorted(leader.items())
                        if not isinstance(value, (dict, list))
                    ],
                    "page_only": True,
                }
            )

    if warnings:
        blocks.append({"type": "list", "title": "warnings (verbatim)", "items": warnings})

    refusals = [
        f"{item.get('arm')}: {item.get('reason')}"
        for item in (scoreboard.get("refusals") or [])
        if isinstance(item, dict)
    ]
    if refusals:
        blocks.append(
            {"type": "list", "title": "refusals (verbatim)", "items": refusals, "page_only": True}
        )

    notes = [str(note) for note in (scoreboard.get("notes") or [])]
    if notes:
        blocks.append({"type": "list", "title": "notes", "items": notes, "page_only": True})

    return {
        "version": 1,
        "kind": KIND_SCORECARD,
        "uid": uid,
        "ref": ref or f"scorecard:{experiment}",
        "created_at_utc": created_at_utc,
        "subject": f"Strategy Lab scoreboard — {experiment}",
        "eyebrow": "strategy lab",
        "title": experiment or "scoreboard",
        "headline": f"cutoff {inputs.get('cutoff_utc') or '—'} · metric "
        f"{inputs.get('primary_metric') or '—'}",
        "verdict": {
            "label": f"{len(warnings)} warning(s)" if warnings else "no warnings",
            "tone": "warn" if warnings else "ok",
        },
        "link_label": "Open the scoreboard",
        "blocks": blocks,
        "chart": None,
        "footer": [
            "Promotion is owner-only. A cleared gate is a recommendation to look, "
            "not an authorisation (Spec Q §3, §8).",
            "Every number here was computed by code; no model produced, adjusted or "
            "characterised any of them.",
        ],
    }
