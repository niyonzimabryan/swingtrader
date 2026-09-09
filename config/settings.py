from dotenv import load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings
from typing import Optional

# Pre-load .env via python-dotenv to work around pydantic-settings parser
# dropping keys with certain character patterns in their values.
load_dotenv(override=True)


class Settings(BaseSettings):
    # --- API Keys ---
    anthropic_api_key: str = ""
    alpaca_api_key: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = "https://paper-api.alpaca.markets"
    finnhub_api_key: str = ""
    fmp_api_key: str = ""
    alpha_vantage_api_key: str = ""
    fred_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- Trading Parameters ---
    portfolio_value: float = 100_000.0
    base_position_pct: float = 0.05
    max_position_pct: float = 0.10
    min_position_pct: float = 0.02
    max_portfolio_exposure: float = 0.80
    max_sector_exposure: float = 0.30
    max_concurrent_positions: int = 8
    default_stop_loss_pct: float = 0.05
    max_stop_loss_pct: float = 0.08
    max_holding_days: int = 20
    drawdown_circuit_breaker_pct: float = 0.10
    daily_loss_halt_pct: float = 0.03

    # --- Broker Selection & Execution Mode ---
    broker_primary: str = "alpaca"
    broker_paper: str = "alpaca"
    execution_mode: str = "paper"  # review_only | paper | live
    allow_live_trading: bool = False
    require_order_review: bool = True

    # --- Robinhood Agentic Trading MCP ---
    robinhood_mcp_url: str = "https://agent.robinhood.com/mcp/trading"
    robinhood_account_number: str = ""
    robinhood_mcp_auth_token: str = ""
    robinhood_mcp_headers_json: str = ""
    # Fernet key (urlsafe base64) for the encrypted on-disk OAuth token store.
    # Generate once with: python -m scripts.robinhood_auth --gen-key
    # Store as a deployment secret; the app never writes it back.
    token_encryption_key: str = ""
    robinhood_account_budget: float = 25.0
    robinhood_max_order_notional: float = 5.0
    robinhood_max_daily_notional: float = 10.0
    robinhood_max_open_positions: int = 3
    robinhood_allow_fractional: bool = True
    robinhood_allow_options: bool = False
    robinhood_allowed_symbols: str = ""
    robinhood_blocked_symbols: str = ""
    robinhood_order_type: str = "market"  # market for small-dollar fractional, limit for whole-share control
    robinhood_market_hours: str = "regular_hours"

    # --- Alpaca Paper Trading Safety ---
    alpaca_enabled: bool = True
    alpaca_paper_only: bool = True

    # --- Scoring ---
    memo_threshold: float = 0.55  # Production threshold (override via .env for testing)
    high_conviction_threshold: float = 0.75
    catalyst_escalation_threshold: int = 3  # Haiku score 1-5 to trigger Sonnet
    # BRY-301: scan-complete alert fires once per scan when at least this
    # fraction of scanned tickers failed processing (billing outage / LLM
    # provider down) — a scan can otherwise "complete" with zero memos silently.
    catalyst_failure_rate_alert_threshold: float = 0.9

    # --- Spec I0: Funnel guardrails (protect the first healthy scan) ---
    # After tier-2 screening, keep only the top-N escalated tickers by Gemini
    # score (stable sort, ties by score then symbol). The fixed screener can
    # escalate 8/10 tickers; without this cap a healthy scan pushes 100+ tickers
    # into catalyst (cost + duration bomb).
    tier2_max_escalations: int = 25
    # Hard cap on the merged scan list entering catalyst, applied AFTER the
    # existing priority ordering (tier2 > tier1 > discovery > watchlist >
    # universe) so it never evicts a higher-priority source for a lower one.
    scan_max_catalyst_tickers: int = 40

    # --- Spec I1: Shadow calibration ledger ---
    # Per nightly run, cap how many matured rows get forward returns computed
    # (yfinance/FMP price fetches) so the job stays bounded.
    shadow_returns_max_per_run: int = 300

    # --- Spec I2: Paper autonomy sandbox ---
    # Master switch. Auto-approval is structurally impossible outside the Alpaca
    # PAPER adapter regardless of this flag (hard safety guard).
    auto_approve_paper: bool = False
    auto_approve_min_score: float = 0.55       # memo cohort floor
    exploration_band_enabled: bool = True
    exploration_min_score: float = 0.45        # band = [exploration_min, auto_approve_min)
    auto_max_concurrent_positions: int = 8
    auto_max_new_positions_per_scan: int = 4   # memo cohort
    exploration_max_new_per_scan: int = 2      # exploration cohort
    exploration_position_pct_factor: float = 0.5  # smaller default size for exploration

    # --- Scheduling (ET hours) ---
    pre_market_hour: int = 7
    midday_hour: int = 12
    post_market_hour: int = 17
    scheduler_misfire_grace_time_s: int = 7200

    # --- Monitor reliability (Spec B: hang recovery) ---
    # Every broker/price call in the position & order monitors runs under this
    # timeout so a stuck SDK call can never block the async loop forever.
    monitor_broker_call_timeout_s: int = 30
    # Watchdog: if a monitor loop hasn't ticked within this many seconds during
    # market hours, the process exits(1) so Railway's ON_FAILURE policy restarts it.
    monitor_watchdog_stale_s: int = 900
    # How often the watchdog checks the monitors' last-tick timestamps.
    monitor_watchdog_interval_s: int = 300
    # Daily clean self-restart (pre-market) so no hang survives more than a day.
    # "HH:MM" 24h ET; None/empty disables. Runs regardless of SCHEDULER_ENABLED.
    daily_restart_time_et: Optional[str] = "08:57"

    # --- Database ---
    database_url: str = "sqlite:///swing_trader.db"
    # Directory for sidecar files that must live beside the data, not on the
    # ephemeral container FS: the pattern-backfill queue, the encrypted
    # Robinhood token blob. It used to be derived from a `sqlite:///` path,
    # which silently became the working directory the moment DATABASE_URL
    # pointed at Postgres. Set it explicitly (Railway: /data); left empty it
    # still falls back to the SQLite file's directory. See `data_dir()`.
    data_dir: str = ""

    # --- Workspace API / MCP service (Spec K) ---
    # Separate process from the bot. Off by default: with the flag false the
    # service still answers /health so a deploy is observable, and refuses
    # every authenticated call with 503.
    workspace_api_enabled: bool = False
    workspace_host: str = "0.0.0.0"
    workspace_port: int = 8000
    # Read by clients (scripts, docs, .mcp.json), not by the server itself.
    workspace_base_url: str = ""
    workspace_token: str = ""
    # Spec K section 4.1: 60 read and 10 write calls per minute per token.
    workspace_read_rate_limit_per_minute: int = 60
    workspace_write_rate_limit_per_minute: int = 10
    # OAuth 2.1 + dynamic client registration is designed but not implemented;
    # see workspace/oauth.py. With this true the service advertises protected
    # resource metadata and nothing else changes.
    workspace_oauth_enabled: bool = False

    # --- Model Selection ---
    # Override scoring tier model (default: opus)
    scoring_model: str = "claude-opus-4-6"
    analyst_model: str = "claude-sonnet-5"
    filter_model: str = "claude-haiku-4-5-20251001"

    # --- V2: Web Search & Discovery ---
    web_search_provider: str = "gemini"  # "gemini" (default) or "anthropic"
    discovery_max_tickers: int = 12
    discovery_model: str = "claude-sonnet-5"  # Discovery uses Sonnet, NOT Haiku
    discovery_output_max_tokens: int = 8192
    discovery_max_searches: int = 8

    # --- V2: Extended Thinking ---
    discovery_thinking_budget: int = 0      # Thinking tokens for discovery scan (was 10000; search quality drives discovery, not thinking)
    opus_thinking_budget: int = 16000       # Thinking tokens for Opus evaluation

    # --- Gemini Flash Screening (Tier 2) ---
    gemini_api_key: str = ""
    gemini_flash_model: str = "gemini-2.5-flash"
    # gemini-3.1-pro-preview refuses to invoke Google Search whenever JSON output
    # is requested (verified 2026-07-09/11 with side-by-side probes: 0 queries vs
    # 3 queries/7-13 sources on 2.5-flash for identical prompts — BRY-300). All
    # three stages below combine grounding with JSON output, so they pin the GA
    # flash model. Revisit when a GA pro model passes the same probe.
    gemini_search_model: str = "gemini-2.5-flash"
    gemini_discovery_model: str = "gemini-2.5-flash"
    gemini_web_research_model: str = "gemini-2.5-flash"
    gemini_flash_escalation_threshold: float = 0.50  # Tickers scoring above this escalate to Sonnet
    # Grounded tier-2 responses carry a prose preamble before the JSON; 512/2048 truncate
    # mid-JSON (finish=MAX_TOKENS). 4096 fits reliably. NOTE: Google Search grounding is
    # incompatible with response_mime_type=application/json, so structured output is not an
    # option here — the screener relies on a large budget + robust extraction instead.
    gemini_flash_max_output_tokens: int = 4096
    web_research_max_searches: int = 5
    web_research_cache_enabled: bool = True
    web_research_cache_ttl_hours: int = 24

    # --- Historical Pattern Analog Engine ---
    perplexity_api_key: str = ""
    perplexity_search_enabled: bool = True
    perplexity_search_max_requests_per_run: int = 20
    pattern_event_search_provider: str = "gemini"  # gemini | perplexity | hybrid
    pattern_event_search_enabled: bool = True
    pattern_event_cache_ttl_days: int = 90
    pattern_peer_cache_ttl_days: int = 30
    pattern_max_peer_count: int = 20
    pattern_min_direct_matches: int = 5
    pattern_min_total_matches: int = 10
    pattern_max_search_queries_per_catalyst: int = 8
    pattern_max_events_per_query: int = 10
    pattern_embedding_provider: str = "gemini"  # gemini | perplexity | off
    pattern_analog_engine_enabled: bool = False
    pattern_stage_wallclock_budget_s: int = 45
    pattern_cold_ticker_async_backfill: bool = True
    pattern_price_source: str = "fmp"  # fmp | yfinance
    pattern_backfill_queue_path: str = ".pattern_backfill_queue.jsonl"
    pattern_inline_outcome_max_per_scan: int = 10  # cap on store-time outcome computation per scan
    pattern_backfill_max_tickers_per_run: int = 20  # cap on queue-drain work per scheduled run

    # --- V2: Deep Research (Phase C) ---
    openai_api_key: str = ""
    deep_research_provider: str = "gemini"  # "gemini" or "openai"
    deep_research_score_threshold: float = 0.75

    # --- Firecrawl (optional narrative scraper + paywall fallback) ---
    firecrawl_api_key: str = ""
    firecrawl_max_calls_per_scan: int = 50
    archive_is_enabled: bool = True

    # --- V2: Watchlist ---
    watchlist_haiku_threshold: int = 2  # Lower bar for watchlist tickers
    watchlist_max_size: int = 25

    # --- Langfuse Observability ---
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = "https://us.cloud.langfuse.com"

    # --- Pipeline Parallelization ---
    parallel_agents_enabled: bool = True
    parallel_agents_scope: str = "both"  # "ad_hoc" | "scan" | "both"
    parallel_workers_default: int = 3
    parallel_workers_degraded: int = 2
    parallel_timeout_fundamental_s: int = 180
    parallel_timeout_pattern_s: int = 300
    parallel_timeout_web_research_s: int = 300

    # --- Parallel Stability Controller ---
    parallel_auto_degrade_enabled: bool = True
    parallel_bad_run_window: int = 12
    parallel_bad_run_count_trigger: int = 3
    parallel_cooldown_runs: int = 20
    parallel_recovery_good_runs: int = 8
    parallel_alert_on_state_change: bool = True

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @field_validator("robinhood_order_type")
    @classmethod
    def validate_robinhood_order_type(cls, value: str) -> str:
        normalized = (value or "market").strip().lower()
        if normalized not in {"market", "limit"}:
            raise ValueError("ROBINHOOD_ORDER_TYPE must be 'market' or 'limit'")
        return normalized


