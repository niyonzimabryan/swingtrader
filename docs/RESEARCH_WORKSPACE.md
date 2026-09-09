# Research workspace (Spec M)

Durable, sourced, revisitable research that a new session picks up mid-thought.
Dossiers say what we know, theses say what we believe **and what would change
our mind**, and the decision journal says what was decided and why — including
the decisions to pass.

**Flag:** `RESEARCH_WORKSPACE_ENABLED`, default `false`. With it off the five
research tools are not registered on the MCP surface, the daily invalidator
check is a no-op, and `scripts/sync_research_mirror.py` refuses to run.

Spec: [`specs/investment-workspace/M-research-workspace.md`](../specs/investment-workspace/M-research-workspace.md).

---

## 1. The model

Six tables, added by `migrations/versions/0004_research_workspace.py`.

| Table | What it holds |
| --- | --- |
| `dossiers` | One row per ticker: name, sector, timestamps. |
| `dossier_sections` | **Append-only** revisions. A write inserts a row pointing at the one it supersedes; nothing is ever overwritten, so "what did we believe in July" is a query. |
| `theses` | The claim, the argument, the required bear case, a **stated probability** and `resolution_at`, the status, and the denormalised count of machine-checkable invalidators. |
| `thesis_invalidators` | The load-bearing table: a description, a type, the parameters to check it, and `post_hoc`. |
| `decision_journal` | Append-only decisions with the thesis hash, the cohort answer id, the budget drawn from, and — filled in later by a job — what actually happened. |
| `research_questions` | What an agent could not resolve, written down instead of guessed at. |

Everything narrative lives in a section, including the fields Spec M calls
structured metadata (`business_model`, `revenue_drivers`, `key_customers`,
`competitive_position`, `capital_structure`). Only `sector` is a column: it is
the one field that is a filter rather than prose, and a section carries the
sources and the revision history a scalar column would throw away.

### The gate on `active`

A thesis reaches `active` only with **all** of:

1. at least one invalidator,
2. at least one **machine-checkable** invalidator,
3. a stated probability in `[0, 1]`,
4. a `resolution_at` date,
5. a non-empty bear case.

Enforced twice. `research_workspace/store.py::activate` refuses with the full
list of what is missing (a checklist, not the first blocker), and three CHECK
constraints on `theses` — supported identically by SQLite and Postgres — stop a
hand-written `UPDATE` from producing an unfalsifiable active thesis. The count
of machine-checkable invalidators is denormalised onto `theses` precisely so
that constraint can exist: a CHECK cannot count rows in another table.

### Probabilities and revisions

The first probability written lands in `original_probability` and is never
touched again. Later revisions update `probability` and append to
`probability_history_json`. **The Brier score uses the original.** A forecast
corrected towards the outcome is not a forecast.

---

## 2. Invalidators: what triggers each

`research_workspace/invalidators.py`. The daily job
(`research_workspace/jobs.py::daily_invalidator_check`) evaluates every armed
invalidator on every `active` or `weakened` thesis. Pure Python — no model call
anywhere in it (Spec K §5).

