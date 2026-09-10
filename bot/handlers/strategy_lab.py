"""Owner-only Strategy Lab commands (Spec Q §13, §15 PR 4 and PR 6).

``/experiments``       running experiments, their arms and the tier distribution
``/strategies``        the roster: champion, challengers, versions, statuses
``/strategy <slug>``   one strategy's decisions, executions and recent activity
``/pause_experiment``  and ``/resume_experiment`` — Spec Q §13's pause controls
                       (by name or row id; the configured experiment by default)
``/promote_arm``       and ``/demote_arm`` — a tier change, rendered then confirmed
``/promotions``        the append-only audit trail of every tier change

PR 4 shipped the first five and deliberately not the tier controls, because "an
owner-only button that calls a promotion path which does not exist yet would be
worse than no button". PR 6 added the path — ``strategy_lab/promotion.py`` for the
bindings, ``orchestrator/strategy_lab_promotion.py`` for the deployment gates —
so the buttons arrive with it. ``/live_kill`` is still Phase 6's switch
(``bot/handlers/proposals.py``) and is untouched: one kill switch, one row, one
command.

**Nothing here produces a number.** Counts are ``len()`` over stored rows.
Performance figures come from ``scripts/strategy_lab_scoreboard.py`` through
``orchestrator.strategy_lab_shadow.scoreboard``, which computes them in
``strategy_lab/metrics.py`` and ``comparables/inference.py``; this module reads
values out of that payload and formats them. AGENTS.md §1.2: no model output is
a statistic, and a Telegram message is not an exception to that.

Authorisation is the ``@authorized`` decorator, which is the chat-id allowlist
the whole bot uses; an unauthorised chat is silently ignored, and
``tests/test_strategy_lab_bot.py`` asserts a non-owner reaches none of these
handlers.
"""

from __future__ import annotations

from telegram import Update
from telegram.ext import ContextTypes

from bot.auth import authorized
from bot.formatters import escape_md
from bot.handlers._blocking_utils import BlockingCallTimeout, run_blocking
from utils.logger import get_logger

log = get_logger("bot_strategy_lab")

#: These handlers open a database session and, for the scorecard, run a
#: bootstrap. They go through `run_blocking` like `/performance` does, so a slow
#: read cannot block the bot's event loop.
LAB_TIMEOUT_S = 120

DISABLED_TEXT = (
    "Strategy Lab is disabled. Set STRATEGY_LAB_ENABLED=true (and "
    "STRATEGY_LAB_SHADOW_ENABLED=true for the post-scan shadow pass) to turn "
    "it on. See docs/ENV_SETUP.md section 10."
)


def _lab():
    from orchestrator import strategy_lab_shadow

    return strategy_lab_shadow


def _settings(context):
    pipeline = context.bot_data.get("pipeline")
    return getattr(pipeline, "settings", None)


async def _reply(update: Update, text: str) -> None:
    """Send a MarkdownV2 card, split at Telegram's 4096-character limit.

    `/strategies` over a longer roster, or `/experiments` once several
    experiments have run, will exceed it. `split_message` is the same splitter
    the message queue uses, so a long card degrades into two messages rather
    than into a `BadRequest` the caller never sees.
    """
    from bot.formatters import split_message

    for chunk in split_message(text):
        await update.message.reply_text(chunk, parse_mode="MarkdownV2")


async def _guarded(update: Update, context: ContextTypes.DEFAULT_TYPE, name: str, build):
    """Run a blocking read off the loop and report failures as text, not silence."""
    settings = _settings(context)
    if settings is None:
        await update.message.reply_text("System initializing...", parse_mode=None)
        return None
    if not _lab().lab_enabled(settings):
        await update.message.reply_text(DISABLED_TEXT, parse_mode=None)
        return None
    try:
        return await run_blocking(operation=name, fn=lambda: build(settings), timeout_s=LAB_TIMEOUT_S)
    except BlockingCallTimeout:
        await update.message.reply_text(
            f"{name} timed out after {LAB_TIMEOUT_S}s. Try again shortly.",
            parse_mode=None,
        )
    except Exception as exc:
        log.error("strategy_lab_command_failed", command=name, error=str(exc)[:300])
        await update.message.reply_text(f"Error: {str(exc)[:200]}", parse_mode=None)
    return None


