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
    memo_threshold: float = 0.20  # Lowered from 0.55 for testing — restore before live
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

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}
