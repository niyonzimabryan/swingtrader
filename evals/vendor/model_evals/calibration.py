"""Judge-calibration labeling packs.

Builds a tiny, human-labelable CSV from replay corpora and scores whether a
position-swapped LLM judge agrees with Bryan's labels closely enough to
tie-break DISCOVERY evals. Stdlib-only; no model calls happen here.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import sys
from dataclasses import dataclass
from statistics import mean
from typing import Iterable

from .schema import ReplayRecord, load_jsonl
from .scorers import judge_delta
from .stats import bootstrap_ci

HUMAN_LABELS = {
    "candidate": 1.0,
    "cand": 1.0,
    "b": 1.0,
    "incumbent": -1.0,
    "inc": -1.0,
    "a": -1.0,
    "tie": 0.0,
    "same": 0.0,
    "no_preference": 0.0,
    "no preference": 0.0,
}

CSV_FIELDS = [
    "app",
    "task",
    "item_id",
    "run_id",
    "input",
    "incumbent_output",
    "candidate_output",
    "judge_vote_ab",
    "judge_vote_ba",
    "human_preference",
    "notes",
]


@dataclass
class CalibrationResult:
    n: int
    n_excluded: int
    correlation: float
    agreement: float
    agreement_ci_low: float
    agreement_ci_high: float
    mean_judge_delta: float
    mean_human_delta: float
    clears_bar: bool
    reason: str


def prepare_rows(
    records: list[ReplayRecord],
    *,
    app: str,
    task: str | None = None,
    candidate_outputs: dict[str, dict] | None = None,
    count: int = 20,
    seed: int = 0,
    require_candidate: bool = False,
) -> list[dict]:
    """Select deterministic label rows from replay records."""
    candidate_outputs = candidate_outputs or {}
    filtered = [r for r in records if task is None or r.task == task]
    if require_candidate:
        filtered = [r for r in filtered if r.item_id in candidate_outputs]
    selected = _deterministic_sample(filtered, count=count, seed=seed)
    rows = []
    for r in selected:
        rows.append({
            "app": app,
            "task": r.task,
            "item_id": r.item_id,
            "run_id": r.run_id,
            "input": _compact_json(r.input),
            "incumbent_output": _compact_json(r.incumbent_output),
            "candidate_output": _compact_json(candidate_outputs.get(r.item_id, {})),
            "judge_vote_ab": _vote(candidate_outputs.get(r.item_id, {}).get("vote_ab")),
            "judge_vote_ba": _vote(candidate_outputs.get(r.item_id, {}).get("vote_ba")),
            "human_preference": "",
            "notes": "",
        })
    return rows


def prepare_top5_stage3_rows(
    logs_root: str,
    *,
    candidate_outputs: dict[str, dict] | None = None,
    count: int = 20,
    seed: int = 0,
    require_candidate: bool = False,
) -> list[dict]:
    """Build stage-3 calibration rows directly from top5 pipeline logs.

    One row is one run/topic editorial decision: the incumbent output is the
    logged top-5 package plus omissions. Candidate output can be supplied from a
    replay JSONL keyed by the same run key.
    """
    records = []
    for run_dir, run_key in _top5_run_dirs(logs_root):
        editorial = _load_json(os.path.join(run_dir, "03-editorial.json"))
        if not editorial or editorial.get("api_error"):
            continue
        records.append(ReplayRecord(
            task="top5.stage3",
            item_id=run_key,
            input={
                "date": editorial.get("date"),
                "topic_id": editorial.get("topic_id"),
                "input_count": editorial.get("input_count"),
            },
            incumbent_model=editorial.get("model") or "unknown",
            incumbent_output={
                "top5": editorial.get("top5", []),
                "collection_tagline": editorial.get("collection_tagline", ""),
                "collection_reasoning": editorial.get("collection_reasoning", ""),
                "notable_omissions": editorial.get("notable_omissions", []),
            },
            run_id=run_key,
        ))
    return prepare_rows(
        records,
        app="top5",
        task="top5.stage3",
        candidate_outputs=candidate_outputs,
        count=count,
        seed=seed,
        require_candidate=require_candidate,
    )


def load_candidate_outputs(path: str | None) -> dict[str, dict]:
    """Load candidate/judge outputs from JSONL keyed by item_id.

    Accepted lines:
      {"item_id": "...", "candidate_output": {...}}
      {"item_id": "...", "output": {...}}
      {"item_id": "...", "vote_ab": "B", "vote_ba": "A", ...}
    """
    if not path:
        return {}
    out = {}
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            raw = json.loads(line)
            iid = raw.get("item_id")
            if not iid:
                continue
            body = raw.get("candidate_output", raw.get("output"))
            if body is None:
                body = {k: v for k, v in raw.items() if k != "item_id"}
            out[str(iid)] = body
    return out


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in CSV_FIELDS})


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_html(rows: list[dict], path: str) -> None:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{html.escape(row['app'])}</td>"
            f"<td>{html.escape(row['task'])}</td>"
            f"<td>{html.escape(row['item_id'])}</td>"
            f"<td><pre>{html.escape(row['incumbent_output'])}</pre></td>"
            f"<td><pre>{html.escape(row['candidate_output'])}</pre></td>"
            "<td>candidate / incumbent / tie / skip</td>"
            "</tr>"
        )
    doc = """<!doctype html>