# --------------------------------------------------------------------------- #
# /experiments
# --------------------------------------------------------------------------- #


@authorized
async def experiments_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Running experiments, their arms, and the tier distribution (Spec Q §13)."""
    data = await _guarded(update, context, "experiments_command", _lab().experiment_overview)
    if data is None:
        return
    await _reply(update, render_experiments(data))


def render_experiments(data) -> str:
    """MarkdownV2 for `/experiments`. Pure: a function of the payload only."""
    experiments = list(data.get("experiments") or ())
    if not experiments:
        return (
            "*🧪 STRATEGY LAB — EXPERIMENTS*\n\n"
            "No experiment is registered yet\\. The configured name is "
            f"`{_code(str(data.get('configured', '')))}`; it registers on the "
            "first scan after STRATEGY\\_LAB\\_SHADOW\\_ENABLED is true\\."
        )
    lines = ["*🧪 STRATEGY LAB — EXPERIMENTS*", ""]
    for experiment in experiments:
        marker = " ⭐" if experiment.get("configured") else ""
        lines.append(
            f"*{escape_md(experiment['name'])}*{marker} — `{_code(experiment['status'])}`"
        )
        lines.append(
            f"  primary metric: `{_code(str(experiment.get('primary_metric') or ''))}` "
            f"\\| planned variants: `{experiment.get('planned_variants', 0)}`"
        )
        arms = list(experiment.get("arms") or ())
        tiers: dict[str, int] = {}
        for arm in arms:
            tiers[arm["mode"]] = tiers.get(arm["mode"], 0) + 1
        tier_text = ", ".join(f"{count} {mode}" for mode, count in sorted(tiers.items()))
        lines.append(f"  arms: `{len(arms)}` \\({escape_md(tier_text or 'none')}\\)")
        for arm in arms:
            lines.append(
                f"    `#{arm['arm_id']}` `{_code(arm['slug'])}@{_code(arm['version'])}` "
                f"— `{_code(arm['mode'])}`/`{_code(arm['status'])}` "
                f"\\| decisions `{arm['decisions']}`"
            )
        lines.append("")
    lines.append(
        "_Shadow arms send no broker order: `strategy_lab` imports no broker\\._"
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# /strategies
# --------------------------------------------------------------------------- #


@authorized
async def strategies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Champion, challengers, versions and status (Spec Q §13)."""
    data = await _guarded(update, context, "strategies_command", _lab().strategy_overview)
    if data is None:
        return
    await _reply(update, render_strategies(data))


def render_strategies(data) -> str:
    rows = list(data.get("strategies") or ())
    lines = ["*🧬 STRATEGY LAB — ROSTER*", ""]
    for row in rows:
        role = "champion" if row.get("champion") else "challenger"
        replay = "replayable" if row.get("historically_replayable") else "forward\\-only"
        lines.append(
            f"*{escape_md(row['slug'])}* `{_code(row['version'])}` — {escape_md(role)}"
        )
        lines.append(
            f"  status `{_code(row['status'])}` \\| scope `{_code(row['scope'])}` "
            f"\\| policy `{_code(row['policy'])}` \\| {replay}"
        )
        lines.append(
            f"  expected hold: `{row['expected_holding_days']}` calendar days"
        )
        lines.append("")
    lines.append("_`/strategy <slug>` for one strategy's decisions and results\\._")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# /strategy <slug>
# --------------------------------------------------------------------------- #


@authorized
async def strategy_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """One strategy: decision count, matured count, results, drawdown, warnings."""
    args = context.args or []
    if not args:
        await update.message.reply_text(
            "Usage: /strategy <slug>  (see /strategies for the roster)",
            parse_mode=None,
        )
        return
    slug = str(args[0]).strip().lower()

    def build(settings):
        return {
            "detail": _lab().strategy_detail(settings, slug),
            "scoreboard": _lab().scoreboard(settings),
        }

    data = await _guarded(update, context, "strategy_command", build)
    if data is None:
        return
    await _reply(update, render_strategy(slug, data["detail"], data["scoreboard"]))


