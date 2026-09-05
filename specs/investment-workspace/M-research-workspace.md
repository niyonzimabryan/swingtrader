# Spec M — Research workspace

**Series:** [Investment Workspace K–Q](README.md) · **Status:** Draft v0.1 · **Date:** 2026-09-05
**Flag:** `RESEARCH_WORKSPACE_ENABLED=false`

---

## 1. Problem

Every research conversation today is disposable. The reasoning behind a position lives
in a chat transcript that no future session can read, so each session restarts from
zero, re-derives the same facts, and cannot tell whether a view has already been tested
and rejected. Worse, there is no record of *what would change our mind* — which is the
only part of a thesis that makes it falsifiable rather than a story.

## 2. Outcome

Durable, sourced, revisitable research that an agent session picks up mid-thought:

- **Dossiers** — what we know about a company, with sources and timestamps.
- **Theses** — what we believe, why, what the position is, and **what would invalidate
  it**, with explicit invalidation triggers that a scheduled job can actually check.
- **Decision journal** — what was decided, when, on what evidence, and what happened.
- All of it queryable through Spec K's tools and readable as Markdown in the repo.

## 3. Domain model

### `dossiers`
One per ticker. Structured metadata (sector, business model summary, revenue drivers,
key customers, competitive position, capital structure) plus versioned free-text
sections. Every section carries `updated_at`, `updated_by` (`human` / model id), and a
`sources` list. A section with no source is `unsourced=true` and rendered with a warning
everywhere it appears.

### `dossier_sections`
Append-only revisions: `dossier_id`, `section_key`, `body_md`, `sources_json`,
`author`, `supersedes_id`, `created_at`. Nothing is ever overwritten. "What did we
believe in July" is a query.

### `theses`
- ticker, title, one-sentence claim
- direction and intended horizon
- the **argument**: numbered claims, each with evidence references
- the **bear case**, required and non-empty — a thesis without one is `draft`
- **invalidators** (§4) — the falsifiable part
- status: `draft` → `active` → `weakened` → `invalidated` → `closed`
- linked positions from Spec L, linked cohort answers from Spec N
- `next_review_at`

### `thesis_invalidators`
The load-bearing table. Each row: a description, a **type**, and where machine-checkable,
the parameters to check it. Types:

| Type | Example | Checkable |
|---|---|---|
| `metric_threshold` | "gross margin falls below 45% for two quarters" | yes, from fundamentals |
| `price_level` | "closes below $82 for three sessions" | yes |
| `event` | "guidance cut" / "CFO departure" / "13D exits" | yes, from Spec O planes |
| `time_decay` | "no re-rating within two quarters" | yes |
| `qualitative` | "the AI capex narrative breaks" | no — flagged for human review |

A scheduled job evaluates every checkable invalidator daily and, on a trigger, moves the
thesis to `weakened` and pages. **The system never auto-closes a position** — it
surfaces that the reason for holding may be gone, which is Bryan's decision to act on.

### `decision_journal`
Append-only: date, ticker(s), decision (`opened`/`added`/`trimmed`/`closed`/`passed`),
the thesis at the time (hash), the cohort evidence at the time (hash), position sizing
rationale, expected holding period, and — filled in later by a job — what actually
happened. This is the input to "am I any good at this", which no amount of backtesting
substitutes for.

### `research_questions`
Open questions with status. An agent that hits something it cannot resolve writes it
here instead of guessing. A future session's first move is to read the open questions.

## 4. Invalidator discipline

Three rules, because this is where the value is:

1. **A thesis with no invalidator cannot leave `draft`.** Enforced by a database check
   and by `research_write` refusing the transition.
2. **At least one invalidator must be machine-checkable.** A thesis that can only be
   disproved by a human's change of mood is a feeling.
3. **Invalidators are set before the position, never after.** The table records
   `created_at` and the linked position's open date; an invalidator added after entry is
   flagged `post_hoc=true` and excluded from the honesty metrics in §6.

## 5. The Markdown mirror

Every dossier and thesis is written to the repo as well as Postgres:

```text
research/
  companies/
    AMD.md              dossier: sections, sources, last-updated
  theses/
    2026-09-amd-mi400-ramp.md
  journal/
    2026-09.md
  questions.md
```

Front-matter carries `thesis_id`, `status`, `next_review_at`, `invalidators`, and a
`generated_at` stamp with a "do not hand-edit; use `research_write`" banner. Direction
is Postgres → repo. `scripts/sync_research_mirror.py --import` exists for the one case
where Bryan edits Markdown directly, and it round-trips through validation.

Why both: Postgres gives querying, staleness, and joins. Git gives durability,
diffability, review, and an offline fallback that works when the API is down (Spec K
§3.3). Neither alone is sufficient.

## 6. Review cadence

A scheduled job, no inference:

- Theses past `next_review_at` are surfaced in the Sunday report and via
  `research_get`, ranked by position size × staleness.
- Any triggered invalidator pages immediately.
- Dossier sections older than a configurable horizon (default 90 days) are marked
  `stale` in every response that includes them.
- **Honesty metrics**, computed quarterly and printed whether or not they flatter:
  hit rate on active theses, median time-to-invalidation, share of theses that were
  invalidated versus quietly abandoned, and share of closed positions whose journal
  entry matched the actual exit reason. The last one measures whether the process is
  real or decorative.

## 7. How an agent uses it

The workflow written into `CLAUDE.md` and `AGENTS.md`:

1. `research_get` before answering anything about a ticker. Never re-derive what is
   already recorded; never contradict it silently — if the new evidence conflicts,
   say so and write a revision.
2. Gather new evidence through the Spec O planes and Spec N cohorts, never from
   unsourced recall.
3. `research_write` the delta, with sources. Model-authored sections are stamped with
   the model id and are visibly distinct from human-authored ones.
4. When forming or changing a view, write the invalidators **first**.
5. `journal_append` on any decision, including the decision to pass.

An adversarial pass is a first-class step, not an optional flourish: Spec P defines a
`thesis-critic` subagent whose only job is to attack the thesis using the same tools.
Its output is stored as the bear case, attributed, and never silently merged into the
bull argument.

## 8. Test plan

| Test | Asserts |
|---|---|
| `test_thesis_requires_invalidator` | `draft` → `active` is refused without one |
| `test_requires_machine_checkable` | The transition is refused if all invalidators are `qualitative` |
| `test_post_hoc_invalidator_flagged` | One added after the position opens is marked and excluded from metrics |
| `test_sections_are_append_only` | An update creates a revision; the prior body is still readable |
| `test_unsourced_is_flagged` | A section with no source renders with the warning in every surface |
| `test_invalidator_trigger_pages` | A crossed `price_level` moves the thesis to `weakened` and pages |
| `test_never_auto_closes` | A triggered invalidator creates no order and no proposal |
| `test_mirror_roundtrip` | Postgres → Markdown → `--import` → Postgres is lossless |
| `test_offline_read` | With the API down, the Markdown alone answers thesis and invalidators |

## 9. Definition of done

- Three real theses exist with invalidators set, mirrored to the repo, and reviewable.
- A deliberately tripped invalidator pages and moves status without touching a position.
- A new session, given only the repo, can state Bryan's current view on a held name and
  what would change it.
- The quarterly honesty metrics render, including the unflattering ones.
