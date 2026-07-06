# Langfuse Observation-Level Addendum - 2026-07-04 Audit

- No new P0 findings from observation-level Langfuse data; no traces or observations exist after the 2026-07-04 16:35 UTC restart.
- New P2 telemetry findings: Gemini generations are absent from Langfuse, so screener/research/pattern-discovery costs are invisible there; the initially-reported Sonnet "off-config drift" was a false positive (see corrected P2-LF-2 — prod matches deployed config; sonnet-5 exists only in uncommitted Codex edits).
- BRY-243 corpus accounting is ambiguous: 22 scoring-tagged scheduled Opus generations exist, plus 7 scoring-shaped ad-hoc Opus generations without the `scoring` tag.

## Scope And Method

- Read `docs/audits/2026-07-04-system-audit.md` first and treated P0-2, P0-4, and P1-1 as baseline, not new findings.
- Used the repo REST client `evals/langfuse_api.py`; no Langfuse SDK. Env was loaded inside temp scripts from `~/.env` for keys and repo `.env` for `LANGFUSE_BASE_URL`.
- Verified auth with raw `_get("/api/public/traces", ...)`, then sampled `/api/public/observations` from `2026-06-01T00:00:00Z` to now. Result: 172 traces and 172 generation observations, within the requested cap. Trace detail endpoint was spot-checked for evidence rows.
- All cost/token rollups below are Langfuse-visible Claude observations only. Gemini calls do not appear in the Langfuse observation set.

## New Findings

### P2-LF-1. Scoring-shaped Opus generations exist without the `scoring` tag

**Evidence:** `evals/build_dataset.py` builds the corpus with `traces_by_tag("scoring", ...)`. Observation-level parsing found 29 Opus outputs shaped like scoring decisions, but only 22 carry `scoring`; the other 7 are `ad_hoc` only. This means the 22/150 BRY-243 count is correct for scheduled-tagged scoring, but understates all scoring-shaped Opus calls in the window by 7/29 (24%).

| Timestamp UTC | Ticker | Observation | Trace | Score | Rec |
| --- | --- | --- | --- | ---: | --- |
| 2026-06-11T21:27:00.963Z | AAPL | `f54418b87a481cf9` | `957f34127804938223b892010b9cba4d` | 0.28 | pass |
| 2026-06-12T16:21:18.458Z | DFTX | `a53f6c9b0d821e29` | `12afbb242692d903a5447e8f7d90c50d` | 0.12 | pass |
| 2026-06-18T07:36:03.058Z | BE | `d3df873e52f8a711` | `68db1d2b6f5b9e11e3269b33dbb57cad` | 0.15 | pass |
| 2026-06-28T17:09:26.383Z | NFLX | `5205ab1eeae135ac` | `1fe5f0ad1e2efa803f5148dd5ff2bbb6` | 0.30 | pass |
| 2026-06-28T23:17:52.409Z | STZ | `7b54b9453ab7bbd4` | `22b497df77574556c85fb72ad4980750` | 0.35 | pass |
| 2026-06-28T23:22:57.740Z | GOOG | `e455ab5e973dd144` | `d28a2c8959d41311eed6571701bbd1a9` | 0.25 | pass |
| 2026-06-30T23:40:34.676Z | STZ | `e2116caae3ec6c6f` | `43aac89e7b8836519dc97220a2be8143` | 0.32 | pass |

**Spec:** Decide whether BRY-243 intentionally excludes ad-hoc scoring. If yes, make that explicit in eval docs and keep the tag filter. If no, tag ad-hoc scoring calls with `scoring` plus `ad_hoc`, or update the corpus builder to include scoring-shaped ad-hoc traces with a separate label.

### P2-LF-2. ~~Production Sonnet calls are off-config~~ CORRECTED: production is on-config; the sonnet-5 default is an uncommitted working-tree change

