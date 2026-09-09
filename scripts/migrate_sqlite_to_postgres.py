#!/usr/bin/env python3
"""One-shot SQLite -> Postgres migration with a parity proof (Spec K §3.2).

    python -m scripts.migrate_sqlite_to_postgres \
        --source sqlite:////data/swing_trader.db \
        --target "postgresql+psycopg://user:pass@host:5432/railway"

What it does, in order:

1. Opens the SQLite source **read-only** (``mode=ro`` URI) — the file stays an
   archive and this script cannot be the thing that damages it.
2. Brings the target to the Alembic head (``ensure_schema``), so the column
   types are the ones the models declare rather than whatever a raw ``CREATE
   TABLE`` would guess.
3. Refuses a target that already holds rows unless ``--force`` is given, in
   which case it deletes them in reverse dependency order first. That is what
   makes the script idempotent: a second run reproduces the first run's target
   exactly instead of doubling every table.
4. Copies every table in dependency order (parents before children), in
   batches, preserving primary keys.
5. Resets each Postgres identity sequence past the largest copied id, so the
   first application insert after the cutover does not collide.
6. Reads both databases back and compares, per table, the row count **and** a
   content hash computed identically on both engines.
7. Writes a Markdown report to ``docs/audits/``.

Exit status is 0 only if every table matched on both count and hash.

The content hash
----------------

Rows are read through SQLAlchemy with the ORM column types applied, ordered by
primary key, and each value is rendered into one canonical text form before
hashing. That canonicalisation is the whole point: SQLite hands back ``0`` for a
boolean and Postgres hands back ``False``; SQLite hands back an ``int`` for a
whole number in a ``Float`` column and Postgres a ``float``. A hash over the raw
driver values would differ on identical data and prove nothing. A hash over the
canonical form differs only when the data differs.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import Boolean, Date, Float, Integer, create_engine, func, select
from sqlalchemy.engine import Engine, make_url

from database.models import Base
from database.schema import ensure_schema
from utils.timeutils import utcnow_naive

DEFAULT_REPORT_DIR = Path(__file__).resolve().parents[1] / "docs" / "audits"
DEFAULT_BATCH_SIZE = 1000

#: Field and row separators for the canonical row rendering. Chosen because no
#: canonical value can contain them: every value is either a repr, an ISO
#: timestamp, a hex digest, or a string with its own escaping applied.
FIELD_SEP = "\x1f"
ROW_SEP = "\x1e"


class MigrationError(RuntimeError):
    """The migration cannot proceed, or did not verify."""


# --- ordering ---------------------------------------------------------------


def tables_in_dependency_order() -> list:
    """Parents before children, so foreign keys are satisfiable as we insert."""
    return list(Base.metadata.sorted_tables)


# --- canonical row rendering ------------------------------------------------


def canonical(value, column) -> str:
    """One engine-independent text form for a stored value.

    Driven by the *column type*, not by the Python type that came back, because
    the Python type is exactly what differs between the two engines.
    """
    if value is None:
        return "\\N"

    type_ = column.type
    if isinstance(type_, Boolean):
        # SQLite: 0/1. Postgres: False/True.
        return "1" if value else "0"
    if isinstance(type_, Float):
        # SQLite returns an int for a whole number stored in a REAL column.
        return repr(float(value))
    if isinstance(type_, Integer):
        return repr(int(value))
    if isinstance(type_, Date) and not isinstance(value, datetime):
        return value.isoformat() if isinstance(value, date) else str(value)
    if isinstance(value, datetime):
        # UtcDateTime has already normalised these to naive UTC on read.
        return value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if FIELD_SEP in text or ROW_SEP in text:
        raise MigrationError(
            f"{column.table.name}.{column.name} contains a separator byte; the "
            "content hash cannot be computed unambiguously."
        )
    return text


def order_by_columns(table) -> list:
    pk = list(table.primary_key.columns)
    return pk or list(table.columns)


def content_hash(engine: Engine, table, batch_size: int = DEFAULT_BATCH_SIZE):
    """``(row_count, sha256_hex)`` for one table, engine-independent."""
    columns = list(table.columns)
    digest = hashlib.sha256()
    rows = 0
    statement = select(*columns).order_by(*order_by_columns(table))
    with engine.connect().execution_options(stream_results=True) as connection:
        result = connection.execute(statement)
        while True:
            chunk = result.fetchmany(batch_size)
            if not chunk:
                break
            for row in chunk:
                rendered = FIELD_SEP.join(
                    canonical(value, column) for value, column in zip(row, columns)
                )
                digest.update(rendered.encode("utf-8"))
                digest.update(ROW_SEP.encode("utf-8"))
                rows += 1
    return rows, digest.hexdigest()


def row_count(engine: Engine, table) -> int:
    with engine.connect() as connection:
        return int(connection.execute(select(func.count()).select_from(table)).scalar_one())


# --- source / target plumbing ----------------------------------------------


def read_only_source_url(url: str) -> str:
    """A SQLite URL the driver opens read-only; anything else, unchanged.

    The production file is an archive from the moment the cutover starts. The
    driver enforcing that is worth more than a promise in a docstring.
    """
    parsed = make_url(url)
    if parsed.get_backend_name() != "sqlite":
        return url
    path = parsed.database
    if not path or path == ":memory:":
        return url
    return f"sqlite:///file:{path}?mode=ro&uri=true"


def target_is_populated(engine: Engine) -> dict[str, int]:
    """Tables in the target that already hold rows."""
    populated = {}
    for table in tables_in_dependency_order():
        count = row_count(engine, table)
        if count:
            populated[table.name] = count
    return populated


def clear_target(engine: Engine) -> None:
    """Delete every migration-owned row, children before parents."""
    with engine.begin() as connection:
        for table in reversed(tables_in_dependency_order()):
            connection.execute(table.delete())


def copy_table(source: Engine, target: Engine, table, batch_size: int) -> int:
    columns = list(table.columns)
    names = [c.name for c in columns]
    copied = 0
    statement = select(*columns).order_by(*order_by_columns(table))
    with source.connect().execution_options(stream_results=True) as reader:
        result = reader.execute(statement)
        while True:
            chunk = result.fetchmany(batch_size)
            if not chunk:
                break
            payload = [dict(zip(names, row)) for row in chunk]
            with target.begin() as writer:
                writer.execute(table.insert(), payload)
            copied += len(payload)
    return copied


def resync_sequences(target: Engine) -> list[str]:
    """Point each Postgres identity sequence past the largest copied id.

    Rows are inserted with their original primary keys, which does not advance
    the sequence. Without this the first application insert after the cutover
    fails on a duplicate key — the classic dump-and-load footgun.
    """
    if target.dialect.name != "postgresql":
        return []
    resynced = []
    with target.begin() as connection:
        for table in tables_in_dependency_order():
            for column in table.primary_key.columns:
                if not isinstance(column.type, Integer):
                    continue
                sequence = connection.exec_driver_sql(
                    "SELECT pg_get_serial_sequence(%s, %s)",
                    (table.name, column.name),
                ).scalar()
                if not sequence:
                    continue
                connection.exec_driver_sql(
                    f'SELECT setval(%s, COALESCE((SELECT MAX("{column.name}") '
                    f'FROM "{table.name}"), 0) + 1, false)',
                    (sequence,),
                )
                resynced.append(f"{table.name}.{column.name}")
    return resynced


# --- verification & report --------------------------------------------------


def verify(source: Engine, target: Engine, batch_size: int) -> list[dict]:
    results = []
    for table in tables_in_dependency_order():
        source_rows, source_hash = content_hash(source, table, batch_size)
        target_rows, target_hash = content_hash(target, table, batch_size)
        results.append(
            {
                "table": table.name,
                "source_rows": source_rows,
                "target_rows": target_rows,
                "source_hash": source_hash,
                "target_hash": target_hash,
                "ok": source_rows == target_rows and source_hash == target_hash,
            }
        )
    return results


def render_report(results, source_url, target_url, *, forced, resynced) -> str:
    ok = all(r["ok"] for r in results)
    total_source = sum(r["source_rows"] for r in results)
    total_target = sum(r["target_rows"] for r in results)
    lines = [
        "# SQLite -> Postgres migration report",
        "",
        f"**Run:** {utcnow_naive().isoformat(sep=' ', timespec='seconds')} UTC  ",
        f"**Source:** `{source_url}`  ",
        f"**Target:** `{target_url}`  ",
        f"**Existing target rows cleared first (`--force`):** {'yes' if forced else 'no'}  ",
        f"**Result:** {'PASS — every table matched on row count and content hash' if ok else 'FAIL — see the table below'}",
        "",
        f"{len(results)} tables, {total_source} source rows, {total_target} target rows.",
        "",
        "| Table | Source rows | Target rows | Source hash | Target hash | Parity |",
        "| --- | ---: | ---: | --- | --- | --- |",
    ]
    for r in results:
        lines.append(
            f"| `{r['table']}` | {r['source_rows']} | {r['target_rows']} | "
            f"`{r['source_hash'][:16]}` | `{r['target_hash'][:16]}` | "
            f"{'ok' if r['ok'] else '**MISMATCH**'} |"
        )
    lines += [
        "",
        "Hashes are SHA-256 over the table's rows ordered by primary key, with "
        "each value rendered into one canonical text form so that a boolean or a "
        "whole-number float hashes the same on both engines. They are truncated "
        "to 16 hex characters for display; the script compares the full digest.",
        "",
        f"Identity sequences resynced: {len(resynced)}"
        + (f" ({', '.join(resynced)})" if resynced else ""),
        "",
    ]
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------


def run(
    source_url: str,
    target_url: str,
    *,
    force: bool = False,
    batch_size: int = DEFAULT_BATCH_SIZE,
    report_dir: Path | None = None,
    verify_only: bool = False,
) -> dict:
    """Migrate and verify. Returns the run summary; raises on refusal."""
    source = create_engine(read_only_source_url(source_url))
    target = create_engine(target_url)
    try:
        action = ensure_schema(target)
        populated = target_is_populated(target)
        forced = False

        if not verify_only:
            if populated and not force:
                raise MigrationError(
                    "Target is not empty and --force was not given. Rows already "
                    f"present: {populated}. Re-run with --force to delete them "
                    "and reload from the source, or point --target at an empty "
                    "database."
                )
            if populated:
                clear_target(target)
                forced = True
            for table in tables_in_dependency_order():
                copy_table(source, target, table, batch_size)

        resynced = [] if verify_only else resync_sequences(target)
        results = verify(source, target, batch_size)
        ok = all(r["ok"] for r in results)

        report = render_report(
            results,
            make_url(source_url).render_as_string(hide_password=True),
            make_url(target_url).render_as_string(hide_password=True),
            forced=forced,
            resynced=resynced,
        )
        report_path = None
        if report_dir is not None:
            report_dir.mkdir(parents=True, exist_ok=True)
            stamp = utcnow_naive().strftime("%Y-%m-%dT%H%M%SZ")
            report_path = report_dir / f"{stamp}-sqlite-to-postgres.md"
            report_path.write_text(report, encoding="utf-8")

        return {
            "ok": ok,
            "schema_action": action,
            "forced": forced,
            "results": results,
            "report": report,
            "report_path": report_path,
        }
    finally:
        source.dispose()
        target.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Copy a SQLite database into Postgres and prove parity.",
    )
    parser.add_argument(
        "--source",
        default=os.environ.get("SQLITE_SOURCE_URL", "sqlite:///swing_trader.db"),
        help="SQLite URL to read (opened read-only).",
    )
    parser.add_argument(
        "--target",
        default=os.environ.get("DATABASE_URL", ""),
        help="Postgres URL to write. Defaults to $DATABASE_URL.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Delete existing target rows first. Without this a non-empty "
        "target is refused.",
    )
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=DEFAULT_REPORT_DIR,
        help="Where the Markdown parity report is written.",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Compare the two databases and write the report; copy nothing.",
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv[1:])
    if not args.target:
        print("--target (or $DATABASE_URL) is required", file=sys.stderr)
        return 2
    try:
        summary = run(
            args.source,
            args.target,
            force=args.force,
            batch_size=args.batch_size,
            report_dir=args.report_dir,
            verify_only=args.verify_only,
        )
    except MigrationError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(summary["report"])
    if summary["report_path"]:
        print(f"report written to {summary['report_path']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