def render_strategy(slug: str, detail, payload) -> str:
    """One strategy card. Every performance figure is copied out of `payload`."""
    if detail is None:
        return escape_md(DISABLED_TEXT)
    if detail.get("unknown"):
        roster = ", ".join(detail.get("roster") or ())
        return (
            f"Unknown strategy `{_code(slug)}`\\. The roster is "
            f"`{_code(roster)}`\\."
        )

    lines = [
        f"*🧬 {escape_md(detail['slug'])}* `{_code(detail['version'])}`",
        "",
        f"status `{_code(detail['status'])}` \\| scope `{_code(detail['scope'])}` "
        f"\\| policy `{_code(detail['policy'])}`",
        "",
        f"_{escape_md(_clip(detail.get('hypothesis') or '', 300))}_",
        "",
    ]
    if not detail.get("historically_replayable"):
        lines.append(
            f"⚠️ forward\\-only: {escape_md(_clip(detail.get('replayability_reason') or '', 200))}"
        )
        lines.append("")

    arms = list(detail.get("arms") or ())
    if not arms:
        lines.append("No arm for this strategy under the configured experiment\\.")
    for arm in arms:
        lines.append(
            f"*arm `#{arm['arm_id']}`* — `{_code(arm['mode'])}`/`{_code(arm['status'])}`"
        )
        lines.append(
            f"  decisions `{arm['decisions']}` \\(long `{arm['long']}`, "
            f"flat `{arm['flat']}`, abstain `{arm['abstain']}`\\)"
        )
        lines.append(
            f"  executions `{arm['executions']}` \\| closed `{arm['closed']}` "
            f"\\| open `{arm['open']}`"
        )
    lines.append("")
    lines.extend(_scoreboard_lines(payload, only_slug=slug))
    recent = list(detail.get("recent_decisions") or ())
    if recent:
        lines += ["", "*RECENT DECISIONS*"]
        for row in recent:
            strength = row.get("signal_strength")
            strength_text = f"{strength:.2f}" if isinstance(strength, (int, float)) else "—"
            lines.append(
                f"  `{_code(row['ticker'])}` `{_code(row['action'])}` "
                f"s\\=`{_code(strength_text)}` \\(arm `#{row['arm_id']}`\\)"
            )
    return "\n".join(lines)


