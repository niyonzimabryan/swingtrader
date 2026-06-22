# Goal Prompt: Historical Pattern Analysis Robust Fix

Use this prompt to kick off implementation:

```text
Implement the robust Historical Pattern Analysis/Event Analog Engine described in /Users/bryanniyonzima/AppsinTesting/swingtrader/specs/historical-pattern-analysis-robust-fix.md.

Start by reading:
- /Users/bryanniyonzima/AppsinTesting/swingtrader/.claude/napkin.md
- /Users/bryanniyonzima/AppsinTesting/swingtrader/todoscratchpad.md
- /Users/bryanniyonzima/AppsinTesting/swingtrader/specs/historical-pattern-analysis-robust-fix.md
- /Users/bryanniyonzima/AppsinTesting/swingtrader/agents/pattern_agent.py
- /Users/bryanniyonzima/AppsinTesting/swingtrader/config/peers.py
- /Users/bryanniyonzima/AppsinTesting/swingtrader/data/pattern_data.py
- /Users/bryanniyonzima/AppsinTesting/swingtrader/database/models.py
- /Users/bryanniyonzima/AppsinTesting/swingtrader/utils/web_search_client.py

Implement in phases from the spec:
1. Safety/settings/secrets hygiene.
2. Peer resolver with cached FMP-first peer fallback.
3. Historical event, event outcome, event context, and pattern search run schema.
4. Gemini event discovery/extraction.
5. Perplexity Search API fallback only, not Perplexity Agent API.
6. Analog ranking and PatternAgent integration behind pattern_analog_engine_enabled.
7. Memo/scoring updates and tests.
8. Backfill/bakeoff scripts for recent failures and known catalyst examples.

Do not route unstructured catalysts to earnings beats. Emit honest pattern statuses. Keep provider calls cached and capped. Preserve existing legacy earnings behavior until the new engine is verified. Do not commit or log API keys.
```
