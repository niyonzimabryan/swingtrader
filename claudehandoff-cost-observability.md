# Claude Handoff - API Cost Investigation & Langfuse Observability

Date: 2026-02-22
Repository: `/Users/bryanniyonzima/Downloads/AppsinTesting/swingtrader`

## 1) Problem Statement

Operator noticed high API spend on Feb 22, 2026:

| Model | Daily Cost | % of Total |
|-------|-----------|------------|
| Claude Sonnet 4.6 | $15.51 | 75% |
| Claude Sonnet 4 | $2.33 | 11% |
| Claude Opus 4.6 | $1.81 | 9% |
| Claude Haiku 4.5 | $1.15 | 5% |
| **Total** | **$20.80** | |

Additional symptoms:
- Morning pre-market scan took ~80 minutes
- 2 of 3 daily scans returned zero memos (no tickers passed threshold)
- No visibility into per-run costs — Railway logs vanish on redeploy

## 2) Root Cause Analysis

### Why Sonnet 4.6 is 75% of spend ($15.51/day)

Sonnet is called across **6 pipeline stages** for every ticker that advances past Haiku pre-screen:

| Stage | Sonnet Calls/Scan | Token Profile | Why Expensive |
|-------|-------------------|---------------|---------------|
| **Web Research** | 10-25 tickers | Multi-round tool-use (web_search_20250305). Each call triggers 2-3 API rounds where full conversation history + search results are resubmitted. 10 max searches × accumulated context = massive token volume. | Biggest single cost driver. |
| **Catalyst Analysis** | 30-50 tickers | 7K input (2K system + 5K catalyst text), 2K output (structured risk/materiality JSON) | High volume: every ticker passing Haiku threshold 3. |
| **Pattern Agent** | 20-50 tickers × 2 | Two Sonnet calls per ticker: setup classification + historical interpretation. Each ~2.5K input. | 2x multiplier per ticker. |
| **Discovery** | 1 per scan | Sonnet + web_search + 10K thinking tokens (was). System prompt + regime context → web search → JSON ticker list. | Thinking tokens were $1+/scan for marginal benefit. |
| **Fundamental** | 10-25 tickers | Peer narrative generation. Smallest per-call cost (~1.5K tokens). | Low individual cost, moderate volume. |
| **Memo Generation** | 5-15 memos | Thesis + bear case synthesis from all agent outputs. ~2.5K input, 700 output. | Only runs for tickers above threshold. |

### Why 80-minute scan duration

- 503 S&P 500 tickers processed sequentially in the main loop
- Each ticker: news fetch → Haiku pre-screen (up to 10 candidates) → potential Sonnet escalation
- Tickers passing catalyst gate trigger parallel post-catalyst agents (fundamental + pattern + web_research) + Opus scoring
- A ticker hitting the full pipeline takes 2+ minutes (Opus thinking alone: ~20s)
- With 503 tickers and ~25-50 hitting full pipeline: 50-80 minutes is expected

### Why 2/3 scans return zero memos

The scoring chain is genuinely conservative:
1. Catalyst must score >= 0.3 (materiality × 0.7 + direction_confidence × 0.3)
2. Composite score from 4 agents must exceed 0.55 memo threshold
3. Opus evaluation can only adjust ±0.30 from raw score

On quiet market days, tickers land at 0.47-0.53 (just below threshold). Full pipeline cost is incurred but no memo is generated.

### Sonnet 4 spend ($2.33) — not from SwingTrader

All code references `claude-sonnet-4-6`. The `SONNET_FALLBACK` constant in `anthropic_client.py` is set to `claude-sonnet-4-6`. The $2.33 on "Claude Sonnet 4" is from another source sharing the same API key (likely Claude Code sessions).

### Opus spend ($1.81) — NOT the cost problem

Opus with 16K thinking tokens costs ~$0.06-0.10 per evaluation. With ~20-30 Opus calls/day across 3 scans, $1.81 is expected and reasonable. Opus is the final quality gate — thinking tokens have the highest ROI here.

## 3) What was implemented

### A. Langfuse OTEL Auto-Instrumentation

**Purpose:** Structured observability for every LLM call — model, tokens, cost, latency — grouped by scan session and pipeline stage.

**How it works:**
- `opentelemetry-instrumentation-anthropic` auto-wraps every `client.messages.create()` call at the SDK level
- Zero changes to `anthropic_client.py` — instrumentation is transparent
- `langfuse.propagate_attributes()` context managers in `pipeline.py` add session IDs and stage tags
- Graceful no-op if Langfuse keys are not set (no import errors, no crashes)

**Files changed:**

| File | Change |
|------|--------|
| `requirements.txt` | Added `langfuse>=3.0`, `opentelemetry-instrumentation-anthropic>=0.1` |
| `main.py` | Added `_init_langfuse()` at startup (sets env vars, instruments SDK, returns client). Added `langfuse_client.flush()` on shutdown. |
| `config/settings.py` | Added `langfuse_public_key`, `langfuse_secret_key`, `langfuse_base_url` |
| `.env.example` | Added Langfuse key placeholders |
| `orchestrator/pipeline.py` | Added `_langfuse_context()` helper + session/tag wrapping (see below) |

