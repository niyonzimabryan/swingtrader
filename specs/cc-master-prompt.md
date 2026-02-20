# Master Prompt for Claude Code

Copy everything below this line into CC:

---

I have a swing trading agent system in this repo. The `.env` is fully configured with API keys for Anthropic, Alpaca (paper), Finnhub, FMP, FRED, and Telegram. The system runs and produces memos via Telegram, but there are bugs and a major feature to build.

Read `todoscratchpad.md` for current status, `swing-trader-prd.md` (especially the new Section 14) for design decisions, and `specs/pattern-agent-spec.md` for the Pattern Agent build spec.

Please work through the following in order:

## Phase A: Bug Fixes (do all of these first)

### A1. Direction Logic (highest priority)

The system always outputs "SHORT" in memo headers even when the thesis is bullish. Three files need changes:

**`scoring/engine.py` line 63:** `primary_direction = catalyst.direction` — when catalyst direction is `neutral` or `ambiguous`, this poisons everything downstream.
- Fix: Derive `primary_direction` from the highest-confidence non-neutral signal across all agents. Check catalyst first (highest weight), then fundamental, then pattern, then sentiment. If none have a non-neutral direction, default to `"bullish"` (Phase 1 is long-only per PRD).
- Normalize direction values: treat `"ambiguous"` as `"neutral"` everywhere.

**`scoring/engine.py` lines 64-67:** The direction penalty counts `"ambiguous"` as a disagreement.
- Fix: In the disagreement counter, also exclude `d == "ambiguous"` alongside `d != "neutral"`. Both mean "no opinion."

**`memo/generator.py` line 66:** `"direction": "long" if catalyst.direction == "bullish" else "short"`
- Fix: Use `scoring_result["direction"]` instead of `catalyst.direction`. Map: `"bullish"` → `"long"`, `"bearish"` → `"short"`, anything else → `"long"` (Phase 1 default).

### A2. Catalyst Confidence Display

Memos show `?` for catalyst confidence. The `AgentOutput` object has the confidence value but it's not reaching the memo formatter.
- Check `bot/formatters.py` and `memo/templates/ic_memo.py` — find where confidence is rendered and ensure it reads from `memo_data["catalyst"]["confidence"]` or equivalent. The catalyst agent sets confidence on the AgentOutput and also in raw_data.

### A3. Scoring Weights

**`scoring/weights.py`** — Update weights for Phase 1 with Pattern Agent being built:

```python
SIGNAL_WEIGHTS = {
    "catalyst": 0.40,
    "fundamental": 0.30,
    "pattern": 0.22,
    "sentiment": 0.08,
}
```

This gives real weight to the agents that produce actual signal (catalyst + fundamental + pattern once built) while keeping Reddit sentiment low since it's still stubbed.

## Phase B: Build the Pattern Agent

Read the full spec at `specs/pattern-agent-spec.md`. This is the biggest piece of work. Summary:

When the system generates a trade thesis, the Pattern Agent should:
1. **Classify the setup** (Sonnet call) — categorize the thesis into a standardized setup type (e.g., "earnings_beat_guide_up", "insider_cluster_buy")
2. **Search for historical instances** — use FMP earnings surprises, insider trading data, etc. for the same ticker + peers from `config/peers.py`
3. **Compute forward returns** — use yfinance to get T+5, T+10, T+15, T+20 returns from each historical event date
4. **Compute summary statistics** — win rate, median return, avg winner/loser, max drawdown
5. **Interpret via Sonnet** — send stats to Sonnet for 2-3 sentence interpretation
6. **Return AgentOutput** — score based on win rate × sample size confidence × risk/reward quality

Implementation order:
1. Add `HistoricalPattern` model to `database/models.py` (see spec for schema)
2. Create `data/pattern_data.py` — data adapter for FMP historical events + yfinance returns
3. Rewrite `agents/pattern_agent.py` — replace the 37-line stub with full implementation per spec
4. Update `memo/templates/ic_memo.py` — render historical pattern stats (instances found, win rate, median return, drawdown) in the HISTORICAL PRECEDENT section of memos
5. Cache aggressively — historical earnings surprises don't change, fetch once and store in SQLite

The spec has full details on setup type taxonomy, scoring logic, edge cases, and the database table schema.

## Phase C: Verify Everything Works

After both phases:
1. Run `/test NVDA` (should have rich earnings history for pattern matching)
2. Run `/test AAPL`
3. Run `/test MSFT`
4. Verify: direction shows correctly (LONG for bullish theses), confidence values display, pattern data appears in memos, scores are higher than before (should be possible to cross 0.55 threshold now)

## Important Notes

- Do NOT execute any trades on Alpaca — analysis and memos only for now
- Pattern Agent and Reddit Sentiment Agent are intentionally different: Pattern is being built now, Reddit stays stubbed (returns 0.5)
- If memo threshold (0.55) is still too high to see output after fixes, temporarily lower it in settings to 0.45 for testing, but note this in the code with a TODO comment
- All changes should be tested by sending `/test` commands via Telegram and verifying the memo output
