-- Sharadar table schema: sp500 (SQLite)
-- As of 2026-08-18
-- https://api.sharadar.com/v1.0/schema/sp500?format=sqlite

CREATE TABLE IF NOT EXISTS "sp500" (
  "date" TEXT NOT NULL,
  "action" TEXT NOT NULL,
  "ticker" TEXT NOT NULL,
  "name" TEXT,
  "contraticker" TEXT,
  "contraname" TEXT,
  "note" TEXT,
  PRIMARY KEY ("date", "action", "ticker")
);

CREATE INDEX IF NOT EXISTS "sp500_action_idx" ON "sp500" ("action");

CREATE INDEX IF NOT EXISTS "sp500_contraticker_idx" ON "sp500" ("contraticker");

CREATE INDEX IF NOT EXISTS "sp500_ticker_idx" ON "sp500" ("ticker");