| Type | Parameters | Triggers when |
| --- | --- | --- |
| `price_level` | `{"operator": "below"\|"above", "price": 82.0, "consecutive_sessions": 3}` | the last *n* **closes** are all on the wrong side of the level. An intraday wick is not a close. |
| `metric_threshold` | `{"fact_type": "gross_margin", "operator": "below", "value": 0.45, "consecutive_periods": 2}` plus optional `entity_cik` / `ticker` / `unit` | the latest known value for each of the last *n* periods is on the wrong side. Read from `source_observations` through `filings/observations.py` at today's cutoff, so a restatement supersedes the original without either row being deleted. |
| `time_decay` | `{"deadline": "2027-03-31"}`, or `{"quarters": 2}` / `{"days": 180}` from the position open date (falling back to the invalidator's `created_at`), plus optional `unless_metric` | the deadline has passed. With `unless_metric`, it does **not** trigger if the named observable moved the way the thesis said it would. |
| `event` | `{"event_key": "guidance_cut", "match": {…}}` | a matcher registered by a later plane says so. With no matcher the check reports `pending_plane` — not `not_triggered`, because "nothing happened" and "nobody is watching" are different sentences. |
| `qualitative` | none | never. Created with status `needs_human_review` rather than `armed`, so "armed" means exactly "a job is watching this", and surfaced for the human in every check run. |

Operators: `below`, `below_or_equal`, `above`, `above_or_equal`. Parameters are
validated at **write** time — a check job that discovers a malformed parameter
at 6am is a check that silently never ran.

Two rules hold for every type:

- **Missing data never triggers.** A price feed that is down, a fact type with
  no observations, an event plane that has not shipped: each reports its own
  outcome (`insufficient_data`, `pending_plane`) and leaves the thesis alone. A
  page fired because a feed broke teaches the owner to ignore pages.
- **A trigger pages and does nothing else.** The invalidator moves to
  `triggered`, the thesis moves from `active` to `weakened`, and a `PageEvent`
  goes through the same Telegram path the rest of the system uses
  (`research_workspace/paging.py`, wired from `main.py`). **No order and no
  proposal is created**, and the package cannot import `execution/`, `bot/` or
  `orchestrator/` — asserted by `tests/test_no_execute_scope.py`, not by
  convention.

Post-hoc invalidators (added after the linked position opened) are kept,
flagged, watched, and **excluded from the honesty metrics** (Spec M §4 rule 3).

### Prices

`price_level` reads through `research_workspace/prices.py`, not directly from a
vendor. `TablePriceSource` reads the cached `price_data` rows (offline, and what
tests use); `MarketDataPriceSource` wraps the incumbent
`data/market_data.py::MarketDataAdapter`. When the Spec O price plane lands,
that is the one import site to change.

---

## 3. Source tiers, trust, and what the mirror withholds

`research_workspace/trust.py`. A source answers two independent questions.

| Tier | Content trust | In the repo mirror? |
| --- | --- | --- |
| `human`, `model`, `internal_analysis` | trusted | yes |
| `primary_regulator` (EDGAR), `company_disclosure` | **untrusted** | yes |
| `news`, `vendor_data`, `web_scrape` | **untrusted** | **withheld** |

- **Untrusted content** is text this system did not write — filings included,
  because anyone who can file with the SEC can put text in front of it. It is
  marked `content_trust: "untrusted"` in every response and wrapped in
  delimiters carrying a per-response random nonce (Spec P §5). That is a
  parsing aid, not a control; the controls are architectural — no tool here can
  place an order, and no tool here can produce a statistic.
- **Withheld from the mirror** is content the public repo may not carry
  (Spec K §3.3): vendor licences forbid redistribution, and news bodies and
  news-derived features are out for the same reason.

`research_write` refuses a section whose sources are **all** unaccountable
third-party text (`news`, `vendor_data`, `web_scrape`) unless `human_authored`
is set. A section that also rests on a filing, a computed number, or a human's
own knowledge is a synthesis, not a laundered news body. A section with **no**
sources is *not* refused: it is written `unsourced=true` and carries a warning
in every surface that renders it.

---

## 4. The Markdown mirror

`scripts/sync_research_mirror.py`. Direction is **Postgres → repo**.

```
research/
  companies/AMD.md                       dossier: sections, sources, warnings
  theses/2026-09-amd-mi400-ramp.md       front matter carries the invalidators
  journal/2026-09.md                     decisions, budgets, cohort ids
  questions.md                           the open questions
```

Every file has YAML front matter and a do-not-hand-edit banner.

```bash
python -m scripts.sync_research_mirror            # export
python -m scripts.sync_research_mirror --check    # would anything change?
python -m scripts.sync_research_mirror --import   # read prose back, validated
```

**The export is deliberately partial.** A section whose sources include a
withheld tier is written as a marker carrying its Postgres id:

```
<!-- withheld: news-derived, see dossier_sections/41 -->
```

`--import` reads that marker as **keep the database copy**. The round-trip
guarantee therefore applies to *permitted* content — human and model prose,
sourced claims, cohort ids, summary figures — and both halves are tested
(`test_mirror_roundtrip_permitted_content`, `test_mirror_withholds_by_provenance`).

`--import` applies prose: a dossier section body (as an append-only revision,
through the same validation the tools use) and a thesis's claim and bear case.
It **refuses** a hand-edited `status`, `probability`, `resolution_at` or
invalidator list, naming the tool that owns the field. Those are exactly the
fields the §4 gate protects, and a Markdown file cannot carry the gate with
them. `questions.md` is export-only.

**Offline fallback.** `research_workspace/offline.py` answers "what is my view
on X, and what would change it" from the files alone. It imports no database
module, no HTTP client, and nothing from this package — a test parses its
imports to keep that true, because a fallback that needs the thing that is down
is not a fallback.

---

## 5. Review cadence and the honesty metrics

`research_workspace/metrics.py`, `research_workspace/jobs.py`.

- `review_queue()` — theses past `next_review_at`. Spec M ranks by position
  size × staleness; position size is Spec L's ledger (a parallel phase), so this
  ranks by staleness alone and says so rather than inventing a size.
- Dossier sections older than `RESEARCH_SECTION_STALE_DAYS` (default 90) are
  marked `stale` in every response that includes them. Marked, never hidden.
- `quarterly_honesty_report()` prints, whether or not it flatters: the hit rate
  over resolved theses, the median time to invalidation, the split between
  theses that were **invalidated** and theses that were **quietly abandoned**,
  and the share of closed decisions whose journal entry matched the actual exit
  reason.
- **Brier** over resolved theses, scored on `original_probability`, with the
  Murphy decomposition `BS = reliability − resolution + uncertainty`. The
  decomposition bins by *distinct stated probability* — that is what makes the
  identity hold exactly — and reports the residual so the split is checkable.
  Coarse or decile buckets are shown alongside as the display view. Reference
  point: superforecasters score roughly 0.20–0.25.
- **Floors.** Below ten resolved theses the Brier prints `insufficient`. The
  calibration table waits for forty, and uses three coarse buckets (≤40%,
  40–60%, ≥60%) below a hundred. A score over four resolutions is noise wearing
  a decimal point.

---

## 6. How an agent is expected to use the tools

The five tools on the workspace MCP surface (`workspace/research_tools.py`),
registered only when the flag is on:

| Tool | Scope | Use |
| --- | --- | --- |
| `research_get` | `read` | **First**, before answering anything about a ticker. |
| `research_search` | `read` | Has this view already been tested and rejected? |
| `research_write` | `research:write` | A dossier section, a thesis, an invalidator, or a question. |
| `thesis_review` | `research:write` | A verdict: `hold`, `weakened`, `invalidated`. |
| `journal_append` | `research:write` | Any decision, including the decision to pass. |

The loop (Spec M §7, Spec P §3):

1. **Recall** — `research_get`. Never re-derive what is recorded; never
   contradict it silently. If new evidence conflicts, say so and write a
   revision.
2. **Gather** — the Spec O planes and Spec N cohorts. Sourced facts only.
3. **Attack** — the `thesis-critic` subagent. Its output is stored as the bear
   case, attributed, never merged into the bull argument.
4. **Write the invalidators first.** Before the position, always. One added
   afterwards is flagged `post_hoc` and does not count towards the track record.
5. **Record** — `research_write` the delta with sources, `journal_append` the
   decision.

`research_write` with `kind="thesis"` and `status="active"` runs the gate and
refuses with the checklist. `journal_append` with `budget="evidenced"` requires
a cited cohort answer that is `depth="full"` with `status="ok"`; a `quick`,
`insufficient` or `inconclusive` answer is refused, and the decision may still
be recorded as `discretionary` (Spec L §6.6). Until Phase 3 registers a cohort
answer resolver (`research_workspace/citations.py`), *no* citation can be
verified, so every decision recorded now is honestly `discretionary`.

### What these tools cannot do

- Place, modify, or cancel a broker order. There is no `execute` scope, no such
  tool, and no import path from this package to a broker adapter.
- Produce a statistic. Every number here was written by a human, quoted from a
  cited source, or computed by `comparables/`.
- Close a position. A triggered invalidator surfaces that the reason for
  holding may be gone. Acting on that is the owner's decision.
