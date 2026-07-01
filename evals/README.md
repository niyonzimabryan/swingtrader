# swingtrader model-evals

Model-swap evals + a P&L rollback monitor. Report-only — **nothing here edits
`config/settings.py`**. `vendor/` is generated (`model-registry/sync_evals.sh`).

## Run
```bash
python -m pytest evals                        # adapter tests (7)

# scoring parity — pull the corpus LIVE from Langfuse traces (needs LANGFUSE_* env):
export $(grep LANGFUSE ~/.env | xargs)
python -m evals.run_evals scoring --from-traces 2026-04-01T00:00:00Z --candidate claude-sonnet-5

# ...or from a local JSONL snapshot:
python -m evals.run_evals scoring --corpus evals/corpus/scoring.jsonl --candidate claude-sonnet-5

# P&L rollback monitor around a swap date (reads the SQLite outcomes DB):
python -m evals.run_evals pnl --db /data/swing_trader.db --swap-date 2026-08-01
```

## Capture is already on — no prod change needed (read this)
The bot's Langfuse OTEL auto-instrumentation **already captures every scoring call**
as a trace tagged `scoring` with full replayable input + the opus decision JSON.
`build_dataset.from_traces(...)` pulls that corpus directly (`langfuse_api.py`, a
stdlib REST reader — no SDK, works locally/CI). So "turning on shadow-logging"
needed **no edit to the money-bot hot path and no deploy**.

Verified 2026-07-01: pulls real records (tickers HIMS/TMCI/OSCR/…), decisions and
convictions parse, inputs are replayable. swingtrader is **not** offline-backfillable
(a scoring input is market-state at a moment), and only ~11–19 scoring calls exist,
so the eval correctly reports **UNDERPOWERED** (n < N_min=150). It becomes powered as
the bot runs and the trace corpus grows — nothing to accumulate manually.

`shadow_log.py` (explicit Langfuse *dataset* writes) stays available if you later want
a curated store instead of raw traces, but it's optional and unused in v1. `pnl_monitor`
watches realized P&L as a rollback signal only (P&L never gates; §2).

## Files
- `langfuse_api.py` — stdlib REST reader for Langfuse (tolerant of the flaky endpoint)
- `build_dataset.py` — `from_traces` (live pull), `from_jsonl`, `from_langfuse`
- `pnl_monitor.py` — closed-trade P&L join (memos→trades→tickers); pre/post-swap regression check
- `shadow_log.py` — optional explicit dataset writes (unused in v1)
- `tasks.py` — `scoring_spec` (act/skip agreement ≥ 0.90), `filter_spec` (traded-ticker recall ≥ 0.98)
- `run_evals.py` — the CLI
