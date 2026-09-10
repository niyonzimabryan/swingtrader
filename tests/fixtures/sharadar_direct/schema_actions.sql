-- Sharadar table schema: actions (SQLite)
-- As of 2026-08-18
-- https://api.sharadar.com/v1.0/schema/actions?format=sqlite

CREATE TABLE IF NOT EXISTS "actions" (
  "date" TEXT NOT NULL,
  "action" TEXT NOT NULL,
  "ticker" TEXT NOT NULL,
  "name" TEXT NOT NULL,
  "value" REAL,
  "contraticker" TEXT NOT NULL,
  "contraname" TEXT NOT NULL,
  PRIMARY KEY ("date", "action", "ticker", "name", "contraticker", "contraname")
);

CREATE INDEX IF NOT EXISTS "actions_action_idx" ON "actions" ("action");

CREATE INDEX IF NOT EXISTS "actions_contraticker_idx" ON "actions" ("contraticker");

CREATE INDEX IF NOT EXISTS "actions_ticker_idx" ON "actions" ("ticker");
