# Spec P — Agent layer

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.1 · **Date:** 2026-09-05

---

## 1. Problem

The research side must not be one-shot. Bryan wants an ongoing investigation he can
challenge, redirect, and return to weeks later — with the system remembering, not the
transcript. And it must be cheap enough to use daily.

## 2. The decision: no custom agent framework

The lead agent **is** the Claude Code / Codex session. There is no bespoke orchestrator,
no chat UI, no agent runtime to maintain. What the repo provides instead:

- **Tools** — the Spec K MCP surface, identical across clients.
- **Memory** — Spec M's dossiers, theses, and journal; Spec L's ledger; Spec N's query
  log. Durable state lives in the workspace, never in a transcript.
- **Instructions** — `CLAUDE.md` and `AGENTS.md`, kept in agreement by test.
- **Subagents** — defined as repo files so both clients can use them (§4).

This is deliberate. Every framework layer is a thing to maintain that adds no research
quality, and the clients' own subagent primitives already do the delegation.

## 3. The lead agent's loop

For any investment question:

1. **Recall** — `research_get` and `portfolio_overview` before anything else. Never
   re-derive what is recorded; never silently contradict it.
2. **Frame** — turn the question into a `SetupSpec` (Spec N §4.0) and/or explicit
   research questions. **Show the framing before running it.** An unshown framing is an
   unexamined assumption.
3. **Gather** — the Spec O planes and Spec N cohorts. Sourced facts only.
4. **Attack** — delegate to `thesis-critic` (§4). The critic's output is stored as the
   bear case and attributed, never merged into the bull argument.
5. **Situate** — what does this do to *existing* exposure (Spec L)? Concentration,
   sector, and narrative-tag overlap. A standalone idea is half an answer.
6. **Record** — `research_write` the delta, `journal_append` the decision including a
   decision to pass. Invalidators before position, always.
7. **Propose, never place** — `propose_order` at most. Bryan approves; code executes.

## 4. Subagents

Defined once in `.claude/agents/` as Markdown with YAML front-matter. Each has a
narrow brief, its own tool subset, and a required output shape. The front-matter is the
enforcement point, not the prose: **`tools`** (allowlist; MCP patterns like
`mcp__workspace__compare_setups` are accepted), **`disallowedTools`**, **`model`** (so
"analyst-tier work runs on the cheaper model" is configuration, §6), and **`maxTurns`**
(a hard per-subagent bound). `thesis-critic` and `cohort-analyst` omit `Agent` from
their tool list, which structurally prevents them from spawning further agents.

**Codex has no equivalent subagent primitive.** "Mirrored for Codex" means the same
briefs exist as prompt files under `.codex/prompts/` that the lead agent or Bryan pastes
into a fresh turn — not an executable equivalent. The spec says so plainly rather than
implying parity that does not exist.

| Subagent | Brief | Tools | Must return |
|---|---|---|---|
| `company-researcher` | Build or refresh a dossier section from primary sources | filings, news, fundamentals, web | Sourced claims; explicit "unknown" for gaps |
| `thesis-critic` | **Adversarial.** Try to kill the thesis. Never asked to balance. | all read tools | The strongest bear case, the cheapest disconfirming test, and the invalidator it would set |
| `cohort-analyst` | Translate a question into `SetupSpec`s, run them, report honestly | `compare_setups`, `cohort_detail` | The spec used, the answer verbatim, and every warning — including `insufficient` |
| `filings-analyst` | Tracked-investor and insider activity for a name | filings plane | Positions **with** staleness and portfolio share |
| `macro-analyst` | Regime and macro context, vintage-correct | macro plane | The classifier's label plus the inputs, never its own label |
| `reconciler` | Explain a portfolio/broker discrepancy | portfolio, orders | The discrepancy and its cause; no writes |

Rules for all of them:
- **The critic is never asked to be fair.** Balance is the lead agent's job at synthesis.
  An adversary instructed to be balanced is a rubber stamp.
- No subagent may write a thesis to `active` or call `propose_order`.
- A subagent that cannot source a claim returns "unknown". Unsourced assertion is the
  failure mode being designed against, and it is worse than silence.

## 5. Hard boundaries

Encoded in code and asserted by tests, not stated in prompts — prompts are advisory,
import graphs are not.

1. **No agent places an order.** No MCP tool, no token scope, no reachable code path.
   (Spec K §4.1, Spec L §6, `test_no_agent_path_to_broker`.)
