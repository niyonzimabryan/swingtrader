# Swing Trader — Public Project Ledger

> Public-safe running list of release tasks, follow-ups, and ideas.
> Private operator notes should live outside the tracked repo.

> **2026-07-04 system audit:** findings + evidence in
> `docs/audits/2026-07-04-system-audit.md`; remediation specs in
> `specs/audit-2026-07-04/` (A–G). P0/P1 packages A–D shipped (PRs #21–24),
> P2 hygiene E in review (PR #25). This ledger reflects post-remediation state.

## Open Prioritized List

<!-- Nightly reconcile 2026-09-01 (evidence-only; no swingtrader tracker mutated):
     * ZERO commits on any swingtrader ref since the 08-31 scan. origin/main still `f4d8e9f`
       (2026-07-14). Last PR merge was #37 on 2026-07-15 — now **FORTY-EIGHT DAYS**, still the
       longest no-code-movement gap in the fleet. Zero open PRs. Tonight's movement was all
       **top5**: five PRs merged 08-31 18:11..18:42Z (#130, #131, #132, #122, #114).
     * NOTED FOR THE FIRST TIME: the tip of origin/main, `f4d8e9f` ("docs: SYSTEM_CAPABILITIES +
       PRD product-direction refresh"), was a **direct push to main with no PR** (Bryan,
       2026-07-14 23:37 EDT; `gh api .../commits/f4d8e9f/pulls` returns empty). Docs-only and
       carries no Linear id, so nothing is stuck In Review because of it — recorded, not mutated,
       per the standing rule that direct-push work is flagged rather than auto-closed.
     * Checkout on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` unchanged and still the sharpest local risk: worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb` @ `3eb1878`, a branch already contained in
       origin/main, holding **10 modified files plus 2 stashes** that are on no remote. Re-counted
       tonight. **Needs Bryan: commit or discard.**
     * Third worktree still parked outside the repo tree:
       `~/Documents/Codex/2026-07-27/realtime-voice-chat/swingtrader-strategy-lab-spec` on
       `codex/strategy-lab-spec` @ `f4d8e9f` (level with main, clean).
     * swingtrader Linear unchanged: BRY-272/274/276/279/289 still Todo, untouched since 2026-07-08;
       BRY-60/96/99/101/103/236/238 still Backlog. BRY-91 / BRY-104 / BRY-21 still parked in
       **Duplicate** — needs Bryan: close them or fold them into their survivors. BRY-306 (Canceled)
       vs BRY-98 (Done, archived 2026-07-08) remains a deliberate de-dupe.
     * LINEAR CREATE-HALF DEAD, DAY 21 (newest issue `BRY-308`, 2026-08-11). Team BRY: 199
       non-archived — 137 Done, 33 Todo, 18 Canceled, 7 Backlog, 3 Duplicate, **zero In Progress**,
       one In Review (BRY-109, watchthis). Under `linear-kanban` v2 there is no sync to blame:
       issues are created directly now, and nobody has created one in three weeks.
     * Ledger now **+572 / -1** uncommitted before tonight's block (25 blocks, 07-29 .. 09-01);
       last commit touching the file is `1e30d5b` from 2026-07-14. This repo is clean and on main,
       so committing the ledger is a one-line fix and would cost nothing. -->

<!-- Nightly reconcile 2026-08-31 (evidence-only; no swingtrader tracker mutated):
     * ZERO commits on any swingtrader ref since the 08-30 scan. origin/main still `f4d8e9f`
       (2026-07-14). Last PR merge was #37 on 2026-07-15 — now **FORTY-SEVEN DAYS**. Zero open PRs.
       Still the longest no-code-movement gap of any repo scanned. All fleet movement tonight was
       sentinel's: six PRs merged 08-30 06:16Z .. 08-31 04:20Z (#72-#77, the v15 refocus).
     * Checkout on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` unchanged and still the sharpest local risk: worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb` @ `3eb1878`, a branch already contained in
       origin/main, holding **10 modified files plus 2 stashes** that are on no remote. Re-counted
       tonight. **Needs Bryan: commit or discard.**
     * Third worktree still parked outside the repo tree:
       `~/Documents/Codex/2026-07-27/realtime-voice-chat/swingtrader-strategy-lab-spec` on
       `codex/strategy-lab-spec` @ `f4d8e9f` (level with main, clean).
     * LINEAR — the clean split finally has proof. The create half is still dead (newest issue
       `BRY-308`, 2026-08-11, **20 days**), but the GitHub-integration half ran a full lifecycle
       unattended tonight: `BRY-277` (sentinel) went Todo -> In Progress (08-30 06:09:38Z, PR #73
       opened) -> **Done** (08-30 06:16:16Z, PR #73 merged). No human, no sync script. Team-wide:
       199 non-archived, 44 open, **zero In Progress**, one In Review (BRY-109). `t_91c02f58`.
     * swingtrader Linear unchanged: BRY-272/274/276/279/289 still Todo, untouched since 2026-07-08.
       BRY-91 / BRY-104 / BRY-21 still parked in **Duplicate** — needs Bryan: close them or fold them
       into their survivors. BRY-306 (Canceled) vs BRY-98 (Done, archived 2026-07-08) remains a
       deliberate de-dupe.
     * Gateway dead, **day 9** (log frozen 2026-08-22 09:18, both launchd jobs PID `-`, cron Next-run
       8 days in the past, Last run errored on the missing `google_api.py`). Re-confirmed: no crontab
       at all, no launchd plist for `sync_kanban_to_linear.py`. `t_0297cbbb`.
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * Ledger now **+538 / -1** uncommitted (24 nightly blocks, 07-29 .. 08-31); last commit touching
       the file is `1e30d5b` from 2026-07-14. `t_a8a3d3bb` — scope shrank tonight from four
       scratchpads to three, because the sentinel session committed sentinel's ledger inside its own
       PRs. This repo is clean and on main, so the same fix here is a one-line commit. -->

<!-- Nightly reconcile 2026-08-30 (evidence-only; no swingtrader tracker mutated):
     * ZERO commits on any swingtrader ref since the 08-29 scan. origin/main still `f4d8e9f`
       (2026-07-14). Last PR merge was #37 on 2026-07-15 — now **FORTY-SIX DAYS**. Zero open PRs.
       Still the longest no-code-movement gap of any repo scanned. Fleet-wide the quiet broke
       tonight in **sentinel**: PRs #72 and #73 opened within 90s at ~02:08-02:11 EDT, both
       MERGEABLE, a session mid-flight during this scan.
     * Checkout on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` unchanged and still the sharpest local risk: worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb` @ `3eb1878`, a branch already contained in
       origin/main, holding **10 modified files plus 2 stashes** that are on no remote. Re-counted
       tonight. **Needs Bryan: commit or discard.**
     * Third worktree still parked outside the repo tree:
       `~/Documents/Codex/2026-07-27/realtime-voice-chat/swingtrader-strategy-lab-spec` on
       `codex/strategy-lab-spec` @ `f4d8e9f` (level with main, clean).
     * LINEAR MOVED FOR THE FIRST TIME IN 19 DAYS — but nothing swingtrader. `BRY-277` (sentinel)
       went Todo -> **In Progress** at 2026-08-30 02:09 EDT, written by the **GitHub integration**
       when sentinel PR #73 opened, not by the sync script. So the Linear GitHub app is alive;
       only the Hermes->Linear create cron is missing (`t_91c02f58`). Team-wide tonight: 199
       non-archived, 42 open, **1 In Progress** (BRY-277), 1 In Review (BRY-109); newest issue
       still `BRY-308` (2026-08-11).
     * swingtrader Linear unchanged: BRY-272/274/276/279/289 still Todo, untouched since
       2026-07-08. BRY-91 / BRY-104 / BRY-21 still parked in **Duplicate** — needs Bryan: close
       them or fold them into their survivors. BRY-306 (Canceled) vs BRY-98 (Done, archived
       2026-07-08) remains a deliberate de-dupe.
     * Gateway dead, **day 8** (log frozen 2026-08-22 09:18, both launchd jobs PID `-`, cron
       Next-run 7 days in the past, Last run errored on the missing `google_api.py`). Re-confirmed:
       no crontab at all, no launchd plist for `sync_kanban_to_linear.py`. `t_0297cbbb` unchanged.
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * Ledger now **+504 / -1** uncommitted (23 nightly blocks, 07-29 .. 08-30); last commit
       touching the file is `1e30d5b` from 2026-07-14. `t_a8a3d3bb`. -->

<!-- Nightly reconcile 2026-08-29 (evidence-only; no swingtrader tracker mutated):
     * ZERO commits on any swingtrader ref since the 08-28 scan. origin/main still `f4d8e9f`
       (2026-07-14). Last PR merge was #37 on 2026-07-15 — now **FORTY-FIVE DAYS**. Zero open PRs.
       Still the longest no-code-movement gap of any repo scanned. Fleet-wide the quiet broke
       tonight, but only in **Barber** (PR #7 merged 2026-08-28 19:27 EDT, after last night's scan).
     * Checkout on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` unchanged and still the sharpest local risk: worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb` @ `3eb1878`, a branch already contained
       in origin/main, holding **10 modified files plus 2 stashes** that are on no remote.
       Re-counted tonight. Needs Bryan: commit or discard.
     * Third worktree still parked outside the repo tree:
       `~/Documents/Codex/2026-07-27/realtime-voice-chat/swingtrader-strategy-lab-spec`
       on `codex/strategy-lab-spec` @ `f4d8e9f` (level with main, clean).
     * Linear: BRY-272/274/276/279/289 still Todo, untouched since 2026-07-08. BRY-91 / BRY-104 /
       BRY-21 still parked in **Duplicate** — needs Bryan: close them or fold them into their
       survivors. BRY-306 (Canceled) vs BRY-98 (Done, and **archived** 2026-07-08) remains a
       deliberate de-dupe. Team-wide tonight: 199 non-archived issues, 42 open, zero In Progress,
       one In Review (BRY-109).
     * Gateway dead, **day 7** (log frozen 2026-08-22 09:18, both launchd jobs PID `-`, cron
       Next-run 6 days in the past, Last run errored on a missing `google_api.py`). `t_0297cbbb`
       unchanged; compounds `t_91c02f58`, re-confirmed tonight as having no crontab entry and no
       launchd plist at all.
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * Ledger now **+475 / -1** uncommitted (22 nightly blocks, 07-29 .. 08-29); last commit
       touching the file is `1e30d5b` from 2026-07-14. Tracked as `t_a8a3d3bb`. -->

<!-- Nightly reconcile 2026-08-28 (evidence-only; no swingtrader tracker mutated):
     * ZERO commits on any ref since the 08-27 scan. origin/main still `f4d8e9f` (2026-07-14).
       Last PR merge was #37 on 2026-07-15 — now **FORTY-FOUR DAYS**. Zero open PRs. A fetch of
       all eight remotes tonight moved none of them, so this is a repo-wide quiet night, but this
       is still the longest no-code-movement gap of any repo scanned.
     * Checkout on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` unchanged and still the sharpest local risk: worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb` @ `3eb1878`, a branch already contained
       in origin/main, holding **10 modified files plus 2 stashes** that are on no remote.
       Needs Bryan: commit or discard.
     * Third worktree still parked outside the repo tree:
       `~/Documents/Codex/2026-07-27/realtime-voice-chat/swingtrader-strategy-lab-spec`
       on `codex/strategy-lab-spec` @ `f4d8e9f` (level with main, clean).
     * Linear: BRY-272/274/276/279/289 still Todo, untouched since 2026-07-08. BRY-91 / BRY-104 /
       BRY-21 still parked in the **Duplicate** state — needs Bryan: close them or fold them into
       their survivors. BRY-306 (Canceled) vs BRY-98 (Done) remains a deliberate de-dupe.
       Team-wide tonight: 45 non-completed BRY issues, zero In Progress, one In Review (BRY-109).
     * Gateway dead, day 6 (log frozen 2026-08-22 09:18, both launchd jobs PID `-`, cron Next-run
       5 days in the past). `t_0297cbbb` unchanged; compounds `t_91c02f58`, re-confirmed tonight
       as having no crontab entry and no launchd job at all.
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * Ledger now **+448 / -1** uncommitted (21 nightly blocks, 07-29 .. 08-28); last commit
       touching the file is `1e30d5b` from 2026-07-14. Tracked as `t_a8a3d3bb`. -->

<!-- Nightly reconcile 2026-08-27 (evidence-only; no swingtrader tracker mutated):
     * ZERO commits on any ref since the 08-26 scan. origin/main still `f4d8e9f` (2026-07-14).
       Last PR merge was #37 on 2026-07-15 — now **FORTY-THREE DAYS**. Zero open PRs. With top5
       quiet again tonight, this remains the longest no-code-movement gap of any repo scanned.
     * Checkout on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` unchanged and still the sharpest local risk: worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb` @ `3eb1878`, a branch already contained
       in origin/main, holding 10 modified files plus 2 stashes that are on no remote.
       Needs Bryan: commit or discard.
     * Third worktree still parked outside the repo tree:
       `~/Documents/Codex/2026-07-27/realtime-voice-chat/swingtrader-strategy-lab-spec`
       on `codex/strategy-lab-spec` @ `f4d8e9f` (level with main, clean).
     * Linear: BRY-272/274/276/279/289 still Todo, untouched since 2026-07-08. BRY-91 / BRY-104 /
       BRY-21 still parked in the **Duplicate** state — needs Bryan: close them or fold them into
       their survivors. BRY-306 (Canceled) vs BRY-98 (Done) remains a deliberate de-dupe.
     * BRY-237 Reddit-retirement watch item **RETIRED FROM THE WATCH LIST** — Done since
       2026-07-09, verified clean five consecutive nights. It is no longer re-checked nightly.
     * Gateway still dead, day 5 (log frozen 2026-08-22 09:18, both launchd jobs PID `-`, both
       cron jobs Next-run in the past). `t_0297cbbb` unchanged; compounds `t_91c02f58`.
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * Ledger now **+422 / -1** uncommitted (20 nightly blocks, 07-29 .. 08-27); last commit
       touching the file is `1e30d5b` from 2026-07-14. Tracked as `t_a8a3d3bb`. -->

<!-- Nightly reconcile 2026-08-26 (evidence-only; no swingtrader tracker mutated):
     * ZERO commits on any ref since the 08-25 scan. origin/main still `f4d8e9f` (2026-07-14).
       Last PR merge was #37 on 2026-07-15 — now FORTY-TWO DAYS. Zero open PRs. With top5's
       queue moving tonight, this is unambiguously the longest no-code-movement gap here.
     * Checkout is on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` remains the sharpest local risk, unchanged: orphaned worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb` @ `3eb1878`, a branch that IS an ancestor
       of origin/main (already merged), holding 10 modified files plus 2 stashes. None of it is on
       origin/main; a prune loses it. Needs Bryan: commit or discard.
     * Third worktree still parked outside the repo tree:
       `~/Documents/Codex/2026-07-27/realtime-voice-chat/swingtrader-strategy-lab-spec`
       on `codex/strategy-lab-spec` @ `f4d8e9f` (level with main, clean).
     * Linear: BRY-272/274/276/279/289 still Todo, untouched since 2026-07-08. BRY-91 / BRY-104 /
       BRY-21 re-verified still in the **Duplicate** state (last touched 06-30, 06-30, 05-01) —
       needs Bryan: close them or fold them into their survivors. BRY-306 (Canceled) vs BRY-98
       (Done) remains a deliberate de-dupe, not a defect.
     * BRY-237 Reddit-retirement watch item stays RESOLVED (Done 2026-07-09, re-verified tonight).
       This is the 4th consecutive night it has verified clean — it can be dropped from the
       nightly watch list.
     * NEW TONIGHT (cross-project, affects this repo's mirroring) — the Hermes gateway daemon has
       not run since **2026-08-22 09:18**: `launchctl list` shows `ai.hermes.gateway` loaded with
       PID `-`, no process in `ps`, gateway logs stop 08-22, and both `hermes cron` jobs have a
       Next run in the PAST. Filed as `t_0297cbbb` (triage,
       key `alignment:hermes:gateway-daemon-down`). Compounds `t_91c02f58`.
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * This ledger is now **+391** uncommitted (19 nightly blocks, 07-29 .. 08-26); last commit
       touching the file is `1e30d5b` from 2026-07-14. Tracked as `t_a8a3d3bb`. -->

<!-- Nightly reconcile 2026-08-25 (evidence-only; no tracker mutated):
     * ZERO commits on any ref since the 08-24 scan. origin/main still `f4d8e9f` (2026-07-14).
       Last PR merge was #37 on 2026-07-15 — now FORTY-ONE DAYS. Zero open PRs. Still the longest
       no-code-movement gap of any project scanned.
     * Checkout is on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md` (a stray top5 doc, unchanged).
     * `../swingtrader-hpa` remains the sharpest local risk, unchanged: orphaned worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb`, which IS an ancestor of origin/main
       (already merged), holding 10 modified files plus 2 stashes. None of it is on origin/main;
       a prune loses it. Needs Bryan: commit or discard.
     * Linear: BRY-272/274/276/279/289 still Todo, untouched since 2026-07-08. BRY-91 / BRY-104 /
       BRY-21 re-verified still in the **Duplicate** state (last touched 06-30, 06-30, 05-01) —
       needs Bryan: close them or fold them into their survivors. BRY-306 (Canceled) vs BRY-98
       (Done) remains a deliberate de-dupe, not a defect.
     * BRY-237 Reddit-retirement watch item stays RESOLVED (Done 2026-07-09, `465c835` re-verified
       contained in origin/main tonight).
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * This ledger is now +18 uncommitted nightly blocks (07-29 .. 08-25); last commit touching the
       file is `1e30d5b` from 2026-07-14. Tracked as `t_a8a3d3bb`. -->

<!-- Nightly reconcile 2026-08-24 (evidence-only; no tracker mutated):
     * ZERO commits on any ref in 48h. origin/main still `f4d8e9f` (2026-07-14). Last PR merge
       was #37 on 2026-07-15 — now FORTY DAYS. Zero open PRs. Still the longest no-code-movement
       gap of any project here, and now longer than statcard's.
     * Checkout is on `main`, level with origin, clean apart from this ledger and the untracked
       `docs/PRINTING_PRESS_INTEGRATION.md`.
     * `../swingtrader-hpa` remains the sharpest local risk: an orphaned worktree on
       `claude/historical-pattern-analysis-issue-h6k7kb`, which IS an ancestor of origin/main
       (already merged), holding 10 modified files (`utils/model_selector.py`,
       `utils/anthropic_client.py`, `utils/web_search_client.py`, `config/settings.py`,
       `bot/weekly_report.py`, `swing-trader-prd.md`, `.env.example`, 3 test files) plus 2
       stashes. None of it is on origin/main; a prune loses it. Needs Bryan: commit or discard.
     * Linear: BRY-272/274/276/279/289 still Todo and BRY-60/96/99/101/103/236/238 still Backlog,
       all untouched since 2026-07-08/09. BRY-91 / BRY-104 / BRY-21 have sat in the **Duplicate**
       state for months — needs Bryan: close them or fold them into their survivors. BRY-306
       (Canceled) vs PR #37's cited BRY-98 (Done) is a deliberate de-dupe, not a defect; the
       previous nights' "mark BRY-306 Done" suggestion is withdrawn as unnecessary.
     * BRY-237 Reddit-retirement watch item stays RESOLVED (Done 2026-07-09, `465c835` on main).
     * Hermes unchanged: blocked `t_04e108a3`, `t_1bdee59c`, `t_df750a07`, `t_cf765615`,
       `t_b2db97d7`, `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`; triage `t_5333196b`, `t_459e910e`,
       `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * This ledger is now +17 uncommitted nightly blocks (07-29 .. 08-24) with the last commit
       touching the file being `1e30d5b` from 2026-07-14. Tracked as `t_a8a3d3bb`. -->


<!-- Nightly reconcile 2026-08-23 (evidence-only; one stale bullet corrected, see below):
     * QUIET on main — origin/main still `f4d8e9f` (docs: SYSTEM_CAPABILITIES + PRD refresh), the
       same head as the 08-22 scan. Working tree is clean apart from this ledger. ZERO open PRs.
     * CORRECTED TONIGHT — the "P1 (review) — Spec J: event-replay backtester ... in review, two
       open PRs to compare" bullet was stale by ~5 weeks and is now marked shipped. Evidence:
       PR #37 MERGED (2026-07-14) and on origin/main as `1e30d5b`; the parallel PR #36 CLOSED
       unmerged; Linear `BRY-98` = **Done** (2026-07-15); `BRY-306` = **Canceled** (2026-07-15);
       `gh pr list --state open` returns 0. No tracker was mutated — Linear and Hermes already
       agreed; only this ledger disagreed.
     * BRY-237 WATCH-ITEM RESOLVED — the long-standing "Reddit retirement shipped via direct push
       (465c835), Linear may be stuck In Review" flag is closed out: `BRY-237` is **Done**
       (updated 2026-07-09). Hermes `t_ce9d1ea0` is `done`. Nothing to escalate; dropping it from
       the nightly watch list.
     * Open Hermes cards unchanged, all still blocked/triage: `t_04e108a3` (launch assets),
       `t_1bdee59c` (prod ops smoke), `t_df750a07` (PipelineRun cost attribution), `t_cf765615`
       (Alembic plan), `t_b2db97d7` (email backup), `t_806b113c`, `t_a6fac794`, `t_b08f0ee7`, plus
       triage `t_5333196b`, `t_459e910e`, `t_a5fb2edb`, `t_6413f3b7`, `t_a00544b1`, `t_c1256a60`.
     * Linear unchanged: 15 open swingtrader issues, none In Progress or In Review, none touched
       since 2026-07-09. BRY-91 / BRY-104 / BRY-21 still sit in the **Duplicate** state and have
       for months — needs Bryan: close them or fold them into their survivors.
     * ORPHANED WORKTREE, DIRTY — `~/AppsinTesting/swingtrader-hpa` is checked out on
       `claude/historical-pattern-analysis-issue-h6k7kb`, which IS an ancestor of origin/main
       (i.e. already merged), yet holds **10 modified files** (`utils/model_selector.py`,
       `utils/anthropic_client.py`, `utils/web_search_client.py`, `config/settings.py`,
       `bot/weekly_report.py`, `swing-trader-prd.md`, `.env.example`, 3 test files). Those changes
       are NOT on origin/main and would be lost on a worktree prune. Needs Bryan: review and
       commit or discard deliberately.
     * NEW TONIGHT — this ledger's reconcile blocks have never been committed: `git diff
       todoscratchpad.md` is +310 lines across 16 nightly blocks (07-29 .. 08-22), and the last
       commit touching this file is `1e30d5b` from 2026-07-14. Filed as Hermes `t_a8a3d3bb`
       (triage, key `alignment:multi:uncommitted-nightly-scratchpad-blocks`).
     * `docs/PRINTING_PRESS_INTEGRATION.md` still untracked (unchanged from 08-22). -->

<!-- Nightly reconcile 2026-08-22 (evidence-only):
     * QUIET (night 19) — no new commits, no new or changed PRs. origin/main still `f4d8e9f`; last
       PR merge was #37 on 2026-07-15, now THIRTY-EIGHT DAYS ago. Zero open PRs. Still the longest
       no-code-movement gap of any active project.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08. Nothing in team BRY has been updated since BRY-308 on
       2026-08-11 — now 11 days.
     * BRY-306 CONFLICT RE-VERIFIED AND UNCHANGED (night 17): Linear says **Canceled**, the code is
       **on main** (#37 merged 2026-07-15 as `1e30d5b`; #36 closed unmerged the same minute). Not
       auto-resolved — #37 was PR-shipped, so status is the GitHub integration's to own. Needs
       Bryan: mark BRY-306 Done, and note the ID mismatch (PR #37's title cites BRY-98, Done but
       archived 2026-07-08). The P1 (review) bullet below still says "two open PRs to compare" and
       is stale for the seventeenth consecutive night.
     * BRY-237 (legacy Reddit retirement, direct-push `465c835`) re-verified Done since 2026-07-09;
       `465c835` confirmed contained in origin/main. Watch-item stays RESOLVED and retired.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16) with 10 modified files, 2 stashes.
       All open swingtrader cards still carry correct workspace paths. -->

<!-- Nightly reconcile 2026-08-21 (evidence-only):
     * QUIET (night 18) — no new commits, no new or changed PRs. origin/main still `f4d8e9f`; last
       PR merge was #37 on 2026-07-15, now THIRTY-SEVEN DAYS ago. Zero open PRs. Still the longest
       no-code-movement gap of any active project.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08. BRY-60/96/99/101/103/236/238 still Backlog. Nothing in
       team BRY has been updated since 2026-08-11 (BRY-308) — now 10 days.
     * BRY-306 CONFLICT RE-VERIFIED AND UNCHANGED (night 16): Linear says **Canceled**, the code is
       **on main** (#37 merged 2026-07-15 as `1e30d5b`; #36 closed unmerged the same minute). Not
       auto-resolved — #37 was PR-shipped, so status is the GitHub integration's to own. Needs
       Bryan: mark BRY-306 Done, and note the ID mismatch (PR #37's title cites BRY-98, Done but
       archived 2026-07-08). The P1 (review) bullet below still says "two open PRs to compare" and
       is stale for the sixteenth consecutive night.
     * BRY-237 (legacy Reddit retirement, direct-push `465c835`) re-verified Done since 2026-07-09.
       The "stuck In Review via direct push" watch-item stays RESOLVED and is retired from the
       recurring watch list.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16) with 10 modified files, 2 stashes.
       All open swingtrader cards still carry correct workspace paths. -->

<!-- Nightly reconcile 2026-07-29 (evidence-only; bullets left for Bryan to rewrite):
     * Spec J is RESOLVED, not "in review". PR #37 (Opus, `claude/spec-j-backtester-opus`) MERGED
       2026-07-15 as `1e30d5b`; the competing Codex PR #36 was CLOSED unmerged the same minute.
       Linear: BRY-98 Done, BRY-306 (the #36 mirror) Canceled. Hermes `t_8308adac` not on the
       default board. The P1 (review) bullet below is stale.
     * Spec H (`BRY-290`) is MERGED, not pending-a-PR: `scripts/bulk_load_structured_events.py`
       landed on origin/main via PR #31 (`fba764c`); Linear BRY-290 is Done. The P1 (ops) bullet's
       precondition ("after PR for claude/audit-h-structured-backfill merges") is already met —
       what remains is purely the prod bulk-load + FMP-key live run.
     * Post-ledger merges not reflected below: #32 (BRY-300 remnants), #33 (model attestation +
       sonnet-5 tiers, BRY-264), #34 (Spec I flywheel), #35 (BRY-301 billing paging), #37 (Spec J),
       and `f4d8e9f` SYSTEM_CAPABILITIES/PRD refresh (direct push to main, no PR).
     * DUPLICATE LINEAR MIRRORS — the 2026-07-08 resync did NOT archive the old ones. Both are
       live in Todo: BRY-97/BRY-279 (email backup), BRY-106/BRY-274 (cost attribution),
       BRY-107/BRY-276 (Alembic), BRY-235/BRY-272 (launch assets), BRY-243/BRY-289 (parity eval).
       Needs Bryan's call on which side to archive — not auto-resolved.
     * Working tree clean on main; one untracked file `docs/PRINTING_PRESS_INTEGRATION.md`
       (a top5 doc — likely landed in the wrong repo).
     * Orphaned worktree: `../swingtrader-hpa` sits on `claude/historical-pattern-analysis-issue-h6k7kb`
       (PR #16 already MERGED), 35 behind main, with 9 modified files. -->

<!-- Nightly reconcile 2026-07-30 (evidence-only):
     * RETRACTION of the "DUPLICATE LINEAR MIRRORS" line above — it was a query artifact, not a real
       duplication. Linear's list_issues defaults to includeArchived=true; re-queried tonight with
       includeArchived=false, the only live swingtrader Todo issues are BRY-272/274/276/279/289.
       BRY-97/106/107/235/243 all carry archivedAt=2026-07-08T06:34Z. Nothing for Bryan to decide.
     * PR #20 ([codex] Reconcile swingtrader scratchpad) is MERGED (`bb53378`) — it is no longer one
       of the four docs-only PRs awaiting a decision.
     * BRY-237 (legacy Reddit retirement, direct-push `465c835`) was manually moved to Done on
       2026-07-09. The recurring "stuck In Review via direct push" watch-item is RESOLVED.
     * No new commits/PRs since the 2026-07-29 scan: origin/main still at `f4d8e9f`, no open PRs.
     * Unchanged from last night: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main;
       `../swingtrader-hpa` still orphaned on the merged `claude/historical-pattern-analysis-issue-h6k7kb`
       (now 35 ahead of its base with 9 modified files, PR #16 merged). -->

<!-- Nightly reconcile 2026-07-31 (evidence-only):
     * QUIET NIGHT — no new commits, no new/changed PRs since the 2026-07-30 scan. origin/main still
       at `f4d8e9f`; zero open PRs. Linear: zero swingtrader issues In Progress or In Review;
       BRY-272/274/276/279/289 still Todo, BRY-60/96/99/101/103/236/238 still Backlog.
     * BRY-237 re-verified tonight: Done since 2026-07-09T04:04Z (state history Todo → In Review →
       Done). The recurring "stuck In Review via direct push" watch-item stays RESOLVED.
     * The P1 (review) Spec J bullet below is STILL STALE — it says "two open PRs to compare"; #37
       merged 2026-07-15 (`1e30d5b`) and #36 was closed unmerged. Left for Bryan to rewrite.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (mtime 2026-07-12, a top5
       doc that landed in the wrong repo); `../swingtrader-hpa` still an orphaned worktree on the
       merged `claude/historical-pattern-analysis-issue-h6k7kb` with 10 modified files. -->

<!-- Nightly reconcile 2026-08-01 (evidence-only):
     * QUIET NIGHT (night 3) — no new commits, no new or changed PRs since the 2026-07-31 scan.
       origin/main still at `f4d8e9f`; zero open PRs; working tree on `main` and clean apart from
       this ledger and one untracked doc.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, none updated since 2026-07-08.
     * BRY-237 re-verified a third night: Done since 2026-07-09T04:04Z (state history
       Todo → In Review → Done, `completedAt` set). The "stuck In Review via direct push" recurring
       watch-item is RESOLVED and can be retired from the watch list.
     * The P1 (review) Spec J bullet below is STILL STALE (night 3) — it says "two open PRs to
       compare"; #37 merged 2026-07-15 (`1e30d5b`) and #36 was closed unmerged. Left for Bryan.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a top5 doc that landed in
       the wrong repo — top5 already carries its own copy at `docs/PRINTING_PRESS_INTEGRATION.md`
       per its CLAUDE.md, so this one is a stray); `../swingtrader-hpa` still an orphaned worktree
       on the merged `claude/historical-pattern-analysis-issue-h6k7kb` with 10 modified files. -->

<!-- Nightly reconcile 2026-08-04 (evidence-only; covers 08-02→08-04, no reconcile ran those nights):
     * QUIET (night 6) — no new commits, no new or changed PRs. origin/main still at `f4d8e9f`
       (2026-07-15 was the last PR merge, #37); zero open PRs; working tree on clean `main` apart
       from this ledger and one untracked doc. Twenty days with no code movement.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, still untouched since 2026-07-08; BRY-60/96/99/101/103/236/238 still Backlog.
     * BRY-237 re-verified a fourth night: Done since 2026-07-09T04:04Z. RETIRING this from the
       recurring watch list — four consecutive confirmations is enough.
     * The P1 (review) Spec J bullet below is STILL STALE (night 4) — it says "two open PRs to
       compare"; #37 merged 2026-07-15 (`1e30d5b`) and #36 was closed unmerged. Left for Bryan.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb`.
     * This ledger now carries 64 lines of uncommitted reconcile notes (four nights) that have never
       been committed. Same is true in sentinel (86) and watchthis (87). Not committed here — the
       reconciler does not commit, and these are evidence blocks Bryan is meant to fold into the
       bullets below.
     * Hermes workspace paths verified from `hermes kanban list --json`: all ten open swingtrader
       cards correctly point at `/Users/bryanniyonzima/AppsinTesting/swingtrader`. This is the only
       project with no stale-workspace problem. -->

<!-- Nightly reconcile 2026-08-06 (evidence-only):
     * QUIET (night 7) — no new commits, no new or changed PRs since the 2026-08-04 scan.
       origin/main still at `f4d8e9f`; last PR merge was #37 on 2026-07-15. Twenty-two days with no
       code movement. Working tree on clean `main` apart from this ledger and one untracked doc.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08.
     * ROOT CAUSE FOUND for that staleness (applies to every project, logged in full in the top5
       ledger): the Hermes->Linear sync has NO SCHEDULER — no crontab, no LaunchAgent, not one of
       the five Claude scheduled tasks. Linear only moves when an agent runs
       `sync_kanban_to_linear.py` by hand; newest issue team-wide is BRY-306 (2026-07-15).
       Tracking card `t_91c02f58` (triage, `alignment:infra:kanban-linear-sync-not-scheduled`).
     * BRY-237 stays retired from the watch list (Done since 2026-07-09; four prior confirmations).
     * The P1 (review) Spec J bullet below is STILL STALE (night 5) — #37 merged 2026-07-15
       (`1e30d5b`), #36 closed unmerged. Left for Bryan to rewrite.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb`. All ten open swingtrader cards still carry
       correct workspace paths — still the only project with no stale-workspace problem. -->

<!-- Nightly reconcile 2026-08-08 (evidence-only; covers 08-06→08-08, no reconcile ran 08-07):
     * QUIET (night 8) — no new commits, no new or changed PRs. origin/main still at `f4d8e9f`;
       the last PR merge was #37 on 2026-07-15. TWENTY-FOUR DAYS with no code movement, the
       longest gap of any active project. Zero open PRs; working tree on clean `main` apart from
       this ledger and one untracked doc.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all
       still Todo, untouched since 2026-07-08; BRY-60/96/99/101/103/236/238 still Backlog.
     * The kanban->Linear sync is still unscheduled (`t_91c02f58`), re-verified tonight: no
       crontab, no LaunchAgent invoking it. One correction worth carrying: Linear is NOT hard
       frozen — `BRY-307` was hand-created in top5 on 2026-08-07, so issues do appear when a
       session mints them. Full detail in the top5 ledger.
     * The P1 (review) Spec J bullet below is STILL STALE (night 6) — it says "two open PRs to
       compare"; #37 merged 2026-07-15 (`1e30d5b`) and #36 was closed unmerged. Left for Bryan.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16), now carrying 9 modified files.
       All ten open swingtrader cards still carry correct workspace paths — still the only
       project with no stale-workspace problem.
     * This ledger now carries five nights of uncommitted reconcile notes. top5 and sentinel got
       theirs committed by working sessions; nothing has committed here since 2026-07-15. -->

<!-- Nightly reconcile 2026-08-11 (evidence-only; covers 08-08→08-11, no reconcile ran 08-09/08-10):
     * QUIET (night 9) — no new commits, no new or changed PRs. origin/main still at `f4d8e9f`;
       the last PR merge was #37 on 2026-07-15. TWENTY-SEVEN DAYS with no code movement, still the
       longest gap of any active project. Zero open PRs; working tree on clean `main` apart from
       this ledger and one untracked doc.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08; BRY-60/96/99/101/103/236/238 still Backlog.
     * BRY-237 stays retired from the watch list — re-verified Done (2026-07-09). Fifth
       consecutive confirmation; the "stuck In Review via direct push" concern is closed.
     * DUPLICATE-MIRROR QUESTION IS CLOSED, not open. The 2026-07-29 note asked Bryan to choose
       between BRY-97/279, BRY-106/274, BRY-107/276, BRY-235/272, BRY-243/289. Re-queried tonight
       with includeArchived: every old-side issue (BRY-97, 106, 107, 235, 243, plus BRY-98/102)
       carries archivedAt=2026-07-08. Only the 27x side is live. Same query artifact sentinel
       retracted on 07-30. Nothing for Bryan to decide.
     * The kanban->Linear sync is still unscheduled (`t_91c02f58`), re-verified tonight: no
       crontab, no LaunchAgent (only ai.hermes.gateway + gateway-steward), not one of the Claude
       scheduled tasks. It is NOT hard frozen — `BRY-308` was minted 2026-08-11 00:02 by a top5
       session — but it only moves when an agent runs the script by hand.
     * The P1 (review) Spec J bullet below is STILL STALE (night 7) — #37 merged 2026-07-15
       (`1e30d5b`), #36 closed unmerged. Left for Bryan.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16), 9 modified files. All ten open
       swingtrader cards still carry correct workspace paths — still the only project with no
       stale-workspace problem. Six swingtrader cards remain triage with no Linear mirror
       (t_5333196b, t_459e910e, t_a5fb2edb, t_6413f3b7, t_a00544b1, t_c1256a60). -->

<!-- Nightly reconcile 2026-08-12 (evidence-only):
     * QUIET (night 10) — no new commits, no new or changed PRs since the 2026-08-11 scan.
       origin/main still at `f4d8e9f`; last PR merge was #37 on 2026-07-15. TWENTY-EIGHT DAYS
       with no code movement — still the longest gap of any active project. Zero open PRs.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all
       still Todo, untouched since 2026-07-08.
     * The P1 (review) Spec J bullet below is STILL STALE (night 8) — #37 merged 2026-07-15
       (`1e30d5b`), #36 closed unmerged, Linear BRY-306 is Canceled while the code is on main.
       Note the ID mismatch too: PR #37's title says BRY-98, but BRY-98 was archived 2026-07-08;
       the live mirror is BRY-306. Left for Bryan to rewrite.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16), 10 modified files. All open
       swingtrader cards still carry correct workspace paths — still the only project with no
       stale-workspace problem. -->

<!-- Nightly reconcile 2026-08-13 (evidence-only):
     * QUIET (night 11) — no new commits, no new or changed PRs since the 2026-08-12 scan.
       origin/main still at `f4d8e9f`; last merge was #37 on 2026-07-15, now TWENTY-NINE DAYS ago.
       Zero open PRs. Still the longest no-code-movement gap of any active project (sentinel merged
       eight PRs in the same window).
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08. BRY-60/96/99/101/103/236/238 still Backlog.
     * BRY-306 conflict re-verified and UNCHANGED (night 9): Linear says **Canceled**, the code is
       **on main** (#37 merged 2026-07-15 as `1e30d5b`, #36 closed unmerged). This is the one
       swingtrader item where a tracker actively contradicts shipped reality. Not auto-resolved —
       #37 was PR-shipped, so the status is the GitHub integration's to own. Needs Bryan: mark
       BRY-306 Done, and note the ID mismatch (PR #37's title says BRY-98, archived 2026-07-08).
       The P1 (review) bullet below still says "two open PRs to compare" and is stale.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16) with 10 modified files; 2 stashes.
       Working tree otherwise clean on `main`, level with origin. -->

<!-- Nightly reconcile 2026-08-15 (evidence-only; covers 08-13→08-15, no reconcile ran 08-14):
     * QUIET (night 12) — no new commits, no new or changed PRs since the 08-13 scan. origin/main
       still at `f4d8e9f`; the last PR merge was #37 on 2026-07-15, now THIRTY-ONE DAYS ago. Zero
       open PRs. Still the longest no-code-movement gap of any active project (sentinel merged
       eight PRs inside that window and top5 merged ten).
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08; BRY-60/96/99/101/103/236/238 still Backlog.
     * BRY-306 CONFLICT RE-VERIFIED AND UNCHANGED (night 10): Linear says **Canceled**, the code is
       **on main** (#37 merged 2026-07-15 as `1e30d5b`, #36 closed unmerged the same minute). Still
       the one swingtrader item where a tracker actively contradicts shipped reality. Not
       auto-resolved — #37 was PR-shipped, so status is the GitHub integration's to own. Needs
       Bryan: mark BRY-306 Done, and note the ID mismatch (PR #37's title says BRY-98, which was
       archived 2026-07-08). The P1 (review) bullet below still says "two open PRs to compare" and
       is stale for the tenth consecutive night.
     * BRY-237 stays retired from the watch list — spot-checked again tonight, still Done
       (2026-07-09). Sixth confirmation; not re-verified going forward.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16) with 10 modified files. All open
       swingtrader cards still carry correct workspace paths — still the only project with no
       stale-workspace problem (the 15 dead `~/Downloads/AppsinTesting` cards are all top5,
       watchthis and sentinel). -->

<!-- Nightly reconcile 2026-08-16 (evidence-only):
     * QUIET (night 13) — no new commits, no new or changed PRs since the 08-15 scan. origin/main
       still at `f4d8e9f`; the last PR merge was #37 on 2026-07-15, now THIRTY-TWO DAYS ago. Zero
       open PRs. Still the longest no-code-movement gap of any active project.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08; BRY-60/96/99/101/103/236/238 still Backlog.
     * BRY-306 CONFLICT RE-VERIFIED AND UNCHANGED (night 11): Linear says **Canceled**, the code is
       **on main** (#37 merged 2026-07-15 as `1e30d5b`, #36 closed unmerged the same minute). Still
       the one swingtrader item where a tracker actively contradicts shipped reality. Not
       auto-resolved — #37 was PR-shipped, so status is the GitHub integration's to own. Needs
       Bryan: mark BRY-306 Done, and note the ID mismatch (PR #37's title says BRY-98, which was
       archived 2026-07-08 and no longer resolves in team BRY). The P1 (review) bullet below still
       says "two open PRs to compare" and is stale for the eleventh consecutive night.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16) with 10 modified files, 2 stashes.
       All open swingtrader cards still carry correct workspace paths — still the only project with
       no stale-workspace problem. -->

<!-- Nightly reconcile 2026-08-19 (evidence-only; no run on 08-17 or 08-18, so this covers 3 nights):
     * QUIET (night 16) — no new commits, no new or changed PRs. origin/main still `f4d8e9f`; the
       last PR merge was #37 on 2026-07-15, now THIRTY-FIVE DAYS ago. Zero open PRs. Still the
       longest no-code-movement gap of any active project.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08. Nothing in team BRY has been updated since 2026-08-11.
     * BRY-306 CONFLICT RE-VERIFIED AND UNCHANGED (night 14): Linear says **Canceled**, the code is
       **on main** (#37 merged 2026-07-15 as `1e30d5b`; #36 closed unmerged the same minute). Still
       the one swingtrader item where a tracker actively contradicts shipped reality. Not
       auto-resolved — #37 was PR-shipped, so status is the GitHub integration's to own. Needs
       Bryan: mark BRY-306 Done, and note the ID mismatch (PR #37's title cites BRY-98, which is
       Done but was archived 2026-07-08). The P1 (review) bullet below still says "two open PRs to
       compare" and is stale for the fourteenth consecutive night.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16) with 10 modified files, 2 stashes.
       All open swingtrader cards still carry correct workspace paths — still the only project with
       no stale-workspace problem. -->

<!-- Nightly reconcile 2026-08-20 (evidence-only):
     * QUIET (night 17) — no new commits, no new or changed PRs. origin/main still `f4d8e9f`; last
       PR merge was #37 on 2026-07-15, now THIRTY-SIX DAYS ago. Zero open PRs. Still the longest
       no-code-movement gap of any active project.
     * Linear: zero swingtrader issues In Progress or In Review. BRY-272/274/276/279/289 all still
       Todo, untouched since 2026-07-08. BRY-60/96/99/101/103/236/238 still Backlog. Nothing in
       team BRY has been updated since 2026-08-11.
     * BRY-306 CONFLICT RE-VERIFIED AND UNCHANGED (night 15): Linear says **Canceled**, the code is
       **on main** (#37 merged 2026-07-15 as `1e30d5b`; #36 closed unmerged the same minute). Not
       auto-resolved — #37 was PR-shipped, so status is the GitHub integration's to own. Needs
       Bryan: mark BRY-306 Done, and note the ID mismatch (PR #37's title cites BRY-98, Done but
       archived 2026-07-08). The P1 (review) bullet below still says "two open PRs to compare" and
       is stale for the fifteenth consecutive night.
     * BRY-237 (legacy Reddit retirement, direct-push `465c835`) re-verified Done since 2026-07-09.
       The "stuck In Review via direct push" watch-item stays RESOLVED and can be retired.
     * Unchanged: untracked `docs/PRINTING_PRESS_INTEGRATION.md` on main (a stray top5 doc);
       `../swingtrader-hpa` still an orphaned worktree on the merged
       `claude/historical-pattern-analysis-issue-h6k7kb` (PR #16) with 10 modified files, 2 stashes.
       All open swingtrader cards still carry correct workspace paths. -->




### Investment Workspace spec series K–Q (`BRY-310`, new, 2026-09-05)

Specs written in `specs/investment-workspace/` — turns SwingTrader from a scan-and-score
bot into a cloud-hosted investment workspace any agent client (Claude Code local/cloud,
Codex, phone) can attach to. Umbrella + owner decisions + delivery order in
`specs/investment-workspace/README.md`; per-phase Codex `/goal` contracts in
`goal-prompts.md`. Nothing is implemented — these are specs only.

- [x] **Broker decision — confirmed 2026-09-05**: Robinhood now, Schwab deferred ("RH is
      fine, might do Schwab later"). Schwab stub dropped from Phase 1; capability
      contract + fake broker ship instead (`L` §5.2).
- [x] **Robinhood execution scope — decided 2026-09-05: use the Agentic account.** Risk
      caps span the combined book (`L` §5.1, §6). Verified 2026-09-06 from Robinhood's
      own pages; the repo already uses the official server (the "unofficial server"
      worry was a drafting error). Beta, no developer docs, T+1 on cash accounts, no
      dividend tools.
- [x] **Second research pass — verification, folded in as v0.3 (2026-09-06)** — report
      at `docs/research/2026-09-research-verification.md` (merged from
      `claude/research-workspace-verify-hbuexz`); README §10 is the v0.2→v0.3 changelog.
- [ ] **P0 (ops) — cherry-pick the `mcp<2` pin to `main` before the next Railway
      deploy** (commit `18b47a2` on this branch). Verified in a clean venv: `mcp` 2.1.1
      drops `streamablehttp_client`, so a fresh install breaks every Robinhood call and
      the broker reports "install the SDK" while the SDK is installed.
- [x] **Robinhood `tools/list` schema dumped 2026-09-08** — 73 tools,
      `docs/robinhood/tool_schemas.json`. No bracket/attached stop; `stop_market` +
      `gtc` + whole shares + regular hours; `ref_id` idempotency. Findings in `L` §5.1.
      Idle token from June did not survive (desktop re-auth needed) — 30-day unattended
      refresh test stays in Phase 1.
- [ ] **Owner action — Sharadar Prices ($9 "from") at checkout** (README §9.2): what the
      base tier gates, bulk download, terms. Pricing verified by screenshot.
- [ ] **Owner action — twenty-delisting audit** (README §9.3, Spec N §4.2) against
      Sharadar before paying.
- [x] **Data stack decided 2026-09-08** — free sources + Sharadar Prices $9; Agentic
      account is cash/no-margin; sizing advisory with a discretionary budget (README §3).
- [ ] **Builds in flight (spawned 2026-09-08):** Phase 0a `claude/phase-0a-schema-discipline`,
      Phase 3b-core `claude/phase-3b-comparables-core`. Next after 0a merges: 3a, 1, 2.
- [x] **Best-in-class research folded in — v0.2, 2026-09-05** — full report at
      `specs/investment-workspace/research/2026-09-05-best-in-class-research.md`;
      README §5 slot table now carries decisions; README §8 is the v0.1→v0.2 changelog.
      Caveat: the research session's proxy blocked most vendor domains, so prices and
      several vendor capabilities are search-extract tier — verify before spending.
- [x] **Phase 0a — schema discipline** — Alembic baseline + engine-neutral models + CI
      matrix on SQLite and Postgres (`K` §3.1–3.2). Merged as PR #41.
      **This closes `BRY-107` below**, which has been "decision pending" since July;
      the decision is made in Spec K.
      Blocks everything; N and Q need only this, not the cutover.
- [ ] **Phase 0b — Postgres cutover** — code landed on
      `claude/phase-0b-cutover-and-workspace-skeleton`: `scripts/migrate_sqlite_to_postgres.py`
      (dependency-ordered, read-only source, per-table content hashes, idempotent,
      report to `docs/audits/`), a `pg_advisory_xact_lock` around `ensure_schema`,
      revision-signature adoption so the prod file still classifies `legacy` now that
      `0002` adds a table, the `workspace/` FastAPI + MCP service (`/health`, `/v1`,
      `/mcp`, one `whoami` tool, `WORKSPACE_API_ENABLED=false`), owner tokens with
      scopes and per-token rate limits, and the `data_dir()` / `evals/` SQLite fixes.
      **Needs Bryan (owner actions, not done):** provision Postgres, set
      `DATABASE_URL` + `DATA_DIR`, run the migration, create the second Railway
      service. Runbook: `docs/POSTGRES_CUTOVER_RUNBOOK.md`. Blocks Phases 1, 2, 4.
- [ ] **Phase 1 — portfolio ledger + read-only tool surface** (`L`, `K` §4)
- [ ] **Phase 2 — research workspace: dossiers, theses, invalidators, git mirror** (`M`)
- [x] **Independent plan review folded in — v0.4 (2026-09-06)** — 11 should-fixes, 2 cuts
      taken; README §11 changelog. Sizing default for uncited proposals (half cap,
      labelled `unevidenced`) awaits owner confirmation (README §3).
- [ ] **Phase 3a — minimum SEC ingestion contract** (`N` §4.0, `O` §2/§3.4) — XBRL
      companyfacts + submissions acceptanceDateTime + share counts + 8-K 2.02 index.
      Free, no credentials. Needs only 0a. Blocks 3b.
- [ ] **Phase 3b — comparable-setups engine** (`N`) — the centerpiece: "how have setups
      genuinely like this performed", with PIT integrity, benchmark subtraction,
      overlap-aware uncertainty, regime splits, null tests, and a real `insufficient`
      answer. Parallelizable with Phase 5. Fixture first, `depth="quick"` first,
      price-only setup before earnings. Deps: `arch` + `statsmodels`; never `mlfinlab`.
- [ ] **Phase 3p — price plane** (`N` §4.2/§4.3/§4.5) — in review on
      `claude/phase-3p-price-plane`. Five tables (`securities`, `price_bars`,
      `corporate_actions`, `universe_membership`, `price_snapshots`) on migration
      `0002_price_plane`, branched from `0001_baseline` and joined to the 0b/3a
      head by `0004_merge_price_plane`; `PricePlane` interface with a
      fixture-backed and a Sharadar implementation; `sp500_wikipedia_v1` (MIT
      `fja05680/sp500`, committed) and `liquid_us_equity_v1` (computed from `price_bars`
      alone); the twenty-delisting audit recorded on the snapshot. Behind
      `PRICE_PLANE_ENABLED`, default off. Docs: `docs/PRICE_PLANE.md`,
      `docs/DATA_LICENSES.md`. Open items carried out of the build:
      - [ ] **Verify the twenty delisting facts against their Form 25 filings.** The
            build session's egress proxy blocks `www.sec.gov`, so each row cites an
            EDGAR *lookup* rather than an accession number and dates are
            month-reliable / session-approximate. The audit blob says so
            (`sources_verified_against_primary_filing: false`).
      - [ ] **Confirm Sharadar's column names and redistribution terms at checkout.**
            `data.nasdaq.com`, `sharadar.com` and `quantrocket.com` are all blocked;
            the adapter's column list is verified from search extracts only and is
            treated as a hypothesis it checks at runtime. Verification Claim 7 is still
            `UNVERIFIED` across the board.
      - [ ] **Run the delisting audit against Sharadar before paying** (`N` §4.2), and
            cross-check our derived total-return series against Sharadar's `closeadj`.
      - [ ] **Market-cap ranking for `liquid_us_equity_v1` waits for Phase 3a's share
            counts.** v1 is liquidity-only, versioned so the cap leg is a new slug.
- [ ] **Phase 4 — evidence planes: filings (13F/13D/G/Form 4), vintage-correct macro,
      timestamped news** (`O`)
- [ ] **Phase 5 — Strategy Lab** (`Q`) — ships on its own six-PR plan and prompts.
- [ ] **Phase 6 — order proposal→approval→execution** — gated on Phases 0–3 **and** on
      documenting whether Robinhood can place a protective exit that survives our
      process. Human-in-the-loop, not an autonomous goal run.

**Rescued from a worktree that was one `rm` from gone:** the 2026-08-21 Strategy Lab
architecture + agent prompts were staged-but-uncommitted (`AM`) on branch
`codex/strategy-lab-spec` in `~/Documents/Codex/2026-07-27/realtime-voice-chat/
swingtrader-strategy-lab-spec`, on no remote. Copied into
`specs/investment-workspace/strategy-lab/`. Same risk class as the `../swingtrader-hpa`
worktree still flagged in the nightly blocks above — that one is **still unresolved**.

- [ ] **P0 (ops) — Re-enable `SCHEDULER_ENABLED=true` on Railway** (audit §P0-3) — scans are off in prod, so no memos and the scoring corpus can't grow. Safe now that A (pattern engine) + B (scan lock + hang recovery) shipped. Record the flip as a release step (who/when/pre-flip evidence).
- [ ] **P0 (ops) — Run the one-time production backfill** (audit §P0-2, spec A) — `railway run python -m scripts.backfill_historical_events` then `backfill_event_outcomes.py` / `backfill_event_contexts.py`, then re-run `scripts/evaluate_pattern_analog_engine.py` as the bakeoff acceptance gate.
- [ ] **P1 (ops) — Run the structured bulk load in prod** (spec H, `BRY-290`) — after PR for `claude/audit-h-structured-backfill` merges: `railway ssh python -m scripts.bulk_load_structured_events --universe --years 3` (add `--fmp-daily-cap N` matching the FMP plan; ~2 FMP calls/ticker + outcome/context fetches). One-time warm-up of the earnings/upgrade pattern library from FMP structured data — no LLM spend. Also run the acceptance live-run (§H accept #2, 10 tickers `--years 2`) once an FMP key is available locally/CI (the worktree `.env` `FMP_API_KEY` is empty; full key lives on Railway).
- [x] **P1 — Spec J: event-replay backtester — SHIPPED 2026-07-14** (`BRY-98` Done, `BRY-306` Canceled, `t_8308adac`) — [PR #37](https://github.com/niyonzimabryan/swingtrader/pull/37) (Opus impl, `claude/spec-j-backtester-opus`) merged as `1e30d5b` on `origin/main`; the parallel [PR #36](https://github.com/niyonzimabryan/swingtrader/pull/36) (Codex) was closed unmerged. _(Reconciled 2026-08-23 — this line still read "in review, two open PRs to compare"; the repo has had zero open PRs since 07-14.)_ Original scope: J1 exit simulator + J2 event-anchored replay + J3 param sweep + J4 shadow-ledger adapter (`specs/audit-2026-07-04/J-event-replay-backtester.md`). Suite green (pytest 317 / unittest 311 / compileall). LLM-stage replay permanently out of scope by design. Prod-container run is an ops step (see PR runbook).
- [ ] **P1 — Finish public launch assets, README hero, and social preview cleanup** (`BRY-235`, `t_04e108a3`, blocked) — launch assets still local/untracked; README/social-preview cleanup not shipped. Hermes card is infra-blocked (worker crash, `pid not alive` ×2), not substance-blocked.
- [ ] **P1 — Run production ops smoke with private keys and Robinhood tiny caps** (`BRY-236`, `t_1bdee59c`, blocked) — `scripts.doctor --skip-live`, Telegram broker/account/mode checks, Robinhood review-only/tiny-cap validation; blocked on private credentials + Hermes worker crash.
- [ ] **P2 — Populate PipelineRun token and cost attribution** (`BRY-106`, `t_df750a07`, blocked) — cost/token attribution still unshipped; Hermes infra-blocked. Relates to G-observability (Gemini call ledger).
- [ ] **P2 — Add Alembic/Postgres migration plan before more schema churn** (`BRY-107`, `t_cf765615`, blocked) — decision still pending. Spec E added indices + the `reddit_sentiment` drop via the existing inline `create_all()` migration mechanism; the pattern engine added 6 tables the same way, so the Alembic trigger is closer.
- [ ] **P2 — Email backup channel** (`BRY-97`, `t_b2db97d7`, blocked) — reconciled 2026-07-03/07-08: no open PR, no merged implementation, `origin/main` has no `EmailBackupChannel`/`email_backup`/SMTP/Resend code beyond PRD mentions. A full implementation reportedly exists as a parked patch outside the repo that failed review (4 blockers) — fix-and-merge vs. drop is Bryan's call.
- [ ] **P3 — Add own trade history to pattern evidence after 30+ closed trades** (`BRY-60`, `t_806b113c`, blocked) — implementation exists only as a local patch handoff (`.hermes/patches/t_806b113c.patch`, branch `hermes/806b113c`); private evidence must stay aggregate-only.
- [ ] **P3 — Tune scoring weights from real closed-trade data after 50+ trades** (`BRY-96`, `t_a6fac794`, blocked) — wait for 50+ closed trades, then rebalance only if attribution evidence supports it.
- [ ] **P4 — Run scoring parity eval once trace corpus clears N_min=150** (`BRY-243`, `t_b08f0ee7`, blocked) — SCORING tier only (Opus→Sonnet-5). Corpus is 22 tagged / 29 actual scoring calls as of 2026-07-04 (7 ad-hoc calls untagged — fix specced in `specs/audit-2026-07-04/G-observability.md`; ad-hoc scoring counts toward the corpus). Growth is gated on `SCHEDULER_ENABLED=true`, so the card stays blocked until scans resume. Analyst/discovery-tier `sonnet-4-6`→`sonnet-5` is a separate approved upgrade, NOT gated on this.

## Completed

- [x] **Audit A — Pattern engine rescue** (`PR #23`, audit §P0-2/P1-1/P1-2/P1-7) — LEFT-JOIN outcomes, backfill queue→volume + consumer, grounding/PIT guards, FMP `company-screener` endpoint, typed discovery/pattern statuses.
- [x] **Audit B — Production reliability** (`PR #22`, audit §P0-1/P1-3/P1-6) — monitor broker-call timeouts, watchdog self-restart, market-holiday calendar, scan mutual-exclusion lock, scan-failure → Telegram, LLM retries, daily pre-market self-restart.
- [x] **Audit C — Tier-2 Gemini screener repair** (`PR #24`, audit §P0-4) — token-budget fix for mid-JSON truncation + robust extraction (structured output incompatible with Search grounding), parse-rate metric.
- [x] **Audit D — Broker & DB integrity** (`PR #21`, audit §P1-4/P1-5) — bracket-order qty conflict handling, position-not-found reconcile, SQLite WAL + busy_timeout.
- [x] **Audit H — API-first structured event backfill** (branch `claude/audit-h-structured-backfill`, spec H, `BRY-290`) — offline `scripts/bulk_load_structured_events.py` warms the pattern library from FMP `/earnings` surprises + `/grades` upgrade/downgrade clusters (zero LLM calls); H2 taxonomy adds guidance-agnostic `earnings_(beat|miss)_structured` types matched by the ranker as a discounted fallback tier for guidance-specific requests; structured types kept out of the live classifier menus. Suite green (pytest 254 / unittest 248 / compileall); offline bakeoff shows earnings/upgrade/miss cases `active` with structured analogs. Prod bulk-load + FMP-key live run remain ops steps (see open P1).
- [x] **Audit E — Hygiene sweep** (`PR #25`, in review, audit §P2-1/2/3) — `datetime.utcnow()` → naive-UTC helper, `Query.get()`→`Session.get()`, event-loop test helper, DB indices, dropped orphaned `reddit_sentiment` table, `.env.example` completeness + env-split docs, Gemini model startup log. Warnings 342→1.
- [x] **Pattern-library API warm-up** (`spec H`) — FMP earnings/upgrades bulk load to seed the analog store API-first.
- [x] **Model-eval adapter (Problem B)** — `evals/` adapter shipped via PR #19 (merged 2026-07-01). Pulls the scoring corpus live from Langfuse traces; scoring/filter TaskSpecs + P&L rollback monitor. Report-only; not imported by the bot runtime.
- [x] **Legacy Reddit surface retired** (`BRY-237`, `t_ce9d1ea0`) — shipped on `origin/main` at `465c835`: deleted runtime Reddit agent/data modules, removed Reddit env/settings/onboarding surface, added `tests/test_legacy_reddit_retired.py`. The orphaned `reddit_sentiment` table was fully dropped in Audit E (PR #25).
- [x] **Open-source baseline docs** — MIT license, financial disclaimer, contributing guide, README setup path, CI, secret-scan workflow.
- [x] **Paper-first broker safety** — Alpaca paper is the default broker; live trading gated behind explicit config.
- [x] **Robinhood broker option** — Robinhood MCP broker, Telegram broker/mode controls, micro-trading caps, review-first flow, audit events.
- [x] **Robinhood OAuth store** — encrypted MCP SDK token storage plus bootstrap/status commands.
- [x] **Public repo hygiene** — removed private handoff docs from tracked files; local archived copies under ignored `.claude/private_docs/`.

## Future Improvements

- [ ] **Interactive Brokers support** — evaluate after the Robinhood path is stable and documented.
- [ ] **Backtest framework** (`t_5333196b`, no Linear mirror, triage) — replay historical candidates through the pipeline to calibrate scoring before wider live use.
- [ ] **Batch approval UX** (`t_459e910e`, no Linear mirror, triage) — decide whether scheduled scan memos should queue for a morning review workflow.

## Tech Debt & Bugs

- [ ] **Run logging and cost tracking** — persist per-scan token/cost/duration metrics beyond provider dashboards (see G-observability Gemini call ledger + `BRY-106`).
- [ ] **Ad-hoc scoring stage tags** — 7 ad-hoc scoring calls are untagged and miss the BRY-243 corpus; fix specced in `specs/audit-2026-07-04/G-observability.md`.

## Process notes

**Release steps — feature-flag flips are releases, not config tweaks.** Any
Railway feature-flag flip (e.g. `PATTERN_ANALOG_ENGINE_ENABLED`,
`SCHEDULER_ENABLED`) is a release step: record who flipped it, when, and the
pre-flip evidence (backfill/bakeoff run, smoke result) in the tracker before and
after the flip. The pattern-engine went live when its flag was enabled without
the spec-mandated backfill + bakeoff gate, which is how a green test suite
coexisted with a structurally broken production feature.

*Last updated: 2026-07-08*