<meta charset="utf-8">
<title>Judge calibration label pack</title>
<style>
body { font: 14px/1.4 -apple-system, BlinkMacSystemFont, sans-serif; margin: 24px; }
table { border-collapse: collapse; width: 100%; }
th, td { border: 1px solid #ddd; padding: 8px; vertical-align: top; }
pre { white-space: pre-wrap; max-width: 520px; }
</style>
<h1>Judge calibration label pack</h1>
<p>Fill the CSV, not this HTML. Human labels: candidate, incumbent, tie, or skip.</p>
<table>
<thead><tr><th>App</th><th>Task</th><th>Item</th><th>Incumbent</th><th>Candidate</th><th>Label choices</th></tr></thead>
<tbody>
""" + "\n".join(body) + "\n</tbody></table>\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


def score_labels(
    rows: list[dict],
    *,
    min_items: int = 20,
    min_corr: float = 0.40,
    min_agreement: float = 0.65,
    seed: int = 0,
) -> CalibrationResult:
    pairs = []
    excluded = 0
    for row in rows:
        human = _human_delta(row.get("human_preference", ""))
        judge = judge_delta(row.get("judge_vote_ab", ""), row.get("judge_vote_ba", ""))
        if human is None or judge is None:
            excluded += 1
            continue
        pairs.append((human, judge))

    if not pairs:
        return CalibrationResult(0, excluded, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, False, "no valid labeled rows")

    human_values = [p[0] for p in pairs]
    judge_values = [p[1] for p in pairs]
    agreement_values = [1.0 if h == j else 0.0 for h, j in pairs]
    agreement, lo, hi = bootstrap_ci(agreement_values, seed=seed)
    corr = _pearson(human_values, judge_values)
    clears = len(pairs) >= min_items and corr >= min_corr and agreement >= min_agreement
    if len(pairs) < min_items:
        reason = f"UNDERPOWERED: n={len(pairs)} < min_items={min_items}"
    elif corr < min_corr:
        reason = f"HOLD: correlation {corr:.3f} < {min_corr:.3f}"
    elif agreement < min_agreement:
        reason = f"HOLD: agreement {agreement:.3f} < {min_agreement:.3f}"
    else:
        reason = "PASS: judge cleared calibration bar"
    return CalibrationResult(
        n=len(pairs),
        n_excluded=excluded,
        correlation=corr,
        agreement=agreement,
        agreement_ci_low=lo,
        agreement_ci_high=hi,
        mean_judge_delta=mean(judge_values),
        mean_human_delta=mean(human_values),
        clears_bar=clears,
        reason=reason,
    )


def render_result(result: CalibrationResult) -> str:
    status = "PASS" if result.clears_bar else "HOLD"
    return "\n".join([
        "# Judge Calibration Report",
        "",
        f"Status: **{status}**",
        "",
        f"- n: {result.n}",
        f"- excluded rows: {result.n_excluded}",
        f"- human/judge correlation: {result.correlation:.3f}",
        f"- exact agreement: {result.agreement:.3f} "
        f"[{result.agreement_ci_low:.3f}, {result.agreement_ci_high:.3f}]",
        f"- mean human delta: {result.mean_human_delta:.3f}",
        f"- mean judge delta: {result.mean_judge_delta:.3f}",
        f"- reason: {result.reason}",
        "",
    ])


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Prepare and score judge-calibration label packs.")
    sub = p.add_subparsers(dest="cmd", required=True)

    prep = sub.add_parser("prepare", help="prepare rows from ReplayRecord JSONL")
    prep.add_argument("--corpus", required=True, help="ReplayRecord JSONL")
    prep.add_argument("--app", required=True)
    prep.add_argument("--task")
    prep.add_argument("--candidate-output", help="JSONL keyed by item_id with candidate/judge output")
    prep.add_argument("--out", required=True, help="CSV path to write")
    prep.add_argument("--html", help="optional single-file HTML preview")
    prep.add_argument("--count", type=int, default=20)
    prep.add_argument("--seed", type=int, default=0)
    prep.add_argument("--require-candidate", action="store_true")

    top5 = sub.add_parser("prepare-top5-stage3", help="prepare top5 stage-3 rows from pipeline logs")
    top5.add_argument("--logs", required=True, help="top5 logs root")
    top5.add_argument("--candidate-output", help="JSONL keyed by run_key with candidate/judge output")
    top5.add_argument("--out", required=True)
    top5.add_argument("--html")
    top5.add_argument("--count", type=int, default=20)
    top5.add_argument("--seed", type=int, default=0)
    top5.add_argument("--require-candidate", action="store_true")

    score = sub.add_parser("score", help="score a completed calibration CSV")
    score.add_argument("--labels", required=True)
    score.add_argument("--out", help="optional markdown report path")
    score.add_argument("--min-items", type=int, default=20)
    score.add_argument("--min-corr", type=float, default=0.40)
    score.add_argument("--min-agreement", type=float, default=0.65)
    score.add_argument("--seed", type=int, default=0)

    args = p.parse_args(argv)
    if args.cmd == "prepare":
        rows = prepare_rows(
            load_jsonl(args.corpus),
            app=args.app,
            task=args.task,
            candidate_outputs=load_candidate_outputs(args.candidate_output),
            count=args.count,
            seed=args.seed,
            require_candidate=args.require_candidate,
        )
        write_csv(rows, args.out)
        if args.html:
            write_html(rows, args.html)
        print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
        return 0
    if args.cmd == "prepare-top5-stage3":
        rows = prepare_top5_stage3_rows(
            args.logs,
            candidate_outputs=load_candidate_outputs(args.candidate_output),
            count=args.count,
            seed=args.seed,
            require_candidate=args.require_candidate,
        )
        write_csv(rows, args.out)
        if args.html:
            write_html(rows, args.html)
        print(f"wrote {len(rows)} rows to {args.out}", file=sys.stderr)
        return 0
    if args.cmd == "score":
        result = score_labels(
            read_csv(args.labels),
            min_items=args.min_items,
            min_corr=args.min_corr,
            min_agreement=args.min_agreement,
            seed=args.seed,
        )
        md = render_result(result)
        if args.out:
            with open(args.out, "w", encoding="utf-8") as f:
                f.write(md)
        print(md)
        return 0
    raise AssertionError(args.cmd)


def _deterministic_sample(records: list[ReplayRecord], *, count: int, seed: int) -> list[ReplayRecord]:
    keyed = []
    for r in records:
        h = hashlib.sha256(f"{seed}:{r.task}:{r.item_id}".encode()).hexdigest()
        keyed.append((h, r))
    keyed.sort(key=lambda x: x[0])
    return [r for _, r in keyed[:count]]


def _compact_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _vote(value) -> str:
    v = (value or "").strip().upper() if isinstance(value, str) else ""
    return v if v in {"A", "B"} else ""


def _human_delta(value: str) -> float | None:
    v = (value or "").strip().lower()
    if not v or v == "skip":
        return None
    return HUMAN_LABELS.get(v)


def _pearson(xs: list[float], ys: list[float]) -> float:
    if len(xs) < 2 or len(xs) != len(ys):
        return 0.0
    mx, my = mean(xs), mean(ys)
    dx = sum((x - mx) ** 2 for x in xs) ** 0.5
    dy = sum((y - my) ** 2 for y in ys) ** 0.5
    if dx == 0 or dy == 0:
        return 0.0
    return sum((xs[i] - mx) * (ys[i] - my) for i in range(len(xs))) / (dx * dy)


def _top5_run_dirs(logs_root: str) -> Iterable[tuple[str, str]]:
    for date in sorted(os.listdir(logs_root)):
        ddir = os.path.join(logs_root, date)
        if not os.path.isdir(ddir):
            continue
        if os.path.exists(os.path.join(ddir, "03-editorial.json")):
            yield ddir, date
        for topic in sorted(os.listdir(ddir)):
            tdir = os.path.join(ddir, topic)
            if os.path.isdir(tdir) and os.path.exists(os.path.join(tdir, "03-editorial.json")):
                yield tdir, f"{date}/{topic}"


def _load_json(path: str):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


if __name__ == "__main__":
    raise SystemExit(main())