def _clip(text: str, limit: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# The scoreboard section, shared with the weekly report
# --------------------------------------------------------------------------- #


def _code(value) -> str:
    """Escape a value for a MarkdownV2 **code span**, which is not prose.

    Telegram's rule differs between the two: outside a code span every special
    character must be escaped (`escape_md`), but *inside* one only a backtick
    and a backslash may be — everything else is literal, and a stray `\\_` would
    be shown to the reader as a backslash rather than swallowed. That matters
    here more than anywhere else in the bot, because almost every value this
    card prints is a slug or a semantic version: `momentum_v1@1.0.0/shadow`
    escaped as prose renders as `momentum\\_v1@1\\.0\\.0/shadow`.
    """
    return str(value).replace("\\", "\\\\").replace("`", "\\`")


def _fmt(value) -> str:
    """Format a value already computed elsewhere. Never arithmetic."""
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def scoreboard_lines(payload, *, only_slug: str = "") -> list[str]:
    """The operator-facing scorecard section, as MarkdownV2 lines.

    Reads `scripts/strategy_lab_scoreboard.build_scorecard`'s payload and prints
    it. Sample size, maturity, costs, drawdown, benchmark, uncertainty,
    correlation and every warning code come out of that dict verbatim; nothing
    here computes, rounds, selects or characterises a number (Spec Q §10,
    AGENTS.md §1.2). The clean and exploratory sections stay separate, because
    the payload keeps them separate and combining them would be the exact
    dishonesty the split exists to prevent.
    """
    return _scoreboard_lines(payload, only_slug=only_slug)


def _scoreboard_lines(payload, *, only_slug: str = "") -> list[str]:
    if not payload:
        return ["_No scorecard yet: no experiment, or no settled shadow trade\\._"]

    inputs = payload.get("inputs") or {}
    variants = payload.get("variants") or {}
    costs = inputs.get("costs") or {}
    floors = inputs.get("floors") or {}
    lines = [
        "*📊 SCOREBOARD*",
        f"  cutoff `{_code(str(inputs.get('cutoff_utc', '')))}` "
        f"\\| metric `{_code(str(inputs.get('primary_metric', '')))}`",
        f"  costs `{_code(_costs_text(costs))}` "
        f"\\| floors `{_code(_floors_text(floors))}`",
        f"  variants: declared `{variants.get('planned_variants', 0)}`, "
        f"run `{variants.get('n_tried', 0)}`, "
        f"testing denominator `{variants.get('n_trials', 0)}`",
    ]

    from strategy_lab.replay import EVIDENCE_CLEAN, EVIDENCE_EXPLORATORY

    for evidence, title in (
        (EVIDENCE_CLEAN, "CLEAN REPLAY / FORWARD SHADOW"),
        (EVIDENCE_EXPLORATORY, "EXPLORATORY — archival\\_reconstructed"),
    ):
        section = (payload.get("sections") or {}).get(evidence)
        if not section:
            continue
        winner = section.get("winner")
        lines += [
            "",
            f"*{title}* — `{_code(str(section.get('label', '')))}`"
            + (f" \\| winner `{_code(str(winner))}`" if winner else ""),
        ]
        if evidence != EVIDENCE_CLEAN:
            lines.append(
                "  _reconstructed from data with no availability provenance: "
                "never a clean metric, never a promotion gate\\._"
            )
        for row in section.get("arms") or ():
            if only_slug and not str(row.get("arm", "")).startswith(only_slug):
                continue
            lines.append(f"  `{_code(str(row.get('arm', '')))}` — `{_code(str(row.get('status', '')))}`")
            lines.append(
                f"    n matured `{row.get('n_matured', 0)}` \\| open `{row.get('n_open', 0)}` "
                f"\\| dates `{row.get('n_distinct_dates', 0)}`"
            )
            lines.append(
                f"    net `{_code(_fmt(row.get('mean_net_pct')))}%` "
                f"\\| R `{_code(_fmt(row.get('mean_r')))}` "
                f"\\| win `{_code(_fmt(row.get('win_rate')))}`"
            )
            lines.append(
                f"    max DD `{_code(_fmt(row.get('max_drawdown_pct')))}%` "
                f"\\| TUW `{_code(_fmt(row.get('time_under_water_days')))}d` "
                f"\\| vs benchmark `{_code(_fmt(row.get('benchmark_relative_pct')))}pp`"
            )
            interval = row.get("uncertainty")
            if interval:
                lines.append(
                    f"    {escape_md(_fmt(interval.get('estimate')))} "
                    f"\\[{escape_md(_fmt(interval.get('lower')))}, "
                    f"{escape_md(_fmt(interval.get('upper')))}\\] at "
                    f"{escape_md(_fmt(interval.get('level')))} "
                    f"\\(n\\_eff {escape_md(_fmt(interval.get('n_eff')))}, "
                    f"seed {escape_md(_fmt(interval.get('seed')))}\\)"
                )
                lines.append(
                    f"    family\\-adjusted lower "
                    f"`{_code(_fmt(row.get('adjusted_lower')))}` over "
                    f"`{row.get('n_trials', 0)}` trial\\(s\\); step\\-M "
                    f"`{_code(_fmt(row.get('stepm_rejected')))}`"
                )
            else:
                lines.append("    no interval computed")
            for warning in row.get("warnings") or ():
                lines.append(f"    ⚠️ `{_code(str(warning))}`")

        for pair in section.get("overlaps") or ():
            if only_slug and only_slug not in f"{pair.get('left')}{pair.get('right')}":
                continue
            lines.append(
                f"  overlap `{_code(str(pair.get('left')))}` vs "
                f"`{_code(str(pair.get('right')))}`: shared "
                f"`{pair.get('n_shared', 0)}`, Jaccard "
                f"`{_code(_fmt(pair.get('jaccard')))}`, correlation "
                f"`{_code(_fmt(pair.get('correlation')))}` "
                f"\\({escape_md(str(pair.get('correlation_note', '')))}\\)"
            )
        for reason in section.get("reasons") or ():
            lines.append(f"  · {escape_md(_clip(str(reason), 160))}")

    for refusal in payload.get("refusals") or ():
        lines.append(
            f"  ⛔ `{_code(str(refusal.get('arm', '')))}`: "
            f"{escape_md(_clip(str(refusal.get('reason', '')), 160))}"
        )
    warnings = payload.get("warnings") or ()
    if warnings:
        lines.append("  warnings: " + ", ".join(f"`{_code(str(w))}`" for w in warnings))
    lines.append(
        "  _Recommendations are informational\\. Promotion is owner\\-only and "
        "nothing here performs one\\._"
    )
    return lines


def _costs_text(costs) -> str:
    if not costs:
        return "none — return metrics are blocked without a cost model"
    return (
        f"slip {costs.get('slippage_bps')}bps, "
        f"half-spread {costs.get('half_spread_bps')}bps, "
        f"commission {costs.get('commission_bps')}bps"
    )


def _floors_text(floors) -> str:
    if not floors:
        return "none"
    return ", ".join(f"{key} {value}" for key, value in sorted(floors.items()))


# --------------------------------------------------------------------------- #
# Pause / resume
# --------------------------------------------------------------------------- #


async def _set_paused(update: Update, context: ContextTypes.DEFAULT_TYPE, *, paused: bool):
    settings = _settings(context)
    if settings is None:
        await update.message.reply_text("System initializing...", parse_mode=None)
        return
    if not _lab().lab_enabled(settings):
        await update.message.reply_text(DISABLED_TEXT, parse_mode=None)
        return
    args = context.args or []
    name = str(args[0]).strip() if args else str(
        getattr(settings, "strategy_lab_experiment", "") or ""
    )
    if not name:
        await update.message.reply_text(
            "Usage: /pause_experiment <name|id>  (see /experiments)", parse_mode=None
        )
        return
    verb = "pause_experiment" if paused else "resume_experiment"
    try:
        result = await run_blocking(
            operation=verb,
            fn=lambda: _lab().set_experiment_paused(settings, name, paused=paused),
            timeout_s=LAB_TIMEOUT_S,
        )
    except BlockingCallTimeout:
        await update.message.reply_text(f"{verb} timed out.", parse_mode=None)
        return
    except Exception as exc:
        log.error("strategy_lab_command_failed", command=verb, error=str(exc)[:300])
        await update.message.reply_text(f"Error: {str(exc)[:200]}", parse_mode=None)
        return

    if not result.get("ok"):
        await update.message.reply_text(
            f"Refused: {str(result.get('error', ''))[:300]}", parse_mode=None
        )
        return
    changed = "" if result.get("changed") else " (already)"
    await update.message.reply_text(
        f"Experiment {result['name']} is now {result['status']}{changed}.\n"
        + (
            "A paused experiment's arms refuse at the runner, so the shadow pass "
            "stops writing as well as stops reporting."
            if paused
            else "Arms will run again on the next scan."
        ),
        parse_mode=None,
    )


@authorized
async def pause_experiment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/pause_experiment [name]` — Spec Q §13. Defaults to the configured one."""
    await _set_paused(update, context, paused=True)


@authorized
async def resume_experiment_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/resume_experiment [name]` — Spec Q §13."""
    await _set_paused(update, context, paused=False)


# --------------------------------------------------------------------------- #
# /promote_arm, /demote_arm and the confirmation callback (Spec Q §13, PR 6)
# --------------------------------------------------------------------------- #
#
# PR 4's header said these were deliberately absent, because "an owner-only
# button that calls a promotion path which does not exist yet would be worse than
# no button". The path exists now: `strategy_lab/promotion.py` holds the bindings
# and `orchestrator/strategy_lab_promotion.py` the deployment gates. What lives
# here is only the rendering and the callback plumbing.
#
# Nothing here decides anything. The card prints the plan's own refusal lists
# verbatim, the recommendation label is computed arithmetically from the stored
# counts and the configured floors, and the word "promote" never appears as a
# system recommendation — Spec Q §3 keeps promotion authority with the owner.
#
# `/live_kill` is Phase 6's switch and is untouched: one kill switch, one row, one
# command (`bot/handlers/proposals.py`).

PROMOTE_TIMEOUT_S = 120


def _pending(context):
    """The process-local pending-promotion store, created on first use."""
    from orchestrator.strategy_lab_promotion import PendingPromotions

    store = context.bot_data.get("strategy_lab_pending_promotions")
    settings = _settings(context)
    if store is None or getattr(store, "settings", None) is not settings:
        store = PendingPromotions(settings)
        context.bot_data["strategy_lab_pending_promotions"] = store
    return store


def _adapters(context):
    """The venue -> adapter map `main.py` wired, or an empty one.

    An empty map is not a failure mode to paper over: the live capability gate
    treats a missing adapter as an adapter that cannot protect a position, so a
    deployment that never wired one cannot promote to live. That is the intended
    refusal (Spec L §5).
    """
    return context.bot_data.get("strategy_lab_adapters") or {}


async def _tier_change(update: Update, context: ContextTypes.DEFAULT_TYPE, *, to_tier: str | None):
    from strategy_lab.domain import ExecutionMode

    settings = _settings(context)
    if settings is None:
        await update.message.reply_text("System initializing...", parse_mode=None)
        return
    if not _lab().lab_enabled(settings):
        await update.message.reply_text(DISABLED_TEXT, parse_mode=None)
        return

    args = list(context.args or [])
    verb = "demote_arm" if to_tier == "down" else "promote_arm"
    usage = (
        f"Usage: /{verb} <source_arm_id> <tier> [reason...]\n"
        "  tier: shadow | paper | live   (see /experiments for arm ids)"
    )
    if len(args) < 2:
        await update.message.reply_text(usage, parse_mode=None)
        return
    try:
        source_arm_id = int(args[0])
        mode = ExecutionMode(str(args[1]).strip().lower())
    except (TypeError, ValueError):
        await update.message.reply_text(usage, parse_mode=None)
        return
    reason = " ".join(args[2:]).strip() or f"owner {verb} via Telegram"
    owner_id = str(update.effective_chat.id)
    owner = str(getattr(settings, "strategy_lab_experiment_owner", "") or "bryan")

    def build():
        from database.db import get_session
        from orchestrator import strategy_lab_promotion as wiring

        with get_session() as session:
            request = wiring.build_request(
                session,
                settings,
                source_arm_id=source_arm_id,
                to_mode=mode,
                owner=owner,
                reason=reason,
            )
            session.commit()
        return wiring.plan(settings, request, adapters=_adapters(context)), request

    try:
        plan, request = await run_blocking(operation=verb, fn=build, timeout_s=PROMOTE_TIMEOUT_S)
    except BlockingCallTimeout:
        await update.message.reply_text(f"{verb} timed out.", parse_mode=None)
        return
    except Exception as exc:
        log.error("strategy_lab_command_failed", command=verb, error=str(exc)[:300])
        await update.message.reply_text(f"Refused: {str(exc)[:400]}", parse_mode=None)
        return

    if not plan.confirmable:
        await _reply(update, render_promotion(plan, confirmable=False))
        return

    try:
        _token, confirm_cb, cancel_cb, expires_at = _pending(context).offer(
            request, owner_id=owner_id
        )
    except Exception as exc:
        # An unset EXECUTION_APPROVAL_SECRET lands here, and the card is not
        # rendered with buttons — a confirmation nobody can authenticate is not a
        # confirmation (Spec L §6.3).
        await update.message.reply_text(
            f"Cannot mint a confirmation: {str(exc)[:300]}", parse_mode=None
        )
        return

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("✅ Confirm tier change", callback_data=confirm_cb),
            InlineKeyboardButton("❌ Cancel", callback_data=cancel_cb),
        ]]
    )
    text = render_promotion(plan, confirmable=True, expires_at=expires_at)
    await _reply_with_buttons(update, text, markup)


async def _reply_with_buttons(update: Update, text: str, markup) -> None:
    """Send the card with its buttons, falling back to plain text.

    The confirmation card is the one message in this module whose *buttons* are
    the point: a MarkdownV2 rejection that lost them would leave the owner with a
    promotion they cannot confirm and no way to tell why. So a formatting failure
    degrades to an unformatted card with the same buttons rather than to silence.
    """
    try:
        await update.message.reply_text(
            text, parse_mode="MarkdownV2", reply_markup=markup
        )
    except Exception as exc:
        log.warning("promotion_card_markdown_failed", error=str(exc)[:200])
        plain = text.replace("\\", "").replace("*", "").replace("`", "")
        await update.message.reply_text(plain, parse_mode=None, reply_markup=markup)


def render_promotion(plan, *, confirmable: bool, expires_at=None) -> str:
    """The promotion card. Pure: a function of the plan only.

    Spec Q §13 names what it must show — the source arm's immutable strategy
    version, the evidence snapshot, the warnings, the proposed inactive target arm
    and the budget. All five are here, and so is every refusal, because a card
    that hides the third of three reasons is how an owner ends up re-running a
    command five times.
    """
    if plan is None:
        return escape_md(DISABLED_TEXT)
    evidence = plan.evidence
    kind = plan.kind.value
    lines = [
        f"*🔁 {escape_md(kind.upper())} — owner confirmation required*",
        "",
        f"*source* arm `#{plan.source['arm_id']}` "
        f"`{_code(plan.source['slug'])}@{_code(plan.source['version'])}` "
        f"— `{_code(plan.source['mode'])}`/`{_code(plan.source['status'])}`",
        f"*target* arm `#{plan.target['arm_id']}` "
        f"— `{_code(plan.target['mode'])}`/`{_code(plan.target['status'])}` "
        f"\\| risk budget `{_fmt(plan.target['risk_budget'])}`",
        f"*strategy version id* `{plan.source['strategy_version_id']}` "
        f"\\(shared: a rule change is a new version and needs its own evidence\\)",
        "",
        f"*evidence* snapshot `#{evidence.metric_snapshot_id}` at "
        f"`{_code(str(evidence.cutoff_utc or ''))}`",
        f"  decisions `{evidence.n_decisions}` \\| matured `{evidence.n_matured}` "
        f"\\| closed `{evidence.n_closed}`",
        f"  floor `{_code(evidence.floor_name)}` \\= `{evidence.floor_value}` — "
        + ("met" if evidence.floor_met else "*NOT met*"),
        f"  complete: {'yes' if evidence.complete else '*no*'}"
        + (f" \\(missing {escape_md(', '.join(evidence.missing))}\\)" if evidence.missing else ""),
        f"  warnings acknowledged: {'yes' if evidence.warnings_acknowledged else '*no*'}",
    ]
    for warning in evidence.warnings:
        lines.append(f"  ⚠️ `{_code(str(warning))}`")
    lines += ["", f"*recommendation* `{_code(plan.recommendation)}`"]

    if plan.refusals or plan.external_refusals:
        lines += ["", "*REFUSED*"]
        for item in list(plan.refusals) + list(plan.external_refusals):
            lines.append(f"  ⛔ {escape_md(_clip(str(item), 300))}")
    for note in plan.notes:
        lines.append(f"  _{escape_md(_clip(str(note), 300))}_")
    if confirmable:
        lines += [
            "",
            "_Confirming appends an append\\-only `promotion_events` row and "
            "activates the target arm\\. It is **not** approval of any entry: "
            "every proposed live execution still needs its own signed, expiring, "
            "single\\-use callback\\._",
        ]
        if expires_at is not None:
            lines.append(
                f"_This confirmation expires at `{_code(expires_at.isoformat())}`Z\\._"
            )
    else:
        lines += ["", "_Nothing was changed\\. No arm was activated\\._"]
    return "\n".join(lines)


@authorized
async def promote_arm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/promote_arm <source_arm_id> <tier> [reason]` — Spec Q §13."""
    await _tier_change(update, context, to_tier="up")


@authorized
async def demote_arm_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/demote_arm <source_arm_id> <tier> [reason]` — the same machinery, downward.

    A demotion additionally stands the source arm down, which is the one
    asymmetry: promoting says nothing about the arm you promoted from, demoting
    says everything.
    """
    await _tier_change(update, context, to_tier="down")


@authorized
async def promotions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """`/promotions` — the append-only audit trail."""

    def build(settings):
        from database.db import get_session
        from strategy_lab import promotion as pm

        with get_session() as session:
            return pm.promotion_history(session, limit=15)

    rows = await _guarded(update, context, "promotions_command", build)
    if rows is None:
        return
    await _reply(update, render_promotions(rows))


def render_promotions(rows) -> str:
    if not rows:
        return (
            "*🔁 PROMOTION EVENTS*\n\nNone\\. Every arm in the tournament is where "
            "it was created, and only an owner confirmation moves one\\."
        )
    lines = ["*🔁 PROMOTION EVENTS* \\(newest first\\)", ""]
    for row in rows:
        lines.append(
            f"`#{row['promotion_id']}` `{_code(row['kind'])}` "
            f"arm `#{row['source_arm_id']}` → `#{row['target_arm_id']}` "
            f"\\(`{_code(row['from_mode'])}` → `{_code(row['to_mode'])}`\\)"
        )
        lines.append(
            f"  owner `{_code(row['owner'])}` \\| evidence "
            f"`#{row['evidence_metric_snapshot_id']}` \\| budget "
            f"`{_fmt(row['previous_risk_budget'])}` → `{_fmt(row['new_risk_budget'])}`"
        )
        lines.append(f"  at `{_code(str(row['created_at'] or ''))}` — {escape_md(_clip(row['reason'], 160))}")
    lines.append("")
    lines.append("_Append\\-only\\. A correction is another event, never an edit\\._")
    return "\n".join(lines)


