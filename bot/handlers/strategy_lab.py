"""Owner-only Strategy Lab commands (Spec Q §13, §15 PR 4).

Five commands, all read-only except the two that move an experiment's status:

``/experiments``       running experiments, their arms and the tier distribution
``/strategies``        the roster: champion, challengers, versions, statuses
``/strategy <slug>``   one strategy's decisions, executions and recent activity
``/pause_experiment``  and ``/resume_experiment`` — Spec Q §13's pause controls
                       (by name or row id; the configured experiment by default)

**Not here, on purpose.** ``/promote_arm`` and the live-tier controls are Spec Q
§13 commands whose safety services are PR 5 and PR 6: an owner-only button that
calls a promotion path which does not exist yet would be worse than no button.
``/live_kill`` already exists from Phase 6 (``bot/handlers/proposals.py``) and is
untouched.

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
