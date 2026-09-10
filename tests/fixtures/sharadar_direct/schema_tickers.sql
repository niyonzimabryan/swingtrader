-- Sharadar table schema: tickers (SQLite)
-- As of 2026-08-18
-- https://api.sharadar.com/v1.0/schema/tickers?format=sqlite

CREATE TABLE IF NOT EXISTS "tickers" (
  "table" TEXT NOT NULL,
  "permaticker" INTEGER NOT NULL,
  "ticker" TEXT NOT NULL,
  "name" TEXT,
  "exchange" TEXT,
  "isdelisted" TEXT,
  "category" TEXT,
  "cusips" TEXT,
  "siccode" INTEGER,
  "sicsector" TEXT,
  "sicindustry" TEXT,
  "figi" TEXT,
  "famaindustry" TEXT,
  "sector" TEXT,
  "industry" TEXT,
  "scalemarketcap" TEXT,
  "scalerevenue" TEXT,
  "relatedtickers" TEXT,
  "currency" TEXT,
  "location" TEXT,
  "lastupdated" TEXT,
  "firstadded" TEXT,
  "firstpricedate" TEXT,
  "lastpricedate" TEXT,
  "firstquarter" TEXT,
  "lastquarter" TEXT,
  "secfilings" TEXT,
  "companysite" TEXT,
  PRIMARY KEY ("table", "permaticker", "ticker")
);

CREATE INDEX IF NOT EXISTS "tickers_lastupdated_idx" ON "tickers" ("lastupdated");

CREATE INDEX IF NOT EXISTS "tickers_permaticker_idx" ON "tickers" ("permaticker");

CREATE INDEX IF NOT EXISTS "tickers_table_idx" ON "tickers" ("table");

CREATE INDEX IF NOT EXISTS "tickers_ticker_idx" ON "tickers" ("ticker");
