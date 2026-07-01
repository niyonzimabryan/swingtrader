# swingtrader model-evals

Model-swap evals + a P&L rollback monitor. Report-only — **nothing here edits
`config/settings.py`**. `vendor/` is generated (`model-registry/sync_evals.sh`).

## Run
```bash
python -m pytest evals                        # adapter tests (4)

# scoring parity (Opus vs candidate) over the shadow-logged corpus:
python -m evals.run_evals scoring --corpus evals/corpus/scoring.jsonl \
    --candidate claude-sonnet-5 --candidate-out evals/corpus/sonnet5.jsonl

# P&L rollback monitor around a swap date (reads the SQLite outcomes DB):
python -m evals.run_evals pnl --db /data/swing_trader.db --swap-date 2026-08-01
```

## The data situation (read this)
swingtrader is **not** offline-backfillable (a scoring input is market-state at a
moment). Today Langfuse holds ~19 historical scoring calls and the local DB is empty,
so the scoring eval reports **UNDERPOWERED** (n < N_min=150) — by design, not a bug.
The job now is to *accumulate forward*:

1. In prod, after each scoring call, `shadow_log.record_from_scoring(...)` →
   `shadow_log.materialize_langfuse(...)` appends a replay-record to a Langfuse dataset.
2. When the dataset clears 150 items across ≥2 market regimes, the scoring parity
   verdict becomes powered.
3. Meanwhile `pnl_monitor` watches realized P&L (rollback signal only — P&L never
   gates a swap; design §2).

## Files
- `shadow_log.py` — freeze scoring calls into replay-records (→ Langfuse dataset)
- `pnl_monitor.py` — closed-trade P&L join (memos→trades→tickers); pre/post-swap regression check
- `build_dataset.py` — corpus from JSONL or a Langfuse dataset
- `tasks.py` — `scoring_spec` (act/skip agreement ≥ 0.90), `filter_spec` (traded-ticker recall ≥ 0.98)
- `run_evals.py` — the CLI
