from dotenv import load_dotenv
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
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "SwingTrader/1.0"

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

    # --- Scoring ---
    memo_threshold: float = 0.55  # Production threshold (override via .env for testing)
    high_conviction_threshold: float = 0.75
    catalyst_escalation_threshold: int = 3  # Haiku score 1-5 to trigger Sonnet

    # --- Scheduling (ET hours) ---
    pre_market_hour: int = 7
    midday_hour: int = 12
    post_market_hour: int = 17

    # --- Database ---
    database_url: str = "sqlite:///swing_trader.db"

    # --- Model Selection ---
    # Override scoring tier model (default: opus)
    scoring_model: str = "claude-opus-4-6"
    analyst_model: str = "claude-sonnet-4-6"
    filter_model: str = "claude-haiku-4-5-20251001"

    # --- V2: Web Search & Discovery ---
    web_search_provider: str = "anthropic"  # "anthropic" (default)
    discovery_max_tickers: int = 12
    discovery_model: str = "claude-sonnet-4-6"  # Discovery uses Sonnet, NOT Haiku

    # --- V2: Extended Thinking ---
    discovery_thinking_budget: int = 0      # Thinking tokens for discovery scan (was 10000; search quality drives discovery, not thinking)
    opus_thinking_budget: int = 16000       # Thinking tokens for Opus evaluation

    # --- Gemini Flash Screening (Tier 2) ---
    gemini_api_key: str = ""
    gemini_flash_model: str = "gemini-2.0-flash"
    gemini_flash_escalation_threshold: float = 0.50  # Tickers scoring above this escalate to Sonnet

    # --- V2: Deep Research (Phase C) ---
    openai_api_key: str = ""
    deep_research_provider: str = "gemini"  # "gemini" or "openai"
    deep_research_score_threshold: float = 0.75

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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
