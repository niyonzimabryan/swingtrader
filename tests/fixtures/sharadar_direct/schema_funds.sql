-- Sharadar table schema: funds (SQLite)
-- As of 2026-08-18
-- https://api.sharadar.com/v1.0/schema/funds?format=sqlite

CREATE TABLE IF NOT EXISTS "funds" (
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

CREATE INDEX IF NOT EXISTS "funds_date_idx" ON "funds" ("date");

CREATE INDEX IF NOT EXISTS "funds_lastupdated_idx" ON "funds" ("lastupdated");