def data_dir(settings) -> "Path":
    """Directory for sidecar files that must survive a container restart.

    Resolution order:

    1. ``DATA_DIR``, if set. This is the only answer that works once
       ``DATABASE_URL`` points at Postgres, which is why it exists — Phase 0a
       flagged the SQLite-derived path as a cutover bug.
    2. The directory holding the SQLite file, when ``DATABASE_URL`` is a
       ``sqlite:///`` URL with a directory component. Unchanged behaviour for
       the pre-cutover deploy, where ``/data/swing_trader.db`` puts sidecars on
       the mounted volume for free.
    3. The working directory.

    Accepts any object exposing ``data_dir``/``database_url`` attributes so test
    doubles work without a real Settings.
    """
    from pathlib import Path

    explicit = (getattr(settings, "data_dir", "") or "").strip()
    if explicit:
        return Path(explicit)

    url = getattr(settings, "database_url", "") or ""
    if url.startswith("sqlite:///"):
        parent = Path(url[len("sqlite:///"):]).parent
        if str(parent) not in ("", "."):
            return parent
    return Path.cwd()


def resolve_backfill_queue_path(settings) -> "Path":
    """Resolve the pattern backfill queue path, anchoring relative paths to the DB dir."""
    from pathlib import Path

    raw = getattr(settings, "pattern_backfill_queue_path", "") or ".pattern_backfill_queue.jsonl"
    path = Path(raw)
    if path.is_absolute():
        return path
    return data_dir(settings) / path
