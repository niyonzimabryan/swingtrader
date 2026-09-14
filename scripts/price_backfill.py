"""Backfill the price plane from a source into `price_bars` and friends.

    python -m scripts.price_backfill --source fixture --since 2023-01-01
    python -m scripts.price_backfill --source sharadar --since 2015-01-01 --tickers AAPL,MSFT
    python -m scripts.price_backfill --source sharadar --bulk years=10
    python -m scripts.price_backfill --source sharadar --resume /tmp/sharadar_bulk_checkpoint.json
    python -m scripts.price_backfill --source sharadar --since 2016-01-01 \
        --tickers SPY --asset-class fund

Idempotent: the natural keys in `data/prices/store.py` mean re-running the same
`--since` rewrites the same rows rather than duplicating them, so a run that
dies half way is resumed by running it again.

Every name is checked against the Spec N §4.3 reconstruction identity before it
is stored (`derived.check_reconstruction`). A vendor whose adjusted closes do not
agree with its own factors is caught here, at ingest, and not six weeks later in
a cohort. `--skip-reconstruction-check` exists for triage only and prints a
warning that says so.

`--bulk years=5|10|full` (Sharadar only) downloads the vendor's pre-built zip
for `stocks` and `actions` instead of paging `daily_bars`/`corporate_actions`
once per ticker — the right mode for the 10-year Prices tier the owner buys,
where paging thousands of names one at a time would take hours. Security
master rows are still fetched through the ordinary slice path, batched to keep
the `ticker=` query string a sane length, since `tickers` is a full-snapshot
table either way (Sharadar re-publishes it whole regardless of `years`).
`--tickers` narrows a bulk run to a subset **during** staging now, not after
parsing — a filtered run never writes another name's rows to the staging file.

Memory-bounded and resumable (`data/prices/bulk_stream.py`,
`data/prices/sharadar.py`'s "Bulk downloads" docstring section): the `stocks`
zip is streamed into an on-disk SQLite staging file rather than held in memory,
one ticker is derived, checked and stored (one transaction) at a time, and a
checkpoint is written after every ticker. Two flags control this:

* `--checkpoint PATH` — where to write progress (default: a fixed path under
  the OS temp directory, printed at the end of a `--bulk` run so it can be
  handed to `--resume`). Also names the staging SQLite file (`PATH` with a
  `.sqlite` suffix), which persists after the process exits specifically so a
  killed run's staged rows survive it.
* `--resume PATH` — finish a checkpointed `--bulk` run instead of starting a
  new one. Every parameter (`years`, `--tickers`, `--since`, `--until`,
  `--skip-reconstruction-check`) is read back from the checkpoint, not from
  this invocation's flags; only `--source`, `--max-rss-mb` and the enabling
  environment variables need repeating. Tickers already committed are never
  reprocessed — a `--resume` after a clean finish does nothing.
* `--max-rss-mb N` (default 1500) — sampled with `utils.memory.max_rss_mb`
  between tickers; exceeding it aborts cleanly (checkpoint saved, exit code 3)
  rather than waiting for the platform to SIGKILL the process the way the
  whole-zip-in-memory implementation did in production
  (`docs/investment-workspace/handoff/OWNER_SETUP_EXECUTION_2026-09-12.md` §4).

`--asset-class equity|fund|auto` says which Sharadar price table to read.
`equity` is the default and sends `table=stocks`, exactly as this script did
before funds existed. `fund` sends `table=funds` (legacy SFP) — the only table
SPY is in, and therefore the only way to load the benchmark that every abnormal
return in Spec N §5.2 is measured against. `auto` asks the vendor's `tickers`
master which table each name is in and uses that, which is the right answer for
a mixed `--tickers` list and the wrong answer to guess from a symbol.

`fund` and `auto` additionally require `PRICE_PLANE_FUNDS_ENABLED=true`; the
default `equity` does not, so this script behaves identically to before with no
new variable set.

A fund run prints the resulting `security_uid` for each name, because that is
the value the owner has to paste into `COMPARABLE_BENCHMARK_SECURITY_UID`
(`docs/ENV_SETUP.md` §7). `scripts/benchmark_uid.py` prints the same value from
the stored master afterwards, without re-running a backfill.

Requires `PRICE_PLANE_ENABLED=true`, and for `--source sharadar`,
`SHARADAR_API_KEY`.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Iterator, Sequence
from datetime import date, datetime
from pathlib import Path

from data.prices import config as plane_config
from data.prices import store
from data.prices.base import (
    ASSET_CLASS_EQUITY,
    ASSET_CLASS_FUND,
    PricePlane,
    PricePlaneError,
)
from data.prices.derived import check_reconstruction
from utils.memory import max_rss_mb as sample_rss_mb
from utils.timeutils import utcnow_naive

#: Tickers per `security_master` call in bulk mode, so the comma-joined
#: `ticker=` query string stays well under any sane URL-length limit even for
#: a `years=full` run over the whole market.
#: Sharadar rejects a `ticker` query parameter longer than 200 **characters**:
#: "Invalid ticker parameter: ticker exceeds maximum length of 200 characters.
#: Use fewer tickers, date filters, or bulk download (years=) for large
#: universes." — observed live against the production API on 2026-09-14.
#:
#: The previous `MASTER_BATCH_SIZE = 200` read that limit as 200 *tickers*.
#: Two hundred four-character symbols join to roughly a thousand characters, so
#: every `--bulk` run died on its very first master lookup, before writing a
#: single bar. `--tickers` with a short list stayed under the limit by accident,
#: which is why the ten-name slice load worked and the whole-market one never
#: could.
#:
#: 190 rather than 200 leaves room for the vendor counting the parameter
#: slightly differently than we do; the cost of the margin is a few extra
#: requests and the cost of being wrong is the whole run.
MASTER_TICKER_PARAM_MAX_CHARS = 190

#: `--max-rss-mb` default. Chosen well under the bot container's 8 GB cgroup
#: limit — the platform SIGKILLed at 3.4 GB in production, so this guard is
#: meant to abort long before either ceiling, leaving room to actually see the
#: checkpoint and `--resume` rather than losing the process outright.
DEFAULT_MAX_RSS_MB = 1500.0

#: Rows staged per batch — see `data/prices/sharadar.DEFAULT_BULK_BATCH_SIZE`.
DEFAULT_BULK_BATCH_SIZE = 50_000

#: Where `--checkpoint` writes progress when a `--bulk` run does not name one
#: explicitly. A fixed path under the OS temp directory, not a fresh one per
#: run: the whole point is that a second invocation (after a kill) can find
#: it again without the operator having had to note it down first.
DEFAULT_CHECKPOINT_PATH = Path(tempfile.gettempdir()) / "sharadar_bulk_checkpoint.json"


def parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


# --------------------------------------------------------------------------- #
# Bulk checkpoint — one JSON file, read and rewritten after every ticker
# --------------------------------------------------------------------------- #


def _staging_db_path_for(checkpoint_path: Path) -> Path:
    return checkpoint_path.with_suffix(".sqlite")


def _new_bulk_checkpoint(
    *,
    checkpoint_path: Path,
    staging_db_path: Path,
    source: str,
    years: str,
    tickers: list[str] | None,
    since: date | None,
    until: date | None,
    check: bool,
    batch_size: int,
) -> dict:
    return {
        "checkpoint_path": str(checkpoint_path),
        "staging_db_path": str(staging_db_path),
        "source": source,
        "years": years,
        "tickers": tickers,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "check": check,
        "batch_size": batch_size,
        "staging_complete": False,
        "tickers_done": [],
        "bars_written": 0,
        "actions_written": 0,
        "securities_written": 0,
        "tickers_empty": [],
        "tickers_without_a_security_master_row": [],
        "aborted_reason": None,
        "started_at": utcnow_naive().isoformat(),
        "updated_at": None,
    }


def _master_batches(
    tickers: Sequence[str],
    max_chars: int = MASTER_TICKER_PARAM_MAX_CHARS,
) -> Iterator[list[str]]:
    """Group `tickers` so each `",".join(batch)` stays inside the vendor limit.

    Batching by joined length rather than by count is the whole point: symbols
    run from one to five characters, so a fixed count is either wastefully
    small or over the limit depending on which names the zip happens to carry.

    A single symbol longer than `max_chars` is still yielded on its own — there
    is no smaller request to make, and letting the vendor refuse it is more
    honest than dropping it silently.
    """
    batch: list[str] = []
    length = 0
    for ticker in tickers:
        addition = len(ticker) + (1 if batch else 0)
        if batch and length + addition > max_chars:
            yield batch
            batch, length = [ticker], len(ticker)
        else:
            batch.append(ticker)
            length += addition
    if batch:
        yield batch


def _save_bulk_checkpoint(state: dict) -> None:
    """Write `state` to its own `checkpoint_path`, atomically.

    Write-to-temp-then-`rename` so a process killed mid-write never leaves a
    half-written checkpoint behind for the next `--resume` to choke on — the
    exact failure mode this file exists to make survivable.
    """
    state["updated_at"] = utcnow_naive().isoformat()
    path = Path(state["checkpoint_path"])
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _load_bulk_checkpoint(path: str | Path) -> dict:
    path = Path(path)
    if not path.exists():
        raise PricePlaneError(f"--resume: no checkpoint file at {path}")
    return json.loads(path.read_text())


def _bulk_summary(state: dict, *, aborted: bool) -> dict:
    tickers_done = state["tickers_done"]
    tickers_with_bars = (
        len(tickers_done)
        - len(state["tickers_empty"])
        - len(state["tickers_without_a_security_master_row"])
    )
    return {
        "source": state["source"],
        "since": state["since"],
        "until": state["until"],
        "bulk_years": state["years"],
        "tickers_requested": len(state["tickers"]) if state["tickers"] else None,
        "tickers_with_bars": tickers_with_bars,
        "bars_written": state["bars_written"],
        "actions_written": state["actions_written"],
        "securities_written": state["securities_written"],
        "tickers_empty": state["tickers_empty"],
        "tickers_without_a_security_master_row": state["tickers_without_a_security_master_row"],
        "reconstruction_checked": state["check"],
        "checkpoint_path": state["checkpoint_path"],
        "staging_db_path": state["staging_db_path"],
        "aborted": aborted,
        "aborted_reason": state.get("aborted_reason"),
    }


def backfill(
    plane: PricePlane,
    tickers: list[str],
    since: date | None,
    until: date | None = None,
    check: bool = True,
    asset_class: str | None = ASSET_CLASS_EQUITY,
) -> dict:
    """Pull master, actions and bars for `tickers` and store them.

    `asset_class` is passed straight to `plane.security_master`: a class name
    to look up one table, or `None` to let the vendor's master say which table
    each name is in. It is **not** passed to `daily_bars`, which resolves the
    table itself through the same master — one source of truth for "what kind
    of instrument is this", and no way for the two calls to disagree.

    `security_uid_by_ticker` is in the summary because a fund run exists to
    produce exactly that value: `COMPARABLE_BENCHMARK_SECURITY_UID` is a uid,
    not a ticker, and it is otherwise only discoverable by querying the
    database by hand. `main()` prints it and then drops it before recording the
    snapshot — see the comment there.
    """
    from database.db import get_session

    summary = {
        "source": plane.source,
        "since": since.isoformat() if since else None,
        "until": until.isoformat() if until else None,
        "asset_class": asset_class or "auto",
        "tickers_requested": len(tickers),
        "tickers_with_bars": 0,
        "bars_written": 0,
        "actions_written": 0,
        "securities_written": 0,
        "tickers_empty": [],
        "security_uid_by_ticker": {},
        "asset_class_by_ticker": {},
        "reconstruction_checked": check,
    }

    with get_session() as session:
        # Batched for the same reason as the bulk path above: an explicit
        # `--tickers` list of more than ~40 symbols would otherwise blow the
        # vendor's 200-character `ticker` parameter limit.
        master_rows = []
        for batch in _master_batches(list(tickers)):
            master_rows.extend(plane.security_master(batch, asset_class=asset_class))
        summary["securities_written"] = store.upsert_securities(session, master_rows)
        summary["security_uid_by_ticker"] = {
            row.ticker: row.security_uid for row in master_rows
        }
        summary["asset_class_by_ticker"] = {
            row.ticker: row.asset_class for row in master_rows
        }
        for ticker in tickers:
            bars = plane.daily_bars(ticker, since, until)
            if not bars:
                summary["tickers_empty"].append(ticker)
                continue
            if check:
                check_reconstruction(bars)
            summary["bars_written"] += store.upsert_bars(session, bars)
            summary["actions_written"] += store.upsert_corporate_actions(
                session, plane.corporate_actions(ticker, since, until)
            )
            summary["tickers_with_bars"] += 1

    return summary


def backfill_bulk(
    plane: PricePlane,
    years: str,
    tickers: list[str] | None,
    since: date | None,
    until: date | None = None,
    check: bool = True,
    checkpoint_path: str | Path | None = None,
    resume: bool = False,
    max_rss_mb: float | None = DEFAULT_MAX_RSS_MB,
    batch_size: int = DEFAULT_BULK_BATCH_SIZE,
) -> dict:
    """Bulk-load `stocks` + `actions` from Sharadar's zip download, streaming.

    `plane` must be a `SharadarPricePlane` (bulk is not part of the generic
    `PricePlane` interface — `FixturePricePlane` has no vendor zip to fetch).
    Does **not** call `load_bulk_bars`: the `stocks` zip is streamed into an
    on-disk staging file (`plane.open_bulk_bar_stream`,
    `data/prices/bulk_stream.py`) and drained one ticker at a time, so peak
    memory is one ticker's history plus one staging batch, never the whole
    market (`data/prices/sharadar.py`'s "Bulk downloads" docstring section).
    `tickers`, if given, narrows the staged rows to a subset **during**
    staging; otherwise every ticker in the zip is staged.

    The bulk `stocks`/`actions` CSVs carry no `permaticker` (only `ticker`),
    so the streamed bars carry a placeholder `security_uid`. That placeholder
    is replaced here with the real permaticker-derived uid from
    `security_master` before anything is stored — `price_bars` and
    `securities` are joined on `security_uid` (`data/prices/store.py`), so
    storing the placeholder would silently orphan every bulk-loaded bar from
    its security-master row.

    Each ticker is stored in **one transaction** and checkpointed immediately
    after (`checkpoint_path`, default a fixed path under the OS temp
    directory) — a run killed at any point resumes with `resume=True` (or the
    CLI's `--resume`) from the next ticker that was not yet committed, not
    from the top of the zip. `max_rss_mb`, sampled between tickers, aborts the
    run the same clean way (checkpoint saved, `summary["aborted"]` set)
    instead of leaving it to the platform to SIGKILL the process.

    Returns a summary dict; on a `max_rss_mb` abort it reflects everything
    committed so far, with `"aborted": True` and `"aborted_reason"` set.
    """
    from contextlib import ExitStack
    from dataclasses import replace as _replace

    from database.db import get_session

    if not hasattr(plane, "bulk_download"):
        raise PricePlaneError(f"{plane.source} has no bulk download path")

    with ExitStack() as stack:
        if resume:
            if checkpoint_path is None:
                raise PricePlaneError(
                    "--resume needs --checkpoint naming the file from the run being resumed"
                )
            state = _load_bulk_checkpoint(checkpoint_path)
        elif checkpoint_path is not None:
            checkpoint_path = Path(checkpoint_path)
            state = _new_bulk_checkpoint(
                checkpoint_path=checkpoint_path,
                staging_db_path=_staging_db_path_for(checkpoint_path),
                source=plane.source, years=years, tickers=list(tickers) if tickers else None,
                since=since, until=until, check=check, batch_size=batch_size,
            )
            _save_bulk_checkpoint(state)
        else:
            # No persistence requested: an ephemeral checkpoint/staging file
            # for the life of this call only, exactly like the pre-streaming
            # implementation's `tempfile.TemporaryDirectory()` for its zip
            # downloads — a fresh path per call, never shared across
            # concurrent callers or leaked into a fixed location.
            tmp_dir = stack.enter_context(tempfile.TemporaryDirectory(prefix="sharadar_bulk_ckpt_"))
            checkpoint_path = Path(tmp_dir) / "checkpoint.json"
            state = _new_bulk_checkpoint(
                checkpoint_path=checkpoint_path,
                staging_db_path=_staging_db_path_for(checkpoint_path),
                source=plane.source, years=years, tickers=list(tickers) if tickers else None,
                since=since, until=until, check=check, batch_size=batch_size,
            )
            _save_bulk_checkpoint(state)

        years = state["years"]
        tickers = state["tickers"]
        since = date.fromisoformat(state["since"]) if state["since"] else None
        until = date.fromisoformat(state["until"]) if state["until"] else None
        check = state["check"]
        staging_db_path = Path(state["staging_db_path"])

        if not state["staging_complete"]:
            # First attempt at staging, or a prior attempt died mid-stage
            # (`staging_complete` only ever flips once staging finishes) —
            # either way, (re)download and (re)stage from scratch. Staging is
            # cheap to redo (bounded by network/disk I/O, not memory), which
            # is what makes a mid-staging kill safe to `--resume` from at all.
            with tempfile.TemporaryDirectory(prefix="sharadar_bulk_dl_") as tmp:
                stocks_zip = plane.bulk_download("stocks", years, Path(tmp) / "stocks.zip")
                actions_zip = plane.bulk_download("actions", years, Path(tmp) / "actions.zip")
                actions_by_ticker = plane.load_bulk_actions(actions_zip)
                stream = plane.open_bulk_bar_stream(
                    stocks_zip, staging_db_path, tickers=tickers,
                    batch_size=state["batch_size"], actions_by_ticker=actions_by_ticker,
                )
            state["staging_complete"] = True
            _save_bulk_checkpoint(state)
        else:
            # Staging already finished on a prior run: never re-download or
            # re-parse the (potentially whole-market) stocks zip again. The
            # actions zip is small (~5 MB) and re-fetched either way, since it
            # was never staged to disk in the first place.
            with tempfile.TemporaryDirectory(prefix="sharadar_bulk_dl_") as tmp:
                actions_zip = plane.bulk_download("actions", years, Path(tmp) / "actions.zip")
                actions_by_ticker = plane.load_bulk_actions(actions_zip)
            stream = plane.open_bulk_bar_stream(
                None, staging_db_path, actions_by_ticker=actions_by_ticker, resume_staging=True,
            )

        try:
            with get_session() as session:
                ticker_list = stream.tickers()
                uid_by_ticker: dict[str, str] = {}
                securities_written = 0
                for batch in _master_batches(ticker_list):
                    master_rows = plane.security_master(batch)
                    securities_written += store.upsert_securities(session, master_rows)
                    uid_by_ticker.update({row.ticker: row.security_uid for row in master_rows})
                session.commit()
                state["securities_written"] += securities_written
                _save_bulk_checkpoint(state)

                done = set(state["tickers_done"])
                for ticker in ticker_list:
                    if ticker in done:
                        continue  # belt-and-suspenders; `drop` already removes it from `ticker_list`

                    uid = uid_by_ticker.get(ticker)
                    if uid is None:
                        # `tickers` has no row for this name — refuse to store
                        # bars under the bulk parser's placeholder uid, same
                        # principle as `_uid_for` refusing to invent one on
                        # the slice path.
                        state["tickers_without_a_security_master_row"].append(ticker)
                        state["tickers_done"].append(ticker)
                        stream.drop(ticker)
                        _save_bulk_checkpoint(state)
                        continue

                    bars = tuple(
                        _replace(bar, security_uid=uid)
                        for bar in stream.bars_for(ticker)
                        if (since is None or bar.session_date >= since)
                        and (until is None or bar.session_date <= until)
                    )
                    if not bars:
                        state["tickers_empty"].append(ticker)
                        state["tickers_done"].append(ticker)
                        stream.drop(ticker)
                        _save_bulk_checkpoint(state)
                        continue

                    if check:
                        check_reconstruction(bars)

                    actions = tuple(
                        _replace(action, security_uid=uid)
                        for action in actions_by_ticker.get(ticker, ())
                        if (since is None or action.ex_date >= since)
                        and (until is None or action.ex_date <= until)
                    )
                    bars_written = store.upsert_bars(session, bars)
                    actions_written = store.upsert_corporate_actions(session, actions)
                    session.commit()  # one transaction per ticker

                    state["bars_written"] += bars_written
                    state["actions_written"] += actions_written
                    state["tickers_done"].append(ticker)
                    stream.drop(ticker)
                    _save_bulk_checkpoint(state)

                    if max_rss_mb is not None:
                        rss = sample_rss_mb()
                        if rss > max_rss_mb:
                            state["aborted_reason"] = (
                                f"RSS {rss:.0f}MB exceeded --max-rss-mb {max_rss_mb:.0f} "
                                f"after {ticker}"
                            )
                            _save_bulk_checkpoint(state)
                            return _bulk_summary(state, aborted=True)
        finally:
            stream.close()

    return _bulk_summary(state, aborted=False)


def parse_bulk_years(value: str) -> str:
    """`years=10` or bare `10` -> `"10"`, validated against `BULK_YEARS`."""
    from data.prices.sharadar import BULK_YEARS

    years = value.split("=", 1)[1] if "=" in value else value
    years = years.strip()
    if years not in BULK_YEARS:
        raise argparse.ArgumentTypeError(f"--bulk must name years in {BULK_YEARS}, got {value!r}")
    return years


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", default=None, help="fixture | sharadar")
    parser.add_argument("--since", type=parse_date, default=None, help="YYYY-MM-DD")
    parser.add_argument("--until", type=parse_date, default=None, help="YYYY-MM-DD")
    parser.add_argument(
        "--tickers", default="",
        help="comma-separated; default is every ticker the source knows (fixture), "
        "or every ticker in the zip (--bulk)",
    )
    parser.add_argument(
        "--bulk", default=None, type=parse_bulk_years, metavar="years=5|10|full",
        help="Sharadar only: load stocks+actions from the vendor's bulk zip "
        "instead of paging per ticker",
    )
    parser.add_argument(
        "--checkpoint", default=None, metavar="PATH",
        help="--bulk only: where to write progress (default: a fixed path under "
        f"the OS temp directory, {DEFAULT_CHECKPOINT_PATH}). Also names the "
        "staging SQLite file, which persists after this process exits so a "
        "killed run can --resume from it.",
    )
    parser.add_argument(
        "--resume", default=None, metavar="PATH",
        help="finish a checkpointed --bulk run from PATH instead of starting a new "
        "one; --bulk, --tickers, --since, --until and --skip-reconstruction-check "
        "are read back from the checkpoint and this invocation's own values are "
        "ignored",
    )
    parser.add_argument(
        "--max-rss-mb", type=float, default=DEFAULT_MAX_RSS_MB, metavar="MB",
        help="--bulk/--resume only: abort cleanly, with a checkpoint, if this "
        f"process's RSS exceeds MB, sampled between tickers (default {DEFAULT_MAX_RSS_MB:.0f})",
    )
    parser.add_argument(
        "--asset-class", default=ASSET_CLASS_EQUITY,
        choices=(ASSET_CLASS_EQUITY, ASSET_CLASS_FUND, "auto"),
        help="which Sharadar price table to read: equity -> stocks (default, "
        "unchanged behaviour), fund -> funds/SFP (SPY lives here), auto -> ask "
        "the vendor master per ticker. fund and auto need "
        "PRICE_PLANE_FUNDS_ENABLED=true.",
    )
    parser.add_argument("--snapshot", default=None, help="snapshot slug to record coverage on")
    parser.add_argument("--skip-reconstruction-check", action="store_true")
    args = parser.parse_args(argv)

    if args.bulk and args.resume:
        print("--bulk and --resume are mutually exclusive: --resume replays "
              "the checkpointed run's own --bulk/--tickers/--since/--until", file=sys.stderr)
        return 2

    settings = plane_config.get_settings()
    try:
        plane_config.require_enabled(settings)
        plane = plane_config.build_plane(args.source, settings)
    except PricePlaneError as exc:
        print(f"price backfill refused: {exc}", file=sys.stderr)
        return 2

    if args.bulk and plane.source != "sharadar":
        print(f"--bulk is Sharadar-only; --source resolved to {plane.source!r}", file=sys.stderr)
        return 2

    asset_class = None if args.asset_class == "auto" else args.asset_class
    if asset_class != ASSET_CLASS_EQUITY:
        try:
            plane_config.require_funds_enabled(settings)
        except PricePlaneError as exc:
            print(f"price backfill refused: {exc}", file=sys.stderr)
            return 2
    if args.bulk and asset_class != ASSET_CLASS_EQUITY:
        # The bulk zips this script knows how to parse are `stocks` and
        # `actions`. A `funds` bulk zip is a separate brief; refusing is the
        # honest answer, because `--bulk --asset-class fund` would otherwise
        # silently load equities and report success.
        print(
            "--bulk covers the `stocks` zip only; run a fund with the slice "
            "path (--tickers SPY --asset-class fund).",
            file=sys.stderr,
        )
        return 2

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    if not tickers and not args.bulk and not args.resume:
        listed = getattr(plane, "tickers", None)
        if listed is None:
            print(
                "--tickers is required for this source: it has no enumerable universe.",
                file=sys.stderr,
            )
            return 2
        tickers = list(listed())

    from database.db import get_session, init_db

    init_db(settings.database_url)

    if args.skip_reconstruction_check:
        print(
            "WARNING: storing series without the §4.3 reconstruction check. "
            "The stored bars may not reproduce from their own factors.",
            file=sys.stderr,
        )

    if args.bulk or args.resume:
        checkpoint_path = args.resume or args.checkpoint or DEFAULT_CHECKPOINT_PATH
        try:
            summary = backfill_bulk(
                plane, args.bulk, tickers or None, args.since, args.until,
                check=not args.skip_reconstruction_check,
                checkpoint_path=checkpoint_path,
                resume=bool(args.resume),
                max_rss_mb=args.max_rss_mb,
            )
        except PricePlaneError as exc:
            print(f"price backfill refused: {exc}", file=sys.stderr)
            return 2
    else:
        summary = backfill(
            plane, tickers, args.since, args.until,
            check=not args.skip_reconstruction_check, asset_class=asset_class,
        )

    # The two per-ticker maps are for the printout below, not for the stored
    # snapshot: `coverage_summary_json` is one text column, and a 5,000-name
    # equity backfill would put a 5,000-entry uid map in it on every run. The
    # scalar `asset_class` stays, because "which table was this snapshot
    # loaded from" is exactly the kind of thing a snapshot should record.
    uids = summary.pop("security_uid_by_ticker", None) or {}
    classes = summary.pop("asset_class_by_ticker", None) or {}

    snapshot = args.snapshot or settings.price_plane_snapshot
    with get_session() as session:
        store.record_snapshot(session, snapshot, plane.source, summary)

    print(
        f"{summary['bars_written']} bars, {summary['actions_written']} actions, "
        f"{summary['securities_written']} securities from {plane.source} "
        f"into snapshot {snapshot!r}"
    )
    if summary["tickers_empty"]:
        print(f"no bars for: {', '.join(summary['tickers_empty'])}")

    if "checkpoint_path" in summary:
        print(f"checkpoint: {summary['checkpoint_path']}")

    if summary.get("aborted"):
        print(
            f"bulk backfill aborted: {summary['aborted_reason']}; "
            f"resume with --resume {summary['checkpoint_path']}",
            file=sys.stderr,
        )
        return 3

    # The uid printout. A fund run exists to produce it: the benchmark variable
    # is a `security_uid`, and nothing else in the pipeline ever shows one to a
    # human. Printed for every non-default asset class, including `auto`, since
    # `auto` is how an operator finds out a name was a fund at all.
    funds = sorted(t for t, k in classes.items() if k == ASSET_CLASS_FUND)
    if funds:
        print("\nfunds loaded — these are the security_uids:")
        for ticker in funds:
            print(f"  {ticker:<8} {uids.get(ticker, '?')}")
        print(
            "\nSet the cohort benchmark to one of them, e.g.\n"
            f"  COMPARABLE_BENCHMARK_SECURITY_UID={uids.get(funds[0], '?')}\n"
            "then run `python -m scripts.cohort_smoke` (docs/ENV_SETUP.md §7)."
        )
    elif asset_class != ASSET_CLASS_EQUITY:
        print(
            f"\nno fund rows came back for {', '.join(tickers) or 'the requested names'}; "
            "the vendor master put every one of them in the equity table."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