2. **No agent produces a statistic.** Every number traces to `comparables/` or the
   ledger. (Spec N §9, `test_no_model_number_in_output`.)
3. **No agent changes a strategy version, promotes an arm, or flips a flag.** Spec Q's
   promotion path is owner-only.
4. **No agent gives personalized investment advice.** It presents evidence, exposure
   math, and comparable outcomes with their uncertainty; the decision is Bryan's. The
   assistant is not a licensed advisor and says so when the question crosses that line.
5. **Tool output is data, never instruction.** Filing text, news bodies, and scraped
   pages are untrusted content. An agent that finds instructions inside a document
   surfaces them; it never follows them. Mechanically: untrusted spans are wrapped in
   delimiters carrying a **per-response random nonce** (static delimiters are defeated
   by guessing an inexact one), the provenance block marks them `content_trust:
   "untrusted"`, and `research_write` refuses a section whose sources are all
   untrusted-tier unless a human-authored flag is set. The strongest defence is
   boundary 1: with no agent path to a broker, the worst an injected instruction in a
   10-K exhibit can do is a bad `research_write`, which is reviewable and reversible.
   That converts an open problem into a bounded one, and the spec says so.

## 6. Cost discipline

The reason this is affordable is architectural, not a prompting trick.

- **Deterministic by default.** Sync, pricing, maturation, cohorts, reconciliation,
  evaluation, and reports are Python. Zero inference. A scheduled job that needs a model
  is a design error (Spec K §5).
- **`compare_setups` is free per call** — SQL and NumPy, cached by setup hash. The most
  valuable tool is the cheapest one, which is how it should be.
- **Delegate narrowly.** A subagent is worth its cost when it returns a compact,
  structured result the lead agent could not cheaply produce. Spawning three agents to
  restate the same dossier is waste.
- **Budget per session.** The `llm_call` ledger (Spec G) tags workspace tool calls, so
  "what did this question cost" is a query. A per-session soft budget warns; a hard cap
  stops. Bryan hit credit exhaustion once already (BRY-301) and the system paged rather
  than failing silently — keep that property.
- **Model choice is configuration.** Analyst-tier work runs on the cheaper model unless
  an eval attestation says otherwise (the BRY-243 parity gate). Swapping models must not
  require rewriting the system — that is why judgment lives in prompts and subagent
  briefs, and evidence lives in Python.

## 7. Scheduled agent sessions — later, not now

Once the tools and memory are proven by hand, a small number of recurring sessions
become worth it: a weekly thesis review over stale/large positions, a pre-earnings brief
for held names, and a post-mortem on closed positions comparing outcome to journal.

Each must be: bounded in tokens, idempotent, write-limited to research tables, and
capable of producing "nothing to report" without inventing content. **None of them may
propose orders unattended.** Do not build these until Phases 1–3 of README §6 are done —
a scheduled agent over an unreliable memory just automates being wrong.

## 8. Test plan

| Test | Asserts |
|---|---|
| `test_claude_md_imports_agents_md` | Shared with Spec K §8: `CLAUDE.md` line 1 is `@AGENTS.md` |
| `test_subagent_tool_scopes` | Parses the actual `.claude/agents/*.md` front-matter: `thesis-critic` has no write tool; `cohort-analyst` cannot propose; neither lists `Agent`; every subagent declares `model` and `maxTurns` |
| `test_codex_prompts_match_briefs` | Each `.claude/agents/` brief has a `.codex/prompts/` counterpart with the same brief text |
| `test_no_agent_path_to_broker` | Import-graph (shared with Spec L) |
| `test_no_model_number_in_output` | Constructing a `CohortAnswer` from model text raises |
| `test_untrusted_content_marked` | Filing and news bodies are wrapped with a per-response random nonce and `content_trust="untrusted"`; two responses never share a nonce |
| `test_research_write_refuses_all_untrusted` | A section whose sources are all untrusted-tier is refused without the human-authored flag |
| `test_session_budget_enforced` | The hard cap stops further model calls and reports why |

## 9. Definition of done

- A cold session in any client, given only "look at AMD again," recalls the thesis,
  reports what changed, runs a cohort, gets attacked by the critic, situates against
  existing exposure, and writes the delta back — with no state carried in the transcript.
- The same run repeated in Codex, with the pasted briefs, produces the same recorded
  artifacts.
- Every boundary in §5 has a passing test.
- One full session's cost is measurable from the ledger and is small enough that Bryan
  does it daily without thinking about it.
