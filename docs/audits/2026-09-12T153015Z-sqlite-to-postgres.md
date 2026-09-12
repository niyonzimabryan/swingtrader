# SQLite -> Postgres migration report

**Run:** 2026-09-12 15:30:15 UTC  
**Source:** `sqlite:////tmp/snap.db`  
**Target:** `postgresql+psycopg://postgres:***@postgres.railway.internal:5432/railway`  
**Existing target rows cleared first (`--force`):** no  
**Result:** PASS — every table matched on row count and content hash

61 tables, 1764 source rows, 1764 target rows.

| Table | Source rows | Target rows | Source hash | Target hash | Parity |
| --- | ---: | ---: | --- | --- | --- |
| `brokerage_accounts` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `cohort_answers` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `cohort_predictions` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `company_profiles` | 15 | 15 | `c791fd54a973986d` | `c791fd54a973986d` | ok |
| `comparable_queries` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `corporate_actions` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `discovered_tickers` | 136 | 136 | `2973effa2b71f4ad` | `2973effa2b71f4ad` | ok |
| `dossiers` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `entity_history` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `execution_kill_switch` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `experiments` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `exposure_tags` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `historical_events` | 33 | 33 | `0250d6736462d3bc` | `0250d6736462d3bc` | ok |
| `macro_regime` | 16 | 16 | `b0936db525deca93` | `b0936db525deca93` | ok |
| `market_snapshots` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `news_articles` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `news_clusters` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `pattern_provider_cache` | 117 | 117 | `f8f789db6baac0e0` | `f8f789db6baac0e0` | ok |
| `pattern_search_runs` | 20 | 20 | `92577046ea126058` | `92577046ea126058` | ok |
| `peer_edges` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `pipeline_runs` | 25 | 25 | `60ac2d3373ad8c23` | `60ac2d3373ad8c23` | ok |
| `portfolio_snapshots` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `price_bars` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `price_snapshots` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `proposals` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `research_questions` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `scored_candidates` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `securities` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `source_observations` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `strategy_versions` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `theses` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `tickers` | 566 | 566 | `d5a5d5a792738134` | `d5a5d5a792738134` | ok |
| `universe_membership` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `watchlist_tickers` | 1 | 1 | `5670e60e03d06ea9` | `5670e60e03d06ea9` | ok |
| `web_research_cache` | 18 | 18 | `c0f368d4c08e2207` | `c0f368d4c08e2207` | ok |
| `workspace_tokens` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `broker_orders` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `cash_balances` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `catalysts` | 263 | 263 | `4344960132966b84` | `4344960132966b84` | ok |
| `decision_journal` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `dossier_sections` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `event_contexts` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `event_outcomes` | 33 | 33 | `302ce10e06e0c141` | `302ce10e06e0c141` | ok |
| `experiment_arms` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `fundamentals` | 66 | 66 | `dfb8249953058b8d` | `dfb8249953058b8d` | ok |
| `historical_patterns` | 113 | 113 | `62a70a5780bfbbe6` | `62a70a5780bfbbe6` | ok |
| `holdings` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `memos` | 20 | 20 | `8b27f03f923574eb` | `8b27f03f923574eb` | ok |
| `price_data` | 303 | 303 | `d04adc72851224ee` | `d04adc72851224ee` | ok |
| `signals` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `tax_lots` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `thesis_invalidators` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `web_research` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `deep_research_requests` | 1 | 1 | `021c8bced0f4a7a1` | `021c8bced0f4a7a1` | ok |
| `experiment_metric_snapshots` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `historical_contexts` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `strategy_decisions` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `trades` | 8 | 8 | `ad66acb6c9874d1f` | `ad66acb6c9874d1f` | ok |
| `order_events` | 10 | 10 | `e2c76abdafa954d8` | `e2c76abdafa954d8` | ok |
| `promotion_events` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |
| `strategy_trades` | 0 | 0 | `e3b0c44298fc1c14` | `e3b0c44298fc1c14` | ok |

Hashes are SHA-256 over the table's rows ordered by primary key, with each value rendered into one canonical text form so that a boolean or a whole-number float hashes the same on both engines. They are truncated to 16 hex characters for display; the script compares the full digest.

Identity sequences resynced: 60 (brokerage_accounts.id, cohort_answers.id, cohort_predictions.id, company_profiles.id, comparable_queries.id, corporate_actions.id, discovered_tickers.id, dossiers.id, entity_history.id, experiments.id, exposure_tags.id, historical_events.id, macro_regime.id, market_snapshots.id, news_articles.id, news_clusters.id, pattern_provider_cache.id, pattern_search_runs.id, peer_edges.id, pipeline_runs.id, portfolio_snapshots.id, price_bars.id, price_snapshots.id, proposals.id, research_questions.id, scored_candidates.id, securities.id, source_observations.id, strategy_versions.id, theses.id, tickers.id, universe_membership.id, watchlist_tickers.id, web_research_cache.id, workspace_tokens.id, broker_orders.id, cash_balances.id, catalysts.id, decision_journal.id, dossier_sections.id, event_contexts.id, event_outcomes.id, experiment_arms.id, fundamentals.id, historical_patterns.id, holdings.id, memos.id, price_data.id, signals.id, tax_lots.id, thesis_invalidators.id, web_research.id, deep_research_requests.id, experiment_metric_snapshots.id, historical_contexts.id, strategy_decisions.id, trades.id, order_events.id, promotion_events.id, strategy_trades.id)

