"""On-disk SQLite staging for a Sharadar bulk CSV zip.

`SharadarPricePlane.load_bulk_bars` used to read a whole-market zip's CSV
member into a `dict[ticker, list[dict]]` before building a single `DailyBar`.
For the 10-year `stocks` zip that is two full in-memory copies of the whole
market — the accumulation into that dict is what put the bot container at
3.4 GB RSS and got it SIGKILLed (`docs/investment-workspace/handoff/
OWNER_SETUP_EXECUTION_2026-09-12.md` §4). `zipfile` and `csv.DictReader`
already stream the file itself; only the accumulation needed to change.

`BulkStagingStore` replaces the dict with a table on disk: rows are read from
the zip in one pass, in batches, and written to a SQLite file indexed on
`ticker`. A caller then processes one ticker's rows at a time
(`rows_for_ticker`) and `drop_ticker`s them once stored, so peak memory is one
ticker's history plus one staging batch — not the whole market — and peak
staging-disk usage shrinks back down as tickers are drained rather than
growing for the life of the run.

Only the caller's `expected` columns are staged, never every column the CSV
happens to carry. Two reasons, not one: it keeps this store's SQL — table and
column names built from `expected` — safe to build from vendor-supplied CSV
headers, since only a fixed, hardcoded tuple from `data/prices/sharadar.py`
(`STOCKS_COLUMNS` / `ACTIONS_COLUMNS`) ever reaches it; and nothing downstream
reads an unexpected column anyway, so staging it would only cost disk.
"""

from __future__ import annotations

import csv
import io
import sqlite3
import zipfile
from pathlib import Path
from typing import Sequence

from data.prices.base import PricePlaneSchemaError

#: Rows buffered in memory before one `INSERT` batch, and staged rows'
#: read-back granularity. 50k rows of the ~10 columns this store ever stages
#: is a few MB — nowhere near a peak-memory concern next to "the whole zip".
DEFAULT_BATCH_SIZE = 50_000

#: The staging table. One per `BulkStagingStore` — a fresh instance per zip.
TABLE_NAME = "rows"


class BulkStagingStore:
    """A staging table for one bulk CSV's rows, on a SQLite file at `db_path`.

    `stage()` streams a zip's rows in; `distinct_tickers()`, `rows_for_ticker()`
    and `drop_ticker()` are how a caller drains it one ticker at a time.
    Opening a path that already carries a staged table (from a prior process
    that staged and then died) reads its column list back from the schema, so
    `--resume` can reopen a completed staging pass with no `stage()` call.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self.columns: tuple[str, ...] = ()
        if self.has_data():
            cursor = self._conn.execute(f"PRAGMA table_info({TABLE_NAME})")
            self.columns = tuple(row[1] for row in cursor.fetchall())

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "BulkStagingStore":
        return self

    def __exit__(self, *exc_info) -> bool:
        self.close()
        return False

    def has_data(self) -> bool:
        """Whether a staging table already exists (not whether it has rows)."""
        cursor = self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
            (TABLE_NAME,),
        )
        return cursor.fetchone() is not None

    def stage(
        self,
        zip_path: str | Path,
        expected: Sequence[str],
        tickers: Sequence[str] | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> int:
        """Stream `zip_path`'s single CSV member's `expected` columns in.

        Drops and recreates the staging table first, so re-staging after a
        killed run never leaves duplicate rows from the partial attempt —
        staging is cheap to redo from the zip, which is what makes a
        mid-staging kill safe to `--resume` from (`scripts/price_backfill.py`
        just restages rather than trying to resume the byte stream itself).

        `tickers`, if given, filters **during** staging: a name not in
        `tickers` is never written to `db_path` at all. Raises
        `PricePlaneSchemaError` on the same two conditions
        `SharadarPricePlane._read_bulk_csv` always has: not exactly one CSV
        member in the zip, or one of `expected` absent from its header.

        Returns the number of rows staged.
        """
        wanted = set(tickers) if tickers else None
        self.columns = tuple(expected)
        self._recreate_table(self.columns)

        staged = 0
        with zipfile.ZipFile(zip_path) as archive:
            members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if len(members) != 1:
                raise PricePlaneSchemaError(
                    f"{zip_path}: expected exactly one CSV member, found {members}"
                )
            with archive.open(members[0]) as handle:
                text = io.TextIOWrapper(handle, encoding="utf-8")
                reader = csv.DictReader(text)
                fieldnames = reader.fieldnames or ()
                missing = [name for name in expected if name not in fieldnames]
                if missing:
                    raise PricePlaneSchemaError(
                        f"{zip_path}: expected columns {missing} are absent. "
                        f"The CSV header was {list(fieldnames)}."
                    )

                batch: list[tuple] = []
                for row in reader:
                    if wanted is not None and row.get("ticker") not in wanted:
                        continue
                    batch.append(tuple(row.get(c) for c in self.columns))
                    if len(batch) >= batch_size:
                        self._insert_batch(batch)
                        staged += len(batch)
                        batch = []
                if batch:
                    self._insert_batch(batch)
                    staged += len(batch)

        self._conn.execute(
            f'CREATE INDEX IF NOT EXISTS idx_{TABLE_NAME}_ticker ON {TABLE_NAME}(ticker)'
        )
        self._conn.commit()
        return staged

    def _recreate_table(self, columns: Sequence[str]) -> None:
        self._conn.execute(f"DROP TABLE IF EXISTS {TABLE_NAME}")
        col_defs = ", ".join(f'"{c}" TEXT' for c in columns)
        self._conn.execute(f"CREATE TABLE {TABLE_NAME} ({col_defs})")
        self._conn.commit()

    def _insert_batch(self, batch: list[tuple]) -> None:
        placeholders = ", ".join("?" for _ in self.columns)
        col_list = ", ".join(f'"{c}"' for c in self.columns)
        self._conn.executemany(
            f"INSERT INTO {TABLE_NAME} ({col_list}) VALUES ({placeholders})", batch
        )
        self._conn.commit()

    def distinct_tickers(self) -> list[str]:
        """Every ticker still staged, ascending — deterministic run order."""
        cursor = self._conn.execute(
            f"SELECT DISTINCT ticker FROM {TABLE_NAME} ORDER BY ticker"
        )
        return [row[0] for row in cursor.fetchall()]

    def rows_for_ticker(self, ticker: str) -> list[dict]:
        col_list = ", ".join(f'"{c}"' for c in self.columns)
        cursor = self._conn.execute(
            f"SELECT {col_list} FROM {TABLE_NAME} WHERE ticker = ? ORDER BY date",
            (ticker,),
        )
        return [dict(zip(self.columns, row)) for row in cursor.fetchall()]

    def drop_ticker(self, ticker: str) -> None:
        """Delete one ticker's staged rows once its bars are safely stored.

        This is the boundary a `--resume` run trusts: a ticker whose rows are
        gone is a ticker that finished, so it never reappears in
        `distinct_tickers()` and is never reprocessed.
        """
        self._conn.execute(f"DELETE FROM {TABLE_NAME} WHERE ticker = ?", (ticker,))
        self._conn.commit()