**Trace grouping in pipeline.py:**

- `run_full_scan()` → session `scan-YYYYMMDD-HHMMSS`, tag `scheduled_scan`
  - Discovery → tag `discovery`
  - Per-ticker catalyst → tags `catalyst`, `{ticker}`
  - Post-catalyst agents → tags `fundamental`/`pattern`/`web_research`, `{ticker}`
  - Scoring → tags `scoring`, `{ticker}`
  - Memo → tags `memo`, `{ticker}`
- `run_ad_hoc()` → session `adhoc-{ticker}-YYYYMMDD-HHMMSS`, tag `ad_hoc`

### B. Discovery Thinking Budget Reduction

**Change:** `discovery_thinking_budget` default: 10000 → 0

**Rationale:** Discovery's job is web search + filter for tickers with catalysts. Quality comes from the search results, not from extended thinking. The 10K thinking tokens added ~$1/day across 3 scans for marginal improvement in ticker selection.

**Risk:** Minimal. Discovery still runs Sonnet + web_search tool with full search capability. Only the "think deeply before/after searching" step is removed.

## 4) What was NOT changed (and why)

### Web research `max_searches` (stays at 10)

Reducing from 10 to 5 would directly reduce research quality — fewer sources found across the 5 dimensions (catalyst context, competitive dynamics, management signals, bull/bear debate, institutional positioning). With Langfuse now in place, we can observe how many searches the model actually uses per call. If average is 4-5, lowering the cap has no effect. **Decision deferred until data is available.**

### Opus thinking budget (stays at 16000)

Opus is only $1.81/day — not the cost problem. The 16K thinking budget enables genuine stress-testing of trade theses. Since Opus is the final quality gate before memo generation, this is where thinking tokens have the highest ROI.

### Memo threshold (stays at 0.55)

Lowering would generate more memos but at lower average quality. The 2/3 empty scans are a feature, not a bug — the system correctly rejects low-conviction ideas.

### Universe size (stays at 503)

Reducing the S&P 500 universe would save Haiku pre-screening cost (~$1.15/day) but would miss potential catalysts. The real cost is in post-Haiku stages, not Haiku itself.

## 5) New env vars

| Var | Value | Where |
|-----|-------|-------|
| `LANGFUSE_PUBLIC_KEY` | `pk-lf-...` | .env + Railway |
| `LANGFUSE_SECRET_KEY` | `sk-lf-...` | .env + Railway |
| `LANGFUSE_BASE_URL` | `https://us.cloud.langfuse.com` | .env + Railway |

## 6) How to use Langfuse dashboard

After deployment, go to https://us.cloud.langfuse.com and look for:

1. **Sessions view** — Each scan run appears as a session (e.g., `scan-20260223-120000`). Click to see all API calls in that scan.
2. **Filter by tags** — Use tags like `catalyst`, `web_research`, `scoring` to see cost/latency by stage.
3. **Model cost breakdown** — Dashboard shows per-model spend matching the Anthropic console.
4. **Key questions to answer:**
   - How many web searches does the model actually use per call? (If avg is 4-5, max_searches=10 is already optimal)
   - Which stage consumes the most tokens per scan?
   - How many tickers run the full pipeline but score below 0.55? (Wasted spend)
   - Are Opus calls falling back to Sonnet? (Look for traces where Opus and Sonnet appear for the same ticker)

## 7) Cost optimization roadmap (data-driven, pending Langfuse insights)

| Priority | Optimization | Expected Savings | Trigger |
|----------|-------------|-----------------|---------|
| 1 | Reduce web_search max_uses if avg < 5 | $2-5/day | Langfuse shows avg searches/call |
| 2 | Use Haiku for pattern interpretation (2nd call) | $1-2/day | Pattern interpret call is small/deterministic |
| 3 | Skip catalyst for tickers with zero news (pre-filter before Haiku) | $0.50-1/day | Many tickers have no news in 48h window |
| 4 | Cache same-day web research by catalyst type | $1-3/day | Same catalyst type researched for multiple tickers |
| 5 | Batch pattern similarity scoring | $1-2/day | Currently 2 calls per ticker, could batch |

## 8) Rollback instructions

**Disable Langfuse (no code revert):**
- Remove `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` from Railway env vars
- Bot will start without Langfuse (graceful no-op)

**Restore discovery thinking:**
- Set `DISCOVERY_THINKING_BUDGET=10000` in Railway env vars (overrides the code default of 0)

**Full code revert:**
- Revert the 5 changed files listed in Section 3
- Remove `langfuse` and `opentelemetry-instrumentation-anthropic` from `requirements.txt`