async def handle_promotion_callback(query, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Dispatch an `slpr:`/`slpx:` confirmation. Called from the callback router.

    The router already checked authorisation; this checks again, because a tier
    change is the one owner-only control that can put an arm on live capital. The
    real controls are the ones inside `PendingPromotions.take` — owner binding,
    expiry, single use, signature — and the refusal recomputation inside
    `strategy_lab.promotion.confirm`.
    """
    from bot.auth import is_authorized

    if not is_authorized(query.message.chat_id):
        log.warning("unauthorized_promotion_callback", chat_id=query.message.chat_id)
        return
    settings = _settings(context)
    if settings is None:
        await query.message.reply_text("System initializing...", parse_mode=None)
        return

    store = _pending(context)
    owner_id = str(query.message.chat_id)
    try:
        action, token, presented = store.parse(query.data or "")
    except Exception as exc:
        await query.message.reply_text(f"Could not read that: {str(exc)[:200]}", parse_mode=None)
        return

    if action == "cancel":
        store.cancel(token)
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(
            "Cancelled. No promotion_event was written and no arm was activated.",
            parse_mode=None,
        )
        return

    def work():
        from orchestrator import strategy_lab_promotion as wiring

        request = store.take(token, presented=presented, owner_id=owner_id)
        return wiring.confirm(settings, request, adapters=_adapters(context))

    try:
        result = await run_blocking(
            operation="promotion_confirm", fn=work, timeout_s=PROMOTE_TIMEOUT_S
        )
    except BlockingCallTimeout:
        await query.message.reply_text(
            "The confirmation is still running. Check /promotions before "
            "re-running — the confirmation is single-use.",
            parse_mode=None,
        )
        return
    except Exception as exc:
        await query.edit_message_reply_markup(reply_markup=None)
        await query.message.reply_text(f"Not promoted: {str(exc)[:500]}", parse_mode=None)
        return

    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text(
        f"{result['kind']} #{result['promotion_id']} recorded: arm "
        f"#{result['source_arm_id']} ({result['from_mode']}) → arm "
        f"#{result['target_arm_id']} ({result['to_mode']}), risk budget "
        f"{result['new_risk_budget']}.\n"
        "This is not approval of any entry: every proposed execution still needs "
        "its own signed, expiring, single-use approval callback.",
        parse_mode=None,
    )
