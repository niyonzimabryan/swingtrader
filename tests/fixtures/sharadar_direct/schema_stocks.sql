-- Sharadar table schema: stocks (SQLite)
-- As of 2026-08-18
-- https://api.sharadar.com/v1.0/schema/stocks?format=sqlite

CREATE TABLE IF NOT EXISTS "stocks" (
  "ticker" TEXT NOT NULL,
  "date" TEXT NOT NULL,
  "open" REAL,
  "high" REAL,
  "low" REAL,
  "close" REAL,
  "volume" REAL,
  "closeadj" REAL,
  "closeunadj" REAL,
  "lastupdated" TEXT,
  PRIMARY KEY ("ticker", "date")
);

CREATE INDEX IF NOT EXISTS "stocks_date_idx" ON "stocks" ("date");

CREATE INDEX IF NOT EXISTS "stocks_lastupdated_idx" ON "stocks" ("lastupdated");