**Correction (verified 2026-07-04):** `git show HEAD:config/settings.py` defaults `analyst_model`/`discovery_model` to `claude-sonnet-4-6` — matching all 73 production Sonnet observations exactly. The `claude-sonnet-5` default this finding compared against exists only in uncommitted working-tree edits (concurrent Codex session's model work). No drift exists. Actionable residue: when that Codex change lands and deploys, the analyst/discovery tier silently moves to sonnet-5 — worth a deliberate decision/changelog entry rather than a side effect, given PR #18 deliberately gated model swaps on the BRY-243 eval.

**Original evidence (comparison baseline was wrong):** All 73 Sonnet observations used `claude-sonnet-4-6`; working-tree `config/settings.py` defaults `analyst_model` and `discovery_model` to `claude-sonnet-5`. No observed Claude generation is missing usage or cost data.

| Model | Count | Last Seen UTC | Tokens | Cost | Usage Missing | Cost Missing | Config Status |
| --- | ---: | --- | ---: | ---: | ---: | ---: | --- |
| `claude-sonnet-4-6` | 73 | 2026-07-02T16:00:58.276Z | 82,646 | $0.642822 | 0 | 0 | Off-config vs `claude-sonnet-5` default |
| `claude-haiku-4-5-20251001` | 70 | 2026-07-02T16:00:35.931Z | 25,945 | $0.042009 | 0 | 0 | Matches `filter_model` |
| `claude-opus-4-6` | 29 | 2026-07-02T16:02:09.036Z | 108,575 | $1.451115 | 0 | 0 | Matches `scoring_model` |

**Spec:** Check Railway env for `ANALYST_MODEL` / equivalent overrides. Either pin production back to the repo default `claude-sonnet-5`, or document the intentional override so local config, production config, and eval assumptions agree.

### P2-LF-3. Gemini generations are not observable in Langfuse

**Evidence:** Across 172 generation observations, the distinct Langfuse `model` values are only Claude IDs. No `gemini-2.5-flash`, `gemini-3.1-pro-preview`, or other Gemini model IDs appear, even though `config/settings.py` expects Gemini Flash for tier-2 screening and Gemini Pro preview for discovery/web research/pattern event search.

**Impact:** Langfuse model tables and scan cost rollups cannot validate Gemini model drift, Gemini usage/cost, screener truncation rates, or repeated Gemini pattern-discovery calls. This does not change the baseline P0-4/P1-1 findings; it means Langfuse is not the source of truth for those stages.

**Spec:** Add explicit Langfuse observations around Google GenAI calls, or add a lightweight internal LLM call ledger that records provider, model, stage, ticker, latency, token estimate/usage if available, and parse/grounding status.

### P2-LF-4. One resolved Anthropic billing/credit error appears in observations

**Evidence:** Exactly one observation has `level=ERROR`; it is also the only empty generation output. No WARNING-level observations were present.

| Timestamp UTC | Trace | Observation | Stage | Ticker | Model | Cause |
| --- | --- | --- | --- | --- | --- | --- |
| 2026-06-17T22:39:13.387Z | `23cfd0fce3ed505004b82d8ad99b084d` | `b83b9b3edf888c7b` | catalyst/ad-hoc | NVDA | `claude-haiku-4-5-20251001` | Anthropic 400 `invalid_request_error`: credit balance too low |

**Spec:** No production fix appears necessary from Langfuse alone because later Claude calls succeeded. Add provider-credit exhaustion to operational alerting if not already covered.

## Pattern And Analog Observations

No new finding beyond audit P0-2/P1-1. Langfuse adds corroborating evidence only:

- Ten sampled pattern observations across June/July all returned parseable setup-classification or decomposition JSON; none returned final analog rows because the final PatternAgent output is not directly logged as a Langfuse generation.
- The 22 scheduled scoring prompts contained no non-empty analog evidence. On 2026-07-02 all 11 scheduled scoring prompts carried `status: no_matches`, `total_instances: 0`, and empty `most_similar` evidence. On 2026-06-11 all 11 scheduled scoring prompts described "No historical instances found" with no non-empty `most_similar` evidence.
- Exact prompt-hash check found 0 repeated Langfuse-visible pattern/discovery prompts per ticker. This does not clear repeated Gemini discovery spend because Gemini calls are absent from Langfuse.

| Timestamp UTC | Ticker | Observation | Trace | Model | Tokens | Parsed Setup |
| --- | --- | --- | --- | --- | ---: | --- |
| 2026-06-11T21:26:22.642Z | AAPL | `1aa181c444ea494f` | `a021e678fb3d67071e56ec069fb67c92` | `claude-sonnet-4-6` | 485 | product_launch |
| 2026-06-11T21:36:37.633Z | INTC | `08847a4644c132b4` | `d7026371c6d33621ecb27377bb215bb2` | `claude-sonnet-4-6` | 555 | analyst_upgrade_cluster |
| 2026-06-11T21:41:48.653Z | DBI | `6142c0e68c2d3770` | `edd53b292b8378b29a5907b331418070` | `claude-sonnet-4-6` | 507 | m_and_a |
| 2026-06-11T21:49:17.780Z | TMCI | `2f066f12bc5c9791` | `1987cf0853d3fa407fd3ec2fc2e6729c` | `claude-sonnet-4-6` | 468 | general_positive_catalyst |
| 2026-06-18T07:35:12.083Z | BE | `717a9bff37c16026` | `a0d7d46fbfa287aaddc1c15fcd8ce397` | `claude-sonnet-4-6` | 470 | sector_catalyst_positive |
| 2026-06-28T23:22:17.442Z | GOOG | `fab54fecf12f5788` | `4134e65933a2b08fb856f3fe836c5047` | `claude-sonnet-4-6` | 525 | sector_catalyst_positive |
| 2026-07-02T15:40:05.848Z | MSM | `200394ec43109884` | `182a979c5bf67144fab8f2560cdbbc97` | `claude-sonnet-4-6` | 605 | earnings_beat_guide_up |
| 2026-07-02T15:48:47.372Z | PANW | `5010b794cfc7a24e` | `054ff8dd6477354eabfcffd5345b176b` | `claude-sonnet-4-6` | 623 | analyst_upgrade_cluster |
| 2026-07-02T15:56:11.657Z | LNN | `d82f56b7ea5a45d7` | `e78f9e69d2f7ba221d01eeda552a28bd` | `claude-sonnet-4-6` | 582 | earnings_beat_guide_flat |
| 2026-07-02T16:00:58.276Z | HIMS | `a65708e13b208260` | `56fc80dec14f3da360ff0502f6ec732b` | `claude-sonnet-4-6` | 487 | sector_catalyst_negative |

## Scan Cost And Token Rollup

Langfuse-visible Claude spend is stable across the two full scheduled scans. Scoring dominates both cost and tokens; visible pattern-classification/decomposition is under 10% of observed tokens. This table excludes Gemini tier-2, Gemini web research, and Gemini pattern event discovery because those generations are not in Langfuse.

| Scan Date UTC | Tickers | Observations | Tokens | Cost | Dominant Stage | Pattern Token Share | Pattern Cost Share |
| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: |
| 2026-06-11 | 11 | 46 | 75,250 | $0.824142 | scoring ($0.587435, 71.3%) | 7.4% | 3.9% |
| 2026-07-02 | 11 | 41 | 74,093 | $0.806001 | scoring ($0.563025, 69.9%) | 9.7% | 4.9% |

Stage detail:

| Date | Stage | Count | Tokens | Cost | Token Share | Cost Share |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2026-06-11 | catalyst | 21 | 23,675 | $0.185203 | 31.5% | 22.5% |
| 2026-06-11 | pattern | 11 | 5,554 | $0.031710 | 7.4% | 3.9% |
| 2026-06-11 | scoring | 11 | 43,087 | $0.587435 | 57.3% | 71.3% |
| 2026-06-11 | memo | 2 | 2,544 | $0.016356 | 3.4% | 2.0% |
| 2026-06-11 | fundamental | 1 | 390 | $0.003438 | 0.5% | 0.4% |
| 2026-07-02 | catalyst | 14 | 21,673 | $0.182043 | 29.3% | 22.6% |
| 2026-07-02 | pattern | 12 | 7,159 | $0.039225 | 9.7% | 4.9% |
| 2026-07-02 | scoring | 11 | 41,949 | $0.563025 | 56.6% | 69.9% |
| 2026-07-02 | memo | 2 | 2,629 | $0.016263 | 3.5% | 2.0% |
| 2026-07-02 | fundamental | 2 | 683 | $0.005445 | 0.9% | 0.7% |

## Slowest Generations

No new finding: no multi-minute calls or retry storms are visible. The maximum generation duration is 50.7s, and the sampled window has 172 observations for 172 traces.

| Rank | Stage | Ticker | Model | Duration | Observation | Trace |
| ---: | --- | --- | --- | ---: | --- | --- |
| 1 | scoring | DBI | `claude-opus-4-6` | 50.7s | `eeaa3e375f058b9b` | `437b8faeacd7161c229efe641f120754` |
| 2 | scoring | CPNG | `claude-opus-4-6` | 47.3s | `54c8e43a5a164248` | `587fbaa1e5eaec4849f2c6517875b350` |
| 3 | scoring | BBIO | `claude-opus-4-6` | 47.2s | `8e81164504e3cc50` | `7f52047c37785ea3b021aa946d6820a5` |
| 4 | scoring | PANW | `claude-opus-4-6` | 45.6s | `2bff4e0e2e624aa4` | `8f577ca3f2324a39ee9aa2bf0ab9c8a6` |
| 5 | scoring | GH | `claude-opus-4-6` | 45.1s | `f2cf5d6a46a47b37` | `4fd430c4df5f7c916dde2ddb5bf5e026` |
| 6 | scoring | DAL | `claude-opus-4-6` | 45.0s | `2f948ad5f3f821ee` | `0d80522bfa8aa31131aef2e01236c3bb` |
| 7 | scoring | HNGE | `claude-opus-4-6` | 44.9s | `d283c681ff4ddd06` | `5e36a154c398f2f2c90d58716862a367` |
| 8 | scoring | CPRT | `claude-opus-4-6` | 42.5s | `92bac564ad57bbdb` | `3cf75bc648621ba1741cd8361b2d6244` |
| 9 | scoring | LNN | `claude-opus-4-6` | 42.4s | `88d240560ded2ba8` | `c9e176333d1b1563d46ea11cfb59df4b` |
| 10 | scoring | OSCR | `claude-opus-4-6` | 41.7s | `44361766b11347d9` | `3f03ffeac15acd9c514b3436f097fb2b` |

## Explicit No-New-Finding Checks

- **Post-restart fresh-container behavior:** no new findings. `/api/public/traces` and `/api/public/observations` both returned 0 rows at or after `2026-07-04T16:35:00Z`.
- **Warnings and empty outputs:** no new findings beyond P2-LF-4. There are 0 WARNING observations and 1 empty output, the same Anthropic credit error.
- **Usage/cost completeness:** no new findings. All 172 Langfuse generation observations have usage and cost data.
- **Deprecated model IDs:** no new finding from local config comparison. The only observed model drift is `claude-sonnet-4-6` versus the repo default `claude-sonnet-5`; the observed Opus and Haiku IDs match current repo settings.
