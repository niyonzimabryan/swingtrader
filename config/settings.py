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

    # --- Research workspace (Spec M) ---
    # Dossiers, theses, invalidators, the decision journal, and the Markdown
    # mirror. Off by default: with the flag false the five research tools are
    # not registered on the MCP surface at all (advertising a tool that
    # refuses is worse than not advertising it), the daily invalidator check
    # is a no-op, and the mirror script refuses to write.
    research_workspace_enabled: bool = False
    # Spec M §6: a dossier section older than this is marked `stale` in every
    # response that includes it. Marked, never hidden.
    research_section_stale_days: int = 90
    # Spec M §5: where `scripts/sync_research_mirror.py` writes. Relative
    # paths resolve against the repository root.
    research_mirror_dir: str = "research"
    # Spec M §6 small-n floors. A Brier score needs ten resolved theses; the
    # calibration table needs forty, and uses three coarse buckets below a
    # hundred. Below the floor the answer is `insufficient`, not a number.
    research_brier_min_resolved: int = 10
    research_calibration_min_resolved: int = 40
    research_calibration_coarse_below: int = 100
    # --- Portfolio ledger and broker sync (Spec L) ---
    # Off by default, like every new capability. With the flag false the tables
    # exist, the read tools answer from whatever is in them (nothing, at first,
    # and they say so through `provenance.stale`), and no scheduled job runs.
    portfolio_sync_enabled: bool = False
    # Spec L section 4: 60 minutes intraday. Past it every tool response carries
    # stale=true, and any path feeding a proposal refuses rather than serves.
    portfolio_freshness_budget_minutes: int = 60
    # Hourly during market hours, plus one pre-market and one after the close.
    portfolio_sync_interval_minutes: int = 60
    portfolio_sync_pre_market_hour: int = 8
    portfolio_sync_post_close_hour: int = 16
    portfolio_sync_post_close_minute: int = 30
    # Spec L section 4 failure policy: a sync that would drop more than this
    # fraction of an account's known holdings writes nothing and pages.
    portfolio_mass_deletion_threshold: float = 0.5

    # --- Proposal -> approval -> execution lifecycle (Spec L §6, Phase 6) ---
    # Off by default, like every new capability. With the flag false the
    # `propose_order` tool is not registered at all (an advertised tool that
    # answers "disabled" is worse than an absent one) and the execution
    # service refuses every approval callback.
    phase6_execution_enabled: bool = False
    # Spec L §6.6. `risk_fraction` is a *fraction* of equity. A value at or
    # above this is a percentage typed as a fraction (0.5 meaning 0.5%) and is
    # refused rather than converted.
    risk_fraction_percentage_floor: float = 0.05
    # The hard cap. Above it a proposal is refused, never silently clamped.
    risk_fraction_hard_cap: float = 0.01
    # The evidenced budget: per-trade cap and daily notional.
    evidenced_risk_cap: float = 0.01
    evidenced_daily_notional: float = 0.0
    # The discretionary budget: separate per-trade cap and daily notional, so
    # judgment trades and evidenced trades are separate rows and a separate
    # query (Spec L §6.6).
    discretionary_risk_cap: float = 0.0025
    discretionary_daily_notional: float = 0.0
    # `advisory` (default): a non-positive lower bound re-labels the proposal
    # `discretionary` and prints the bound. `strict`: it sizes to zero and an
    # uncited proposal is refused. Owner decision 2026-09-08 — advisory.
    evidence_gate_mode: str = "advisory"
    # A cited cohort answer must be no older than this many trading sessions.
    citation_max_age_sessions: int = 5
    # Spec L §5.1: entry fills -> stop placed -> stop read back from the broker.
    # A position not read back as protected inside this window is marked
    # `unprotected`, pages, and blocks further entries.
    protection_window_seconds: int = 120
    protection_poll_interval_seconds: float = 2.0
    # How long an approval card stays valid. Single-use and owner-bound too.
    approval_ttl_seconds: int = 1800
    # HMAC key for the signed approval reference. No default: an unset secret
    # means no card can be minted, which is the correct failure.
    execution_approval_secret: str = ""
    # Concentration and sector caps for a Phase 6 proposal, over the COMBINED
    # book across every account (Spec L §5.1). Separate from the scan bot's
    # `max_position_pct` / `max_sector_exposure` so Phase 6 cannot move
    # production capital limits.
    proposal_max_position_pct: float = 0.10
    proposal_max_sector_pct: float = 0.30

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

    # --- Price plane (Spec N §4.2/§4.3, Phase 3p) ---
    # Off by default. Nothing in the price plane runs, and no vendor is called,
    # until this is true; `data/market_data.py` (yfinance) is untouched either way.
    price_plane_enabled: bool = False
    # fixture | sharadar. `fixture` reads the committed CSVs and needs no key.
    price_plane_source: str = "fixture"
    # Nasdaq Data Link key for the Sharadar tables. Never hardcoded, never logged.
    nasdaq_data_link_api_key: str = ""
    # Named vintage of the price file that backfills and audits write against.
    price_plane_snapshot: str = "dev"
    # `liquid_us_equity_v1`: top N by 20-session median dollar volume at each
    # month-end. Market-cap ranking waits for the Phase 3a share-count feed.
    liquid_universe_top_n: int = 500
    liquid_universe_window_sessions: int = 20
    # Delisting audit (Spec N §4.2): terminal return over this many sessions,
    # below this threshold, is a collapse rather than a stop.
    delisting_audit_window_sessions: int = 10
    delisting_audit_collapse_threshold: float = -0.60

    # --- Comparable-setups engine (Spec N, Phase 3c) ---
    # Off by default. When false the `compare_setups` and `cohort_detail` MCP
    # tools are not registered at all — an unregistered tool is a clearer
    # refusal than a registered one that answers "disabled".
    comparable_setups_enabled: bool = False
    # The stored universe a cohort's membership is read from, as of the event
    # date. A universe with no `universe_membership` rows for the period caps
    # the cohort at `archival_reconstructed` (Spec N §4.2).
    comparable_universe_slug: str = "liquid_us_equity_v1"
    # The named price-file vintage a cohort runs against. Its delisting audit
    # (Spec N §4.2) must be recorded, or the cohort cannot reach `vendor_pit`.
    comparable_price_snapshot: str = "dev"
    # The total-return benchmark every abnormal return is measured against.
    # A `security_uid` in `price_bars`, not a ticker: tickers are reused.
    comparable_benchmark_security_uid: str = ""
    # The execution policy the §5.3 policy leg replays under.
    comparable_execution_policy: str = "event_swing_14cal_v1"
    # Bootstrap replications for a `quick` answer. `full` uses the configured
    # comparables default (10,000); quick trades width for latency.
    comparable_quick_bootstrap_reps: int = 1000
    # Bootstrap replications for a `full` answer. Spec N §6.1 wants a stationary
    # block bootstrap, not a number of draws; 10,000 is the default and lowering
    # it widens nothing and only makes the interval noisier, so it is configurable
    # rather than fixed for the same reason the floors are.
    comparable_full_bootstrap_reps: int = 10_000
    # `TICKER:CIK,TICKER:CIK` — the join `market_cap_decile` needs, because the
    # price plane's security master has no CIK column and the SEC feed stamps a
    # ticker. Phase 4's entity-history plane replaces it with a stored,
    # point-in-time mapping. Empty means every name is refused a market-cap
    # decile rather than given one computed from a count it could not have had.
    comparable_cik_map: str = ""

    # --- Spec O Phase 3a: minimum SEC ingestion plane ---
    # Off by default. When false, filings.sec_minimal refuses to ingest; the
    # read helpers still work against whatever is already in the ledger.
    plane_sec_minimal_enabled: bool = False
    # SEC requires a self-identifying User-Agent of the form
    # "Sample Company Name AdminContact@<domain>.com" (webmaster FAQ, verified
    # in docs/research/2026-09-research-verification.md claim 13). There is no
    # default: a real contact address must never be baked into the repo, and a
    # made-up one is worse than none. filings.client raises until it is set.
    sec_user_agent: str = ""
    # SEC's published maximum is 10 requests/second. The client refuses a
    # higher value; lower it if EDGAR starts returning 429.
    sec_max_requests_per_second: float = 10.0
    sec_request_timeout_s: float = 30.0
    sec_max_retries: int = 4

    # --- Spec O Phase 4: the three evidence planes ---
    # All off by default. When false the plane's ingest refuses to run; reads
    # of whatever is already stored are unaffected, because a table is not a
    # capability.
    plane_filings_enabled: bool = False
    plane_macro_vintage_enabled: bool = False
    plane_news_enabled: bool = False

    # OpenFIGI — CUSIP -> FIGI -> ticker for ownership tables (Spec O section
    # 3.2). Free. Without a key: 25 requests/minute, 10 jobs per request; with
    # one: 25 requests per 6 seconds, 100 jobs (verification claim 15). The
    # client picks the right pair from whether the key is set.
    openfigi_api_key: str = ""
    openfigi_timeout_s: float = 30.0

    # Alpaca News (Benzinga-sourced) — the primary timestamped news source.
    # Credentials are the existing ALPACA_API_KEY / ALPACA_SECRET_KEY.
    # 200 requests/minute on the free market-data plan (verification claim 11,
    # strong secondary); lower it if Alpaca starts returning 429.
    alpaca_news_base_url: str = "https://data.alpaca.markets"
    alpaca_news_requests_per_minute: int = 200
    alpaca_news_timeout_s: float = 30.0

    # Finnhub is the cross-check for the earliest-timestamp rule, not a
    # primary. 60 requests/minute on the free tier.
    finnhub_news_requests_per_minute: int = 60

    # MinHash/LSH near-duplicate clustering (Spec O section 5.2). 128
    # permutations at a Jaccard threshold of 0.6 over 5-word shingles of
    # title-plus-lead. Changing any of these changes which stories merge, so
    # they are settings rather than literals — but they are *not* per-run
    # knobs: a cluster id is only comparable across runs at fixed parameters.
    news_minhash_permutations: int = 128
    news_cluster_jaccard_threshold: float = 0.5
    news_shingle_size: int = 5

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8", "extra": "ignore"}

    @field_validator("sec_max_requests_per_second")
    @classmethod
    def validate_sec_rate(cls, value: float) -> float:
        if not 0 < value <= 10.0:
            raise ValueError(
                "SEC_MAX_REQUESTS_PER_SECOND must be >0 and <=10.0 "
                "(https://www.sec.gov/os/webmaster-faq: maximum access rate is "
                "10 requests per second)"
            )
        return value

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
