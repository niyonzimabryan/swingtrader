"""report — the artifact you *read* (design §7).

One cost/quality table per task. Self-dating: the header stamps the registry
content hash + the eval commit so a stale report is detectable by eye (the cheap
thread kept from the deferred v2 attestation idea). Status/upgrade columns come
straight from the registry, so the report doubles as a rot check.
"""
from __future__ import annotations

import subprocess
from collections import defaultdict


def _git_short_rev() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, text=True
        ).strip()
    except Exception:
        return "unknown"


def _today() -> str:
    from datetime import date
    return date.today().isoformat()


def _status_cell(catalog, model) -> str:
    try:
        e = catalog.entry(model)
    except Exception:
        return "?"
    st = e.get("status", "?")
    if "upgrade" in e:
        return f"{st}↑{e['upgrade']}"
    if e.get("status") in ("deprecated", "retired"):
        return f"{st}→{e.get('replace', '?')}"
    return st


def _cost_cell(cost, estimated) -> str:
    if cost is None:
        return "n/a"
    return f"{'~' if estimated else ''}${cost:.4f}"


def render(results, catalog, eval_commit: str | None = None, date: str | None = None) -> str:
    eval_commit = eval_commit or _git_short_rev()
    date = date or _today()
    lines = [
        "# Model-eval report",
        "",
        f"- registry (models.json) hash: `{catalog.hash()}`",
        f"- eval commit: `{eval_commit}`",
        f"- date: {date}",
        "- v1 report-driven: verdicts advise; a human edits config. `~` = estimated cost.",
        "",
    ]
    by_task = defaultdict(list)
    for r in results:
        by_task[r.task].append(r)

    for task, rows in by_task.items():
        r0 = rows[0]
        baseline = 1.0 if r0.mode == "parity" else 0.0
        lines += [
            f"## {task}  ({r0.mode})",
            "",
            f"Primary metric: **{r0.primary_metric}**  ·  floor/effect: **{r0.threshold}**",
            "",
            "| Model | Status | $/run | Quality | Δ vs incumbent | 95% CI | Verdict |",
            "|---|---|---|---|---|---|---|",
            f"| {r0.incumbent_model} (incumbent) | {_status_cell(catalog, r0.incumbent_model)} "
            f"| {_cost_cell(r0.cost_incumbent, r0.cost_estimated)} | {baseline:.3f} | — | — | — |",
        ]
        for r in rows:
            delta = r.value - baseline
            lines.append(
                f"| {r.candidate_model} | {_status_cell(catalog, r.candidate_model)} "
                f"| {_cost_cell(r.cost_candidate, r.cost_estimated)} | {r.value:.3f} "
                f"| {delta:+.3f} | [{r.ci_low:.3f}, {r.ci_high:.3f}] | **{r.verdict}** (n={r.n}) |"
            )
        lines.append("")
        for r in rows:
            lines.append(f"- **{r.candidate_model} → {r.verdict}**: {r.reason}")
        lines.append("")
    return "\n".join(lines)
