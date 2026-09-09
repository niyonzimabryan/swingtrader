import json
from datetime import date
from utils.timeutils import utcnow_naive
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, Date,
    CheckConstraint, ForeignKey, Enum, UniqueConstraint, Index, create_engine
)
from sqlalchemy.orm import declarative_base, relationship

from database.types import UtcDateTime

Base = declarative_base()


class Ticker(Base):
    __tablename__ = "tickers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(200), default="")
    sector = Column(String(100), default="")
    market_cap = Column(Float, default=0)
    in_universe = Column(Boolean, default=True)
    added_at = Column(UtcDateTime, default=utcnow_naive)
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)

    # Relationships
    price_data = relationship("PriceData", back_populates="ticker", cascade="all, delete-orphan")
    catalysts = relationship("Catalyst", back_populates="ticker", cascade="all, delete-orphan")
    fundamentals = relationship("FundamentalData", back_populates="ticker", cascade="all, delete-orphan")
    signals = relationship("Signal", back_populates="ticker", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="ticker", cascade="all, delete-orphan")
    memos = relationship("Memo", back_populates="ticker", cascade="all, delete-orphan")


class PriceData(Base):
    __tablename__ = "price_data"
    __table_args__ = (
        UniqueConstraint("ticker_id", "date", name="uq_price_ticker_date"),
        Index("ix_price_ticker_date", "ticker_id", "date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    date = Column(Date, nullable=False)
    open = Column(Float)
    high = Column(Float)
    low = Column(Float)
    close = Column(Float)
    volume = Column(Float)
    adj_close = Column(Float)

    ticker = relationship("Ticker", back_populates="price_data")


class Catalyst(Base):
    __tablename__ = "catalysts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    catalyst_type = Column(String(50), nullable=False)  # earnings_surprise, insider_buying, analyst_revision, etc.
    summary = Column(Text, default="")
    magnitude = Column(Integer, default=0)  # 1-5
    direction = Column(String(20), default="neutral")  # bullish, bearish, ambiguous
    expected_impact_low = Column(Float, default=0)
    expected_impact_mid = Column(Float, default=0)
    expected_impact_high = Column(Float, default=0)
    time_horizon_days = Column(Integer, default=10)
    confidence = Column(Float, default=0)
    raw_source = Column(Text, default="")
    reasoning = Column(Text, default="")
    haiku_score = Column(Integer, default=0)  # 1-5 pre-screen score
    escalated = Column(Boolean, default=False)
    detected_at = Column(UtcDateTime, default=utcnow_naive)
    run_id = Column(String(50), default="")

    ticker = relationship("Ticker", back_populates="catalysts")


class FundamentalData(Base):
    __tablename__ = "fundamentals"
    __table_args__ = (
        UniqueConstraint("ticker_id", "as_of_date", name="uq_fundamental_ticker_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    as_of_date = Column(Date, nullable=False)
    quality_score = Column(Float, default=0)
    balance_sheet_score = Column(Float, default=0)
    valuation_score = Column(Float, default=0)
    growth_score = Column(Float, default=0)
    composite_score = Column(Float, default=0)
    raw_data = Column(Text, default="{}")  # JSON
    peer_comparison = Column(Text, default="")
    flags = Column(Text, default="[]")  # JSON array
    reasoning = Column(Text, default="")
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)

    ticker = relationship("Ticker", back_populates="fundamentals")

    @property
    def flags_list(self) -> list:
        try:
            return json.loads(self.flags)
        except (json.JSONDecodeError, TypeError):
            return []

    @property
    def raw_data_dict(self) -> dict:
        try:
            return json.loads(self.raw_data)
        except (json.JSONDecodeError, TypeError):
            return {}


class Signal(Base):
    __tablename__ = "signals"
    __table_args__ = (
        Index("ix_signal_ticker_agent_run", "ticker_id", "agent_type", "run_id"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    agent_type = Column(String(30), nullable=False)  # macro, catalyst, fundamental, pattern, sentiment
    run_id = Column(String(50), default="")
    score = Column(Float, default=0)
    confidence = Column(Float, default=0)
    direction = Column(String(20), default="neutral")
    reasoning = Column(Text, default="")
    raw_output = Column(Text, default="{}")  # JSON
    created_at = Column(UtcDateTime, default=utcnow_naive)

    ticker = relationship("Ticker", back_populates="signals")


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        Index("ix_trades_status_created_at", "status", "created_at"),
        Index("ix_trades_broker_status", "broker", "status"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    memo_id = Column(Integer, ForeignKey("memos.id"), nullable=True)
    direction = Column(String(10), default="long")  # long, short
    entry_price = Column(Float, default=0)
    exit_price = Column(Float, nullable=True)
    entry_date = Column(UtcDateTime, nullable=True)
    exit_date = Column(UtcDateTime, nullable=True)
    shares = Column(Integer, default=0)
    stop_loss = Column(Float, default=0)
    target_1 = Column(Float, default=0)
    target_2 = Column(Float, default=0)
    position_pct = Column(Float, default=0)
    status = Column(String(20), default="pending")  # pending, open, closed, cancelled
    exit_reason = Column(String(30), nullable=True)  # stop_loss, target_1, target_2, time_exit, manual
    pnl_pct = Column(Float, nullable=True)
    pnl_absolute = Column(Float, nullable=True)
    setup_type = Column(String(50), default="")
    signal_scores = Column(Text, default="{}")  # JSON
    regime_at_entry = Column(String(20), default="")
    alpaca_entry_order_id = Column(String(100), nullable=True)
    alpaca_stop_order_id = Column(String(100), nullable=True)
    broker = Column(String(30), default="alpaca")
    broker_account_id = Column(String(100), nullable=True)
    broker_order_id = Column(String(100), nullable=True)
    broker_stop_order_id = Column(String(100), nullable=True)
    broker_order_strategy = Column(String(50), nullable=True)
    order_review_json = Column(Text, default="{}")
    execution_mode = Column(String(20), default="paper")
    requested_notional = Column(Float, nullable=True)
    filled_notional = Column(Float, nullable=True)
    operator_notes = Column(Text, default="")
    # Position monitoring fields
    peak_price = Column(Float, nullable=True)
    t1_hit = Column(Boolean, default=False)
    t2_hit = Column(Boolean, default=False)
    t1_approaching_sent = Column(Boolean, default=False)
    time_warning_sent = Column(Boolean, default=False)
    drawdown_alert_sent = Column(Boolean, default=False)
    created_at = Column(UtcDateTime, default=utcnow_naive)
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)

    ticker = relationship("Ticker", back_populates="trades")
    memo = relationship("Memo", back_populates="trade")


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(80), unique=True, nullable=False, index=True)
    trigger_source = Column(String(40), default="")
    started_at = Column(UtcDateTime, default=utcnow_naive)
    ended_at = Column(UtcDateTime, nullable=True)
    status = Column(String(30), default="running")
    scanned_count = Column(Integer, default=0)
    screened_count = Column(Integer, default=0)
    researched_count = Column(Integer, default=0)
    memos_generated = Column(Integer, default=0)
    approved_count = Column(Integer, default=0)
    duration_s = Column(Float, nullable=True)
    cost_estimate = Column(Float, nullable=True)
    degraded_stages = Column(Text, default="[]")
    errors_json = Column(Text, default="[]")
    metadata_json = Column(Text, default="{}")
    created_at = Column(UtcDateTime, default=utcnow_naive)
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)


class OrderEvent(Base):
    __tablename__ = "order_events"
    __table_args__ = (
        Index("ix_order_events_broker_order", "broker", "order_id"),
        Index("ix_order_events_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    trade_id = Column(Integer, ForeignKey("trades.id"), nullable=True)
    memo_id = Column(Integer, ForeignKey("memos.id"), nullable=True)
    broker = Column(String(30), default="")
    account_id = Column(String(100), nullable=True)
    order_id = Column(String(100), nullable=True)
    event_type = Column(String(40), default="")
    status = Column(String(40), default="")
    notional = Column(Float, nullable=True)
    raw_payload = Column(Text, default="{}")
    created_at = Column(UtcDateTime, default=utcnow_naive)


class Memo(Base):
    __tablename__ = "memos"
    __table_args__ = (
        # Digest/weekly/automation queries filter and order memos by created_at
        # only (status is filtered in Python, never in SQL), so created_at leads.
        Index("ix_memos_created_at", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    composite_score = Column(Float, default=0)
    classification = Column(String(20), default="")  # high_conviction, moderate, low, no_action
    direction = Column(String(10), default="long")
    full_text = Column(Text, default="")
    trade_params = Column(Text, default="{}")  # JSON: entry, stop, targets, size
    signal_breakdown = Column(Text, default="{}")  # JSON: per-agent scores
    opus_critique = Column(Text, default="")
    memo_data_json = Column(Text, default="{}")  # Full memo_data dict for re-rendering (v2.1)
    thesis = Column(Text, default="")
    bear_case = Column(Text, default="")
    status = Column(String(20), default="pending")  # pending, approved, rejected, watchlisted, expired
    operator_notes = Column(Text, default="")
    telegram_message_id = Column(Integer, nullable=True)
    created_at = Column(UtcDateTime, default=utcnow_naive)
    responded_at = Column(UtcDateTime, nullable=True)

    ticker = relationship("Ticker", back_populates="memos")
    trade = relationship("Trade", back_populates="memo", uselist=False)

    @property
    def trade_params_dict(self) -> dict:
        try:
            return json.loads(self.trade_params)
        except (json.JSONDecodeError, TypeError):
            return {}

    @property
    def signal_breakdown_dict(self) -> dict:
        try:
            return json.loads(self.signal_breakdown)
        except (json.JSONDecodeError, TypeError):
            return {}

    @property
    def memo_data_dict(self) -> dict:
        try:
            return json.loads(self.memo_data_json)
        except (json.JSONDecodeError, TypeError):
            return {}


class MacroRegime(Base):
    __tablename__ = "macro_regime"

    id = Column(Integer, primary_key=True, autoincrement=True)
    date = Column(Date, unique=True, nullable=False, index=True)
    regime = Column(String(20), nullable=False)  # risk-on, neutral, risk-off
    confidence = Column(Float, default=0)
    position_size_multiplier = Column(Float, default=1.0)
    max_positions = Column(Integer, default=6)
    reasoning = Column(Text, default="")
    raw_inputs = Column(Text, default="{}")  # JSON
    created_at = Column(UtcDateTime, default=utcnow_naive)


class HistoricalPattern(Base):
    __tablename__ = "historical_patterns"
    __table_args__ = (
        Index("idx_patterns_lookup", "setup_type", "source_ticker"),
        UniqueConstraint("setup_type", "source_ticker", "event_date", name="uq_pattern_event"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=True)
    setup_type = Column(String(60), nullable=False)
    event_date = Column(String(20), nullable=False)
    source_ticker = Column(String(10), nullable=False)
    is_peer = Column(Boolean, default=False)
    beat_magnitude = Column(Float, nullable=True)
    return_t5 = Column(Float, nullable=True)
    return_t10 = Column(Float, nullable=True)
    return_t15 = Column(Float, nullable=True)
    return_t20 = Column(Float, nullable=True)
    max_drawdown = Column(Float, nullable=True)
    max_drawdown_day = Column(Integer, nullable=True)
    raw_data = Column(Text, default="{}")
    created_at = Column(UtcDateTime, default=utcnow_naive)


# --- V2 Tables ---

class DiscoveredTicker(Base):
    """Tickers found by the Discovery Agent via web search."""
    __tablename__ = "discovered_tickers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False, index=True)
    catalyst_summary = Column(Text, default="")
    catalyst_type = Column(String(50), default="")
    relevance_score = Column(Float, default=0)
    direction_hint = Column(String(20), default="neutral")  # bullish, bearish, neutral
    discovery_context = Column(Text, default="")  # Full context from Discovery Agent
    model_used = Column(String(50), default="")
    run_id = Column(String(50), default="")
    progressed_to_pipeline = Column(Boolean, default=False)
    pipeline_score = Column(Float, nullable=True)
    discovered_at = Column(UtcDateTime, default=utcnow_naive)


class WebResearch(Base):
    """Web research results from the Web Research Agent."""
    __tablename__ = "web_research"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    synthesis = Column(Text, default="")
    catalyst_context = Column(Text, default="")
    competitive_dynamics = Column(Text, default="")
    management_signals = Column(Text, default="")
    bull_bear_debate = Column(Text, default="")
    institutional_positioning = Column(Text, default="")
    key_finding = Column(Text, default="")
    information_score = Column(Float, default=0)
    confidence = Column(Float, default=0)
    direction = Column(String(20), default="neutral")
    sources_summary = Column(Text, default="")
    model_used = Column(String(50), default="")
    run_id = Column(String(50), default="")
    created_at = Column(UtcDateTime, default=utcnow_naive)

    ticker = relationship("Ticker")


class WebResearchCache(Base):
    """Same-day web-research cache for repeated ticker/catalyst runs."""
    __tablename__ = "web_research_cache"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_web_research_cache_key"),
        Index("ix_web_research_cache_lookup", "ticker", "research_date", "catalyst_hash"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(120), nullable=False)
    ticker = Column(String(10), nullable=False)
    research_date = Column(String(10), nullable=False)
    catalyst_hash = Column(String(64), nullable=False)
    provider = Column(String(30), default="")
    model_used = Column(String(80), default="")
    result_json = Column(Text, default="{}")
    created_at = Column(UtcDateTime, default=utcnow_naive)
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)
    expires_at = Column(UtcDateTime, nullable=True)


class WatchlistTicker(Base):
    """Operator or Opus-recommended tickers for lower-threshold re-scanning."""
    __tablename__ = "watchlist_tickers"
    __table_args__ = (
        Index("ix_watchlist_active", "active"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False, index=True)
    sector = Column(String(100), default="")
    reason = Column(Text, default="")
    source = Column(String(30), default="")  # "opus_recommendation", "operator", "discovery"
    active = Column(Boolean, default=True)
    added_at = Column(UtcDateTime, default=utcnow_naive)
    deactivated_at = Column(UtcDateTime, nullable=True)


class HistoricalContext(Base):
    """Contextual data for historical pattern instances — enables similarity scoring."""
    __tablename__ = "historical_contexts"
    __table_args__ = (
        UniqueConstraint("pattern_id", name="uq_context_pattern"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    pattern_id = Column(Integer, ForeignKey("historical_patterns.id"), nullable=False, index=True)
    macro_regime = Column(String(20), default="")  # risk-on, neutral, risk-off
    vix_level = Column(Float, nullable=True)
    fwd_pe_ratio = Column(Float, nullable=True)
    momentum_20d = Column(Float, nullable=True)  # 20-day price return (%)
    sp500_distance_200ma = Column(Float, nullable=True)  # S&P 500 distance from 200-day MA (%)
    created_at = Column(UtcDateTime, default=utcnow_naive)

    pattern = relationship("HistoricalPattern")


class CompanyProfile(Base):
    """Cached structured company profile used for peers, context, and query building."""
    __tablename__ = "company_profiles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(200), default="")
    exchange = Column(String(40), default="")
    sector = Column(String(100), default="")
    industry = Column(String(160), default="")
    market_cap = Column(Float, nullable=True)
    beta = Column(Float, nullable=True)
    description = Column(Text, default="")
    country = Column(String(60), default="")
    currency = Column(String(20), default="")
    raw_json = Column(Text, default="{}")
    profile_source = Column(String(40), default="")
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)
    expires_at = Column(UtcDateTime, nullable=True)


class PeerEdge(Base):
    """Cached ranked peer edge for a target ticker."""
    __tablename__ = "peer_edges"
    __table_args__ = (
        UniqueConstraint("target_ticker", "peer_ticker", "source", "as_of_date", name="uq_peer_edge_source_date"),
        Index("ix_peer_edges_target", "target_ticker"),
        Index("ix_peer_edges_peer", "peer_ticker"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    target_ticker = Column(String(10), nullable=False, index=True)
    peer_ticker = Column(String(10), nullable=False, index=True)
    rank = Column(Integer, default=0)
    score = Column(Float, default=0)
    source = Column(String(80), default="")
    reasons_json = Column(Text, default="[]")
    as_of_date = Column(Date, default=date.today)
    expires_at = Column(UtcDateTime, nullable=True)


class PatternSearchRun(Base):
    """Auditable event analog search run and failure/status envelope."""
    __tablename__ = "pattern_search_runs"
    __table_args__ = (
        Index("ix_pattern_search_runs_ticker_created", "ticker", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(80), nullable=False, index=True)
    ticker = Column(String(10), nullable=False, index=True)
    setup_type = Column(String(80), default="")
    catalyst_hash = Column(String(64), default="")
    status = Column(String(40), default="")
    provider_plan_json = Column(Text, default="{}")
    queries_json = Column(Text, default="[]")
    peer_set_json = Column(Text, default="[]")
    result_counts_json = Column(Text, default="{}")
    cost_estimate = Column(Float, nullable=True)
    duration_s = Column(Float, nullable=True)
    error = Column(Text, default="")
    created_at = Column(UtcDateTime, default=utcnow_naive)


class PatternProviderCache(Base):
    """Cached raw search-provider result keyed by provider/query/filter hash."""
    __tablename__ = "pattern_provider_cache"
    __table_args__ = (
        UniqueConstraint("cache_key", name="uq_pattern_provider_cache_key"),
        Index("ix_pattern_provider_cache_provider", "provider", "expires_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    cache_key = Column(String(120), nullable=False)
    provider = Column(String(30), default="")
    query = Column(Text, default="")
    filters_json = Column(Text, default="{}")
    result_json = Column(Text, default="{}")
    created_at = Column(UtcDateTime, default=utcnow_naive)
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)
    expires_at = Column(UtcDateTime, nullable=True)


class HistoricalEvent(Base):
    """Canonical normalized historical catalyst/event row for analog retrieval."""
    __tablename__ = "historical_events"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_historical_event_dedupe_key"),
        Index("ix_historical_events_ticker_type_date", "ticker", "event_type", "event_date"),
        Index("ix_historical_events_type_date", "event_type", "event_date"),
        Index("ix_historical_events_dedupe", "dedupe_key"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(10), nullable=False, index=True)
    company_name = Column(String(200), default="")
    event_type = Column(String(80), nullable=False, index=True)
    event_subtype = Column(String(120), default="")
    event_date = Column(Date, nullable=False)
    event_timestamp = Column(UtcDateTime, nullable=True)
    event_timing = Column(String(20), default="unknown")
    polarity = Column(String(20), default="neutral")
    magnitude = Column(Float, nullable=True)
    headline = Column(Text, default="")
    summary = Column(Text, default="")
    evidence = Column(Text, default="")
    source_url = Column(Text, default="")
    source_domain = Column(String(160), default="")
    source_type = Column(String(40), default="other")
    provider = Column(String(30), default="")
    provider_query = Column(Text, default="")
    confidence = Column(Float, default=0)
    dedupe_key = Column(String(64), nullable=False, unique=True, index=True)
    embedding_json = Column(Text, nullable=True)
    raw_json = Column(Text, default="{}")
    created_at = Column(UtcDateTime, default=utcnow_naive)
    updated_at = Column(UtcDateTime, default=utcnow_naive, onupdate=utcnow_naive)

    outcome = relationship("EventOutcome", back_populates="event", uselist=False, cascade="all, delete-orphan")
    context = relationship("EventContext", back_populates="event", uselist=False, cascade="all, delete-orphan")


class EventOutcome(Base):
    """Deterministic forward price outcomes after a canonical event."""
    __tablename__ = "event_outcomes"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_outcome_event"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("historical_events.id"), nullable=False, index=True)
    ticker = Column(String(10), nullable=False, index=True)
    anchor_price = Column(Float, nullable=True)
    anchor_trade_date = Column(Date, nullable=True)
    return_t1 = Column(Float, nullable=True)
    return_t3 = Column(Float, nullable=True)
    return_t5 = Column(Float, nullable=True)
    return_t10 = Column(Float, nullable=True)
    return_t20 = Column(Float, nullable=True)
    return_t60 = Column(Float, nullable=True)
    abnormal_return_t5 = Column(Float, nullable=True)
    abnormal_return_t10 = Column(Float, nullable=True)
    abnormal_return_t20 = Column(Float, nullable=True)
    benchmark_symbol = Column(String(20), default="SPY")
    sector_benchmark_symbol = Column(String(20), default="")
    max_drawdown_t20 = Column(Float, nullable=True)
    max_drawdown_day = Column(Integer, nullable=True)
    volume_ratio_t1 = Column(Float, nullable=True)
    gap_pct = Column(Float, nullable=True)
    matured_horizons_json = Column(Text, default="[]")
    status = Column(String(40), default="")
    computed_at = Column(UtcDateTime, default=utcnow_naive)

    event = relationship("HistoricalEvent", back_populates="outcome")


class EventContext(Base):
    """Point-in-time similarity inputs as of the event date."""
    __tablename__ = "event_contexts"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_event_context_event"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    event_id = Column(Integer, ForeignKey("historical_events.id"), nullable=False, unique=True, index=True)
    macro_regime = Column(String(20), default="")
    vix_level = Column(Float, nullable=True)
    sp500_distance_200ma = Column(Float, nullable=True)
    sector_momentum_20d = Column(Float, nullable=True)
    ticker_momentum_20d = Column(Float, nullable=True)
    ticker_volatility_20d = Column(Float, nullable=True)
    market_cap = Column(Float, nullable=True)
    trailing_pe_ratio = Column(Float, nullable=True)
    ev_sales = Column(Float, nullable=True)
    valuation_source_filing_date = Column(Date, nullable=True)
    pit_quality = Column(String(20), default="unavailable")
    raw_json = Column(Text, default="{}")
    computed_at = Column(UtcDateTime, default=utcnow_naive)

    event = relationship("HistoricalEvent", back_populates="context")


class ScoredCandidate(Base):
    """Shadow calibration ledger (Spec I1).

    One row for EVERY ticker that reaches scoring (scheduled + ad-hoc),
    regardless of outcome. Forward returns are filled nightly once each horizon
    matures. This is the labeled dataset (~30/day) that drives all future
    threshold decisions from decile data instead of guesses.
    """
    __tablename__ = "scored_candidates"
    __table_args__ = (
        Index("ix_scored_candidates_run_id", "run_id"),
        Index("ix_scored_candidates_returns_pending", "returns_computed_at", "scored_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(String(80), default="")
    ticker = Column(String(10), nullable=False, index=True)
    scored_at = Column(UtcDateTime, default=utcnow_naive)
    source = Column(String(30), default="")  # tier2_gemini / discovery / watchlist / ...
    final_score = Column(Float, default=0)
    catalyst_score = Column(Float, nullable=True)
    fundamental_score = Column(Float, nullable=True)
    pattern_score = Column(Float, nullable=True)
    pattern_status = Column(String(40), default="")
    web_research_score = Column(Float, nullable=True)
    direction = Column(String(20), default="neutral")
    regime = Column(String(20), default="")
    entry_price = Column(Float, nullable=True)  # price at scoring time
    suggested_stop = Column(Float, nullable=True)
    target_1 = Column(Float, nullable=True)
    memo_generated = Column(Boolean, default=False)
    paper_traded = Column(Boolean, default=False)  # I2
    cohort = Column(String(20), default="below")   # memo | exploration | below
    # Forward returns (percent), filled nightly once each horizon matures.
    ret_t1 = Column(Float, nullable=True)
    ret_t3 = Column(Float, nullable=True)
    ret_t5 = Column(Float, nullable=True)
    ret_t10 = Column(Float, nullable=True)
    ret_t20 = Column(Float, nullable=True)
    returns_computed_at = Column(UtcDateTime, nullable=True)
    created_at = Column(UtcDateTime, default=utcnow_naive)


class DeepResearchRequest(Base):
    """Tracks async deep research tasks for high-conviction ideas."""
    __tablename__ = "deep_research_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    memo_id = Column(Integer, ForeignKey("memos.id"), nullable=False)
    ticker = Column(String(10), nullable=False, index=True)
    task_id = Column(String(200), default="")  # Provider's task ID
    provider = Column(String(30), default="gemini")
    status = Column(String(30), default="submitted")  # submitted, in_progress, completed, failed, timeout
    original_score = Column(Float, default=0)
    research_report = Column(Text, default="")
    reevaluation_result = Column(Text, default="{}")  # JSON: Opus re-evaluation
    updated_score = Column(Float, nullable=True)
    updated_recommendation = Column(String(30), nullable=True)
    duration_s = Column(Float, nullable=True)
    pdf_path = Column(String(500), nullable=True)
    submitted_at = Column(UtcDateTime, default=utcnow_naive)
    completed_at = Column(UtcDateTime, nullable=True)
    error = Column(Text, default="")


# --------------------------------------------------------------------------- #
# Price plane (Spec N §4.2/§4.3, Phase 3p)
#
# Five tables, no foreign keys to anything outside this block. Phases 0b and 3a
# are adding their own tables from the same baseline; a cross-phase FK would
# make the integration merge revision order-dependent for no gain
# (migrations/README.md). Securities are joined on `security_uid`, a stable
# string id that survives ticker changes, which is also why it is not an FK
# target: `securities` carries one row per ticker validity interval, so the
# column is deliberately non-unique there.
# --------------------------------------------------------------------------- #

#: The four delisting reason categories. `performance` is the one that carries a
#: Shumway terminal return (Spec N §4.2); `unknown` is what an unaudited vendor
#: file gets, and it censors rather than matures the outcome (§4.4).
DELISTING_REASONS = ("performance", "merger_acquisition", "other", "unknown")

#: Venue as the Shumway convention needs it: the -30% / -55% split is
#: NYSE/AMEX versus Nasdaq, not exchange-by-exchange.
VENUES = ("nyse_amex", "nasdaq", "other", "unknown")


class Security(Base):
    """Security master: one row per (security, ticker validity interval).

    A ticker rename adds a row with the same ``security_uid`` and a closed
    ``ticker_valid_to``; the bars and actions keep pointing at the uid, so a
    cohort built across a rename does not silently split into two names.
    """

    __tablename__ = "securities"
    __table_args__ = (
        UniqueConstraint(
            "security_uid", "ticker", "ticker_valid_from", name="uq_securities_uid_ticker_from"
        ),
        Index("ix_securities_uid", "security_uid"),
        Index("ix_securities_ticker_valid", "ticker", "ticker_valid_from", "ticker_valid_to"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    security_uid = Column(String(64), nullable=False)
    ticker = Column(String(20), nullable=False)
    ticker_valid_from = Column(Date, nullable=True)
    ticker_valid_to = Column(Date, nullable=True)      # NULL = still the current ticker
    name = Column(String(200), nullable=True)
    exchange = Column(String(40), nullable=True)       # as the vendor spells it
    venue = Column(String(20), nullable=False, default="unknown")
    listing_date = Column(Date, nullable=True)
    delisting_date = Column(Date, nullable=True)
    delisting_reason = Column(String(32), nullable=False, default="unknown")
    source = Column(String(40), nullable=False)
    ingested_at = Column(UtcDateTime, default=utcnow_naive)


class PriceBar(Base):
    """One daily session, with the three series and the factors between them.

    Spec N §4.3 stores raw OHLCV (what fills happened at), the split-adjusted
    close (what signals and replay run on) and the total-return close (what the
    benchmark comparison uses), plus the split factor and dividend cash whose
    ex-date is this session — so any one series reconstructs from the other two.
    """

    __tablename__ = "price_bars"
    __table_args__ = (
        UniqueConstraint("security_uid", "session_date", name="uq_price_bars_uid_date"),
        Index("ix_price_bars_uid_date", "security_uid", "session_date"),
        Index("ix_price_bars_date", "session_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    security_uid = Column(String(64), nullable=False)
    ticker = Column(String(20), nullable=False)
    session_date = Column(Date, nullable=False)

    raw_open = Column(Float, nullable=False)
    raw_high = Column(Float, nullable=False)
    raw_low = Column(Float, nullable=False)
    raw_close = Column(Float, nullable=False)
    volume = Column(Float, nullable=False)

    #: Share multiplier whose ex-date is this session; 1.0 on an ordinary day.
    split_factor = Column(Float, nullable=False, default=1.0)
    #: Cash per share whose ex-date is this session; 0.0 on an ordinary day.
    dividend_cash = Column(Float, nullable=False, default=0.0)

    split_adjusted_close = Column(Float, nullable=False)
    total_return_close = Column(Float, nullable=False)

    source = Column(String(40), nullable=False)
    ingested_at = Column(UtcDateTime, default=utcnow_naive)


class CorporateActionRow(Base):
    """A split, dividend or delisting event stored with its ex-date."""

    __tablename__ = "corporate_actions"
    __table_args__ = (
        UniqueConstraint(
            "security_uid", "ex_date", "action_type", "source",
            name="uq_corporate_actions_uid_date_type_source",
        ),
        Index("ix_corporate_actions_uid_date", "security_uid", "ex_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    security_uid = Column(String(64), nullable=False)
    ticker = Column(String(20), nullable=False)
    ex_date = Column(Date, nullable=False)
    action_type = Column(String(32), nullable=False)   # split | dividend | delisting | ...
    value = Column(Float, nullable=True)
    source = Column(String(40), nullable=False)
    ingested_at = Column(UtcDateTime, default=utcnow_naive)


class UniverseMembership(Base):
    """Point-in-time index or rule membership (Spec N §4.2).

    Half-open intervals: a name is a member as of ``d`` when
    ``member_from <= d < member_to``, with a NULL ``member_to`` meaning "still a
    member". ``known_at_utc`` is when the membership fact could first have been
    acted on, so a cohort's bitemporal filter (§4.1) applies to membership too.
    """

    __tablename__ = "universe_membership"
    __table_args__ = (
        UniqueConstraint(
            "universe_slug", "security_uid", "member_from",
            name="uq_universe_membership_slug_uid_from",
        ),
        Index("ix_universe_membership_slug_window", "universe_slug", "member_from", "member_to"),
        Index("ix_universe_membership_slug_uid", "universe_slug", "security_uid"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    universe_slug = Column(String(64), nullable=False)
    security_uid = Column(String(64), nullable=False)
    ticker = Column(String(20), nullable=False)
    member_from = Column(Date, nullable=False)
    member_to = Column(Date, nullable=True)
    source = Column(String(64), nullable=False)
    known_at_utc = Column(UtcDateTime, nullable=False)


class PriceSnapshot(Base):
    """A named vintage of the price file, so a cohort can say what it ran on.

    ``delisting_audit_json`` carries the Spec N §4.2 twenty-delisting audit for
    this snapshot: a snapshot whose audit says the series merely *stop* is one
    whose terminal returns have to be synthesised, and the cohort renderer needs
    to be able to read that back.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_slug", name="uq_price_snapshots_slug"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_slug = Column(String(64), nullable=False)
    source = Column(String(40), nullable=False)
    coverage_summary_json = Column(Text, nullable=False, default="{}")
    delisting_audit_json = Column(Text, nullable=False, default="{}")
    created_at = Column(UtcDateTime, default=utcnow_naive)

    @property
    def coverage_summary(self) -> dict:
        return json.loads(self.coverage_summary_json or "{}")

    @property
    def delisting_audit(self) -> dict:
        return json.loads(self.delisting_audit_json or "{}")


class WorkspaceToken(Base):
    """An owner token for the workspace API and MCP endpoint (Spec K §4.1).

    One row per client ("codex-laptop", "claude-web"), issued by
    ``scripts/workspace_token.py --issue``. Only the SHA-256 digest of the
    secret is stored: the plaintext is printed once and never persisted, so a
    database dump does not hand anyone an access token.

    SHA-256 rather than bcrypt/argon2 on purpose. A password hash is slow to
    defend a *low-entropy* secret against offline guessing; these secrets are
    256 bits from ``secrets.token_urlsafe``, where guessing is not a threat, and
    a slow hash on every request would be a rate-limiter working against us.

    ``scopes`` is a comma-separated list drawn from
    ``workspace.scopes.SCOPES``. There is deliberately no ``execute`` scope —
    order placement is not reachable by token at all (Spec L §6), and
    ``tests/test_no_execute_scope.py`` keeps it that way.
    """

    __tablename__ = "workspace_tokens"

    id = Column(Integer, primary_key=True, autoincrement=True)
    label = Column(String(100), unique=True, nullable=False, index=True)
    # Hex SHA-256 of the secret. Indexed because it is the lookup key on every
    # authenticated request.
    token_hash = Column(String(64), unique=True, nullable=False, index=True)
    # Leading characters of the secret, for logs and `--list`. Not secret and
    # not sufficient to authenticate.
    token_prefix = Column(String(12), nullable=False, default="")
    scopes = Column(String(200), nullable=False, default="read")
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    last_used_at = Column(UtcDateTime, nullable=True)
    revoked_at = Column(UtcDateTime, nullable=True)
    note = Column(Text, nullable=False, default="")
class SourceObservation(Base):
    """Bitemporal source ledger — Spec Q section 8, extended by Spec O section 2.

    One row is one fact from one provider, carrying both of its times:

    ``valid_at``
        when the fact applies (period end for a duration fact, the instant for
        a point fact, the announcement time for an event).
    ``known_at_utc``
        when we could first have known it. For SEC filings this is the
        submissions ``acceptanceDateTime``, never ``filingDate`` — see
        ``filings/observations.py``, which refuses the latter.

    ``precision`` records whether ``known_at_utc`` is real to the second or is
    a date normalised to the end of its day; ``provenance_class`` records how
    the timestamp was obtained. Both travel with every row so a downstream
    cohort can tell an observed instant from a reconstructed one instead of
    inferring it from the source name.

    Restatements are new rows: ``known_at_utc <= t`` therefore returns the
    as-reported figure without any deletion. ``superseded_observation_id``
    points at the row an amendment replaces; the original stays queryable.
    """

    __tablename__ = "source_observations"
    __table_args__ = (
        # Idempotency: the natural key of an observation is the hash of its
        # identity plus its value, so re-running an ingest writes nothing new
        # while a restatement (new accession, new value) is a new row.
        UniqueConstraint("payload_hash", name="uq_source_obs_payload_hash"),
        # The `known_at_utc <= t` filter every cohort runs, narrowed first by
        # the entity and fact type it asks about.
        Index("ix_source_obs_entity_fact_known", "entity_cik", "fact_type", "known_at_utc"),
        Index("ix_source_obs_ticker_fact_known", "ticker_at_time", "fact_type", "known_at_utc"),
        Index("ix_source_obs_known_at", "known_at_utc"),
        Index("ix_source_obs_source_accession", "source", "accession"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)

    # --- where it came from ---
    source = Column(String(60), nullable=False)
    source_url = Column(Text, nullable=False, default="")
    source_trust = Column(String(30), nullable=False)
    accession = Column(String(40), nullable=True)

    # --- what it is about ---
    entity_cik = Column(String(10), nullable=False)
    ticker_at_time = Column(String(20), nullable=True)
    fact_type = Column(String(80), nullable=False)

    # --- the value ---
    value_numeric = Column(Float, nullable=True)
    value_text = Column(Text, nullable=True)
    unit = Column(String(40), nullable=True)

    # --- the two times ---
    period_start = Column(Date, nullable=True)
    valid_at = Column(UtcDateTime, nullable=False)
    known_at_utc = Column(UtcDateTime, nullable=False)
    precision = Column(String(10), nullable=False)
    provenance_class = Column(String(30), nullable=False)
    replay_eligible = Column(Boolean, nullable=False, default=False)

    # --- bookkeeping ---
    payload_json = Column(Text, nullable=False, default="{}")
    payload_hash = Column(String(64), nullable=False)
    quality_warnings = Column(Text, nullable=False, default="[]")
    superseded_observation_id = Column(
        Integer, ForeignKey("source_observations.id"), nullable=True
    )
    ingested_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)

    @property
    def payload(self) -> dict:
        try:
            return json.loads(self.payload_json or "{}")
        except (ValueError, TypeError):
            return {}

    @property
    def warnings(self) -> list:
        try:
            loaded = json.loads(self.quality_warnings or "[]")
        except (ValueError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []


class EntityHistory(Base):
    """CIK ↔ ticker ↔ name, **with the date range each mapping applied to**.

    Spec O section 3.2. EDGAR's ``company_tickers.json`` and the ``tickers``
    array on a ``submissions`` document are *current* snapshots. Tickers get
    reused and companies rename, so a snapshot joined to a 2019 event resolves
    to whoever holds the symbol today — silently, and in whichever direction
    the reuse happened.

    This table is not foldable into ``source_observations``: an observation
    carries one ``valid_at`` instant, and what resolution needs is a half-open
    *interval* (``valid_from`` <= t < ``valid_to``) that can be closed later
    when a change is observed. A range is a different shape from a point, and
    faking it with two rows makes every read a self-join.

    ``basis`` records how the interval was obtained, because the two sources
    are not equally good:

    ``submissions_former_names``
        EDGAR states the ``from``/``to`` dates itself. A real dated range.
    ``snapshot_observed``
        We saw this value in a snapshot on this date and a different one in a
        later snapshot. The range is bounded by *our observations*, so its
        ``valid_from`` is an upper bound on when the mapping actually started.
        ``valid_from_is_first_observation`` says so, and a resolver asked about
        a date before it answers ``None`` rather than guessing.
    """

    __tablename__ = "entity_history"
    __table_args__ = (
        UniqueConstraint(
            "entity_cik", "attribute", "value", "valid_from",
            name="uq_entity_history_interval",
        ),
        Index("ix_entity_history_cik_attr", "entity_cik", "attribute", "valid_from"),
        Index("ix_entity_history_value", "attribute", "value", "valid_from"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    entity_cik = Column(String(10), nullable=False)
    #: ``ticker`` | ``name``
    attribute = Column(String(20), nullable=False)
    value = Column(String(200), nullable=False)
    #: Half-open: valid_from <= t < valid_to. ``valid_to`` NULL means "still".
    valid_from = Column(Date, nullable=False)
    valid_to = Column(Date, nullable=True)
    valid_from_is_first_observation = Column(Boolean, nullable=False, default=False)
    basis = Column(String(40), nullable=False)
    exchange = Column(String(40), nullable=True)
    source = Column(String(60), nullable=False)
    source_url = Column(Text, nullable=False, default="")
    known_at_utc = Column(UtcDateTime, nullable=False)
    first_observed_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    last_observed_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    ingested_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)


class NewsCluster(Base):
    """One *story*, however many outlets ran it — Spec O section 5.2.

    The cluster's ``known_at_utc`` is the minimum publisher timestamp across
    its members and applies to **the story event only**. A fact extracted from
    one member carries that member's own timestamp (section 5.1); conflating
    the two is how a number that first appeared in a 16:45 reaction piece
    becomes available at the 07:00 preview's time.
    """

    __tablename__ = "news_clusters"
    __table_args__ = (
        UniqueConstraint("cluster_id", name="uq_news_cluster_id"),
        Index("ix_news_cluster_known_at", "known_at_utc"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: Deterministic: the ``article_uid`` of the earliest-published member.
    cluster_id = Column(String(64), nullable=False)
    headline = Column(Text, nullable=False, default="")
    #: Minimum publisher timestamp across members. NULL when no member has one.
    known_at_utc = Column(UtcDateTime, nullable=True)
    member_count = Column(Integer, nullable=False, default=0)
    #: Fraction of this story's structured facts absent from the prior story
    #: for the same symbols. Computed from extracted facts, never from a model.
    novelty_score = Column(Float, nullable=True)
    novelty_basis = Column(Text, nullable=False, default="{}")
    symbols = Column(Text, nullable=False, default="[]")
    replay_eligible = Column(Boolean, nullable=False, default=False)
    updated_at = Column(UtcDateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)


class NewsArticle(Base):
    """One article, with its publisher timestamp and the tier it was found at.

    **This table is the licence boundary.** Alpaca's market-data terms bar
    sharing or publishing the data "or any derived products" (verification
    claim 11), so article text and everything computed from it stays in
    Postgres and never reaches the ``research/`` mirror. Keeping bodies in
    their own table makes that rule checkable at the table level instead of by
    filtering payloads out of a ledger that is otherwise mirror-eligible.

    An article whose publication time cannot be established is stored with
    ``replay_eligible=False`` — it is quarantined, not discarded, because
    "we saw this and could not date it" is a fact worth keeping.
    """

    __tablename__ = "news_articles"
    __table_args__ = (
        UniqueConstraint("article_uid", name="uq_news_article_uid"),
        Index("ix_news_article_cluster", "cluster_id"),
        Index("ix_news_article_published", "published_at_utc"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    #: sha256 over (source, provider id | canonical url, revision hash).
    article_uid = Column(String(64), nullable=False)
    cluster_id = Column(String(64), nullable=True)
    source = Column(String(40), nullable=False)
    provider_id = Column(String(80), nullable=True)
    publisher = Column(String(120), nullable=False, default="")
    #: ``primary`` | ``established`` | ``aggregator`` | ``unattributed``
    tier = Column(String(20), nullable=False)
    canonical_url = Column(Text, nullable=False, default="")
    url = Column(Text, nullable=False, default="")
    headline = Column(Text, nullable=False, default="")
    lead = Column(Text, nullable=False, default="")
    body = Column(Text, nullable=False, default="")
    symbols = Column(Text, nullable=False, default="[]")
    published_at_utc = Column(UtcDateTime, nullable=True)
    first_seen_at_utc = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    #: sha256 of the normalised body — a revision is a new row, not an update.
    content_hash = Column(String(64), nullable=False)
    revision_of_uid = Column(String(64), nullable=True)
    replay_eligible = Column(Boolean, nullable=False, default=False)
    provenance_class = Column(String(30), nullable=False)
    quality_warnings = Column(Text, nullable=False, default="[]")
    ingested_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)

    @property
    def symbol_list(self) -> list:
        try:
            loaded = json.loads(self.symbols or "[]")
        except (ValueError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []
# ---------------------------------------------------------------------------
# Portfolio ledger — Spec L §3 (Phase 1)
#
# Seven tables, no foreign key to any other phase's table, so the integration
# merge revision stays a no-op join. `broker_orders.execution_id` links to
# `strategy_trades.execution_id` (Spec Q, Phase 5) as a plain string for the
# same reason: the column can be populated before the table it names exists.
#
# Every ledger table that a sync writes carries `sync_id` and is **append-only**:
# a later sync inserts new rows and stamps `superseded_at` on the ones it
# replaces. Nothing is updated in place and nothing is deleted, so "what did I
# hold on date D" stays answerable (Spec L §3, `test_sync_is_append_only`).
# ---------------------------------------------------------------------------

#: `tax_lots.booking_method` / `brokerage_accounts.booking_method`. Vocabulary
#: borrowed from beancount: STRICT means a sale must name its lots; NONE means
#: the broker does not track lots at all.
BOOKING_METHODS = ("STRICT", "FIFO", "LIFO", "AVERAGE", "NONE")

#: `holdings.instrument_type`. Only `equity` is modelled for analysis; every
#: other value is rendered as `unsupported_instrument_present` with its
#: notional and is never omitted from an overview (Spec L §3).
INSTRUMENT_TYPES = ("equity", "option", "crypto", "other")

#: `brokerage_accounts.account_type`. The Robinhood Agentic account is `cash`
#: by owner decision (Spec L §5.1), which is what makes T+1 settlement a
#: modelled constraint rather than a footnote.
ACCOUNT_TYPES = ("cash", "margin", "ira")


class BrokerageAccount(Base):
    """One account at one broker, and what the agent may do with it.

    ``agent_placeable`` is the flag Spec L §5.1 requires: reads span every
    account the Robinhood token can see, placement is confined to the Agentic
    account, and a proposal that targets a read-only account must be created
    ``risk_rejected`` with that reason rather than failing later at placement.
    Nothing in this repository places an order in Phase 1; the column exists so
    the refusal is a property of the data rather than of a code path.

    ``capabilities_json`` is the adapter's declared
    :class:`execution.brokers.capabilities.BrokerCapabilities`, recorded at the
    daily probe. Callers check capabilities before intent (Spec L §5).
    """

    __tablename__ = "brokerage_accounts"
    __table_args__ = (
        UniqueConstraint("broker", "external_account_id", name="uq_brokerage_account"),
        Index("ix_brokerage_accounts_enabled", "enabled"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    broker = Column(String(40), nullable=False)
    # Stored masked (`****1234`). The full account number is a credential-like
    # identifier and the ledger has no use for it: the sync matches on the
    # masked form it also writes.
    external_account_id = Column(String(64), nullable=False)
    label = Column(String(100), nullable=False, default="")
    account_type = Column(String(10), nullable=False, default="cash")
    currency = Column(String(10), nullable=False, default="USD")
    booking_method = Column(String(10), nullable=False, default="NONE")

    agent_placeable = Column(Boolean, nullable=False, default=False)
    enabled = Column(Boolean, nullable=False, default=False)

    capabilities_json = Column(Text, nullable=False, default="{}")
    capabilities_probed_at = Column(UtcDateTime, nullable=True)

    last_sync_id = Column(String(36), nullable=True)
    last_sync_at = Column(UtcDateTime, nullable=True)
    last_sync_error = Column(Text, nullable=False, default="")
    last_sync_error_at = Column(UtcDateTime, nullable=True)

    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(UtcDateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)

    @property
    def capabilities(self) -> dict:
        try:
            loaded = json.loads(self.capabilities_json or "{}")
        except (ValueError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


class Holding(Base):
    """A position as one sync saw it. Point-in-time by append, never by update.

    ``average_cost`` and ``cost_basis`` are **null when the broker does not
    supply them**, and null means *unknown*. No consumer may read a null basis
    as zero (`test_unknown_basis_is_null_not_zero`); the helpers in
    ``portfolio/guards.py`` raise instead.

    A non-equity position is stored here too, with ``instrument_type`` set and
    ``notional`` carrying its exposure. A portfolio view that silently omits a
    short put is the worst failure this table has, so options are stored and
    surfaced as a warning rather than filtered out.
    """

    __tablename__ = "holdings"
    __table_args__ = (
        Index("ix_holdings_account_symbol_asof", "account_id", "symbol", "as_of_utc"),
        Index("ix_holdings_sync", "sync_id"),
        Index("ix_holdings_current", "account_id", "superseded_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sync_id = Column(String(36), nullable=False)
    account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False)

    symbol = Column(String(32), nullable=False)
    instrument_type = Column(String(16), nullable=False, default="equity")
    # Option/crypto leg detail: strike, expiry, right, multiplier. Free-form
    # because the ledger does not model them yet and inventing columns for a
    # model that does not exist would be worse than carrying the payload.
    instrument_detail_json = Column(Text, nullable=False, default="{}")

    quantity = Column(Float, nullable=False, default=0.0)
    average_cost = Column(Float, nullable=True)
    cost_basis = Column(Float, nullable=True)
    last_price = Column(Float, nullable=True)
    market_value = Column(Float, nullable=True)
    # Signed exposure for anything that is not a plain long equity position.
    notional = Column(Float, nullable=True)
    currency = Column(String(10), nullable=False, default="USD")

    as_of_utc = Column(UtcDateTime, nullable=False)
    source = Column(String(60), nullable=False, default="")
    superseded_at = Column(UtcDateTime, nullable=True)
    superseded_by_sync_id = Column(String(36), nullable=True)
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)

    @property
    def instrument_detail(self) -> dict:
        try:
            loaded = json.loads(self.instrument_detail_json or "{}")
        except (ValueError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


class TaxLot(Base):
    """An open lot as the broker reports it, where the broker reports lots.

    ``cost_basis`` null means the broker did not expose it. ``booking_method``
    records how a sale from this account books, so a ledger row says which
    method produced a realised figure instead of leaving it to be inferred.
    Basis here is informational: this system does not compute taxes.
    """

    __tablename__ = "tax_lots"
    __table_args__ = (
        Index("ix_tax_lots_account_symbol", "account_id", "symbol"),
        Index("ix_tax_lots_sync", "sync_id"),
        Index("ix_tax_lots_current", "account_id", "superseded_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sync_id = Column(String(36), nullable=False)
    account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False)

    symbol = Column(String(32), nullable=False)
    broker_lot_id = Column(String(64), nullable=True)
    open_date = Column(Date, nullable=True)
    quantity = Column(Float, nullable=False, default=0.0)
    cost_basis = Column(Float, nullable=True)
    # `short` / `long` / `unknown` — never guessed from open_date when the
    # broker is silent, because the holding-period rules have exceptions this
    # ledger does not model.
    term = Column(String(10), nullable=False, default="unknown")
    booking_method = Column(String(10), nullable=False, default="NONE")

    as_of_utc = Column(UtcDateTime, nullable=False)
    source = Column(String(60), nullable=False, default="")
    superseded_at = Column(UtcDateTime, nullable=True)
    superseded_by_sync_id = Column(String(36), nullable=True)
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)


class CashBalance(Base):
    """Settled versus unsettled cash, and what is redeployable when.

    The Agentic account is a cash account (Spec L §5.1): proceeds from a Monday
    close are not redeployable until Tuesday. ``pending_settlements_json`` is a
    list of ``{"amount": float, "settles_on": "YYYY-MM-DD", "source": str}``
    so a proposal that would need T+1 proceeds can be refused *with the
    settlement date*, which is the part that makes the refusal actionable.
    """

    __tablename__ = "cash_balances"
    __table_args__ = (
        Index("ix_cash_balances_account_asof", "account_id", "as_of_utc"),
        Index("ix_cash_balances_sync", "sync_id"),
        Index("ix_cash_balances_current", "account_id", "superseded_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    sync_id = Column(String(36), nullable=False)
    account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False)

    settled_cash = Column(Float, nullable=True)
    unsettled_cash = Column(Float, nullable=True)
    buying_power = Column(Float, nullable=True)
    currency = Column(String(10), nullable=False, default="USD")
    pending_settlements_json = Column(Text, nullable=False, default="[]")

    as_of_utc = Column(UtcDateTime, nullable=False)
    source = Column(String(60), nullable=False, default="")
    superseded_at = Column(UtcDateTime, nullable=True)
    superseded_by_sync_id = Column(String(36), nullable=True)
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)

    @property
    def pending_settlements(self) -> list:
        try:
            loaded = json.loads(self.pending_settlements_json or "[]")
        except (ValueError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []


class BrokerOrder(Base):
    """Every order at the broker, whoever placed it.

    ``origin`` is ``'external'`` for an order Bryan placed in the Robinhood app
    — it appears here with a null ``execution_id``. ``origin='system'`` orders
    carry the ``strategy_trades.execution_id`` that produced them. Phase 1
    writes ``external`` rows only: nothing here places an order.

    Not append-only. An order has a broker-side lifecycle (queued → filled) and
    the row tracks it; the append-only rule is about *positions*, where the
    history is the point.
    """

    __tablename__ = "broker_orders"
    __table_args__ = (
        UniqueConstraint("account_id", "broker_order_id", name="uq_broker_order"),
        Index("ix_broker_orders_account_status", "account_id", "status"),
        Index("ix_broker_orders_symbol", "symbol"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    account_id = Column(Integer, ForeignKey("brokerage_accounts.id"), nullable=False)
    broker_order_id = Column(String(80), nullable=False)
    # The client idempotency key the upstream deduplicates on (Spec L §5.1).
    ref_id = Column(String(80), nullable=True)

    symbol = Column(String(32), nullable=False)
    side = Column(String(10), nullable=False, default="")
    quantity = Column(Float, nullable=True)
    order_type = Column(String(20), nullable=False, default="")
    time_in_force = Column(String(10), nullable=False, default="")
    limit_price = Column(Float, nullable=True)
    stop_price = Column(Float, nullable=True)
    status = Column(String(30), nullable=False, default="")

    submitted_at = Column(UtcDateTime, nullable=True)
    filled_at = Column(UtcDateTime, nullable=True)
    filled_quantity = Column(Float, nullable=True)
    average_fill_price = Column(Float, nullable=True)

    #: `external` (placed in the broker's own app) or `system`.
    origin = Column(String(20), nullable=False, default="external")
    #: `strategy_trades.execution_id` (Spec Q, Phase 5). Deliberately not a
    #: foreign key: cross-phase FKs make the integration merge non-trivial.
    execution_id = Column(String(64), nullable=True)

    sync_id = Column(String(36), nullable=False, default="")
    as_of_utc = Column(UtcDateTime, nullable=False)
    source = Column(String(60), nullable=False, default="")
    raw_json = Column(Text, nullable=False, default="{}")
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(UtcDateTime, nullable=False, default=utcnow_naive, onupdate=utcnow_naive)


class PortfolioSnapshot(Base):
    """The daily rollup, plus the risk figures that sector codes cannot express.

    Beta, realized volatility, and the **maximum and average pairwise**
    correlation among the largest positions are computed deterministically from
    stored daily returns — no vendor risk model and no model call. Every figure
    carries the lookback and the ``n`` it was computed over, because six
    "different" names at 0.8 pairwise correlation are one position and a figure
    without its sample size cannot say so.

    A statistic whose inputs are insufficient is stored **null with a reason**
    in ``metrics_notes_json`` rather than computed from a short window.
    """

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint("snapshot_date", name="uq_portfolio_snapshot_date"),
        Index("ix_portfolio_snapshots_date", "snapshot_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_date = Column(Date, nullable=False)
    as_of_utc = Column(UtcDateTime, nullable=False)
    sync_id = Column(String(36), nullable=False, default="")

    total_value = Column(Float, nullable=True)
    cash_total = Column(Float, nullable=True)
    settled_cash = Column(Float, nullable=True)
    unsettled_cash = Column(Float, nullable=True)
    gross_exposure = Column(Float, nullable=True)
    net_exposure = Column(Float, nullable=True)

    sector_weights_json = Column(Text, nullable=False, default="{}")
    name_weights_json = Column(Text, nullable=False, default="{}")
    largest_positions_json = Column(Text, nullable=False, default="[]")
    largest_position_weight = Column(Float, nullable=True)
    concentration_hhi = Column(Float, nullable=True)

    beta_60 = Column(Float, nullable=True)
    beta_60_n = Column(Integer, nullable=True)
    beta_250 = Column(Float, nullable=True)
    beta_250_n = Column(Integer, nullable=True)
    realized_volatility = Column(Float, nullable=True)
    realized_volatility_n = Column(Integer, nullable=True)
    max_pairwise_correlation = Column(Float, nullable=True)
    avg_pairwise_correlation = Column(Float, nullable=True)
    correlation_lookback_sessions = Column(Integer, nullable=True)
    correlation_names = Column(Integer, nullable=True)
    metrics_notes_json = Column(Text, nullable=False, default="{}")

    #: Hash of the inputs the row was computed from, so a snapshot is
    #: reproducible and a changed input is visible rather than silent.
    inputs_hash = Column(String(64), nullable=False, default="")
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)


class ExposureTag(Base):
    """Exposure by narrative — ``ai-infra``, ``rate-sensitive``, ``china-revenue``.

    Sourced from dossiers (Spec M, Phase 2) and attached by symbol rather than
    by holding row: holdings are re-written every sync, and a tag that had to be
    re-attached each hour would be a tag nobody trusted. ``retired_at`` retires
    a tag without deleting the fact that it once applied.
    """

    __tablename__ = "exposure_tags"
    __table_args__ = (
        UniqueConstraint("symbol", "tag", "source", name="uq_exposure_tag"),
        Index("ix_exposure_tags_tag", "tag"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(32), nullable=False)
    tag = Column(String(60), nullable=False)
    source = Column(String(60), nullable=False, default="manual")
    #: Identifier of the dossier/thesis row the tag came from, when it came
    #: from one. A plain string for the same cross-phase reason as above.
    source_ref = Column(String(120), nullable=True)
    note = Column(Text, nullable=False, default="")
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    retired_at = Column(UtcDateTime, nullable=True)


# ---------------------------------------------------------------------------
# Research workspace — Spec M §3
#
# Five tables: `dossiers` and its append-only `dossier_sections`, `theses` and
# its `thesis_invalidators`, plus `decision_journal` and `research_questions`.
# No foreign key leaves this group, so the Phase 1 integration merge revision
# is a no-op join (migrations/README.md).
# ---------------------------------------------------------------------------

#: Spec M §3: draft -> active -> weakened -> invalidated -> closed.
THESIS_STATUSES = ("draft", "active", "weakened", "invalidated", "closed")

#: Spec M §4. The first four are machine-checkable; `qualitative` is not, and a
#: thesis made of nothing but `qualitative` rows cannot reach `active`.
INVALIDATOR_TYPES = (
    "metric_threshold",
    "price_level",
    "event",
    "time_decay",
    "qualitative",
)
MACHINE_CHECKABLE_INVALIDATOR_TYPES = (
    "metric_threshold",
    "price_level",
    "event",
    "time_decay",
)

#: Spec M §3 `decision_journal`.
DECISION_KINDS = ("opened", "added", "trimmed", "closed", "passed")

#: Spec L §6.6: two budgets, recorded separately so "how do my judgment trades
#: do versus my evidenced trades" is a query.
DECISION_BUDGETS = ("evidenced", "discretionary")


class Dossier(Base):
    """One per ticker: structured metadata plus versioned sections (Spec M §3).

    Everything narrative lives in :class:`DossierSection`, keyed by
    ``section_key``, including the fields Spec M names as structured metadata
    (business model, revenue drivers, key customers, competitive position,
    capital structure). Only ``sector`` is a column: it is the one field that is
    a filter rather than prose, and a section carries the sources and the
    revision history that a scalar column would throw away.
    """

    __tablename__ = "dossiers"
    __table_args__ = (
        UniqueConstraint("ticker", name="uq_dossiers_ticker"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    company_name = Column(String(200), nullable=False, default="")
    sector = Column(String(100), nullable=False, default="")
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)


class DossierSection(Base):
    """Append-only dossier revisions (Spec M §3). Nothing is ever overwritten.

    An update writes a new row whose ``supersedes_id`` points at the row it
    replaces; the prior body stays readable, so "what did we believe in July" is
    a query rather than an archaeology project.

    ``unsourced`` is stored rather than derived so that "every surface that
    renders this section renders the warning" is one column read, and so a
    section written without sources cannot be laundered into a sourced one by a
    later reader's interpretation of ``sources_json``.
    """

    __tablename__ = "dossier_sections"
    __table_args__ = (
        Index("ix_dossier_sections_current", "dossier_id", "section_key", "superseded"),
        Index("ix_dossier_sections_created", "dossier_id", "created_at"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    dossier_id = Column(Integer, ForeignKey("dossiers.id"), nullable=False)
    section_key = Column(String(80), nullable=False)
    body_md = Column(Text, nullable=False, default="")
    # A JSON list of {"url", "tier", "title", "as_of"} objects. Tier is the
    # Spec K §3.3 / Spec P §5 vocabulary in research_workspace/trust.py.
    sources_json = Column(Text, nullable=False, default="[]")
    # "human" or a model id ("claude-opus-4-6"). `author_kind` is the coarse
    # split Spec M §7 requires to be visibly distinct.
    author = Column(String(120), nullable=False, default="human")
    author_kind = Column(String(20), nullable=False, default="human")
    unsourced = Column(Boolean, nullable=False, default=False)
    # Spec P §5: set only by a human asserting authorship, and the only way a
    # section whose sources are all untrusted-tier may be written.
    human_authored = Column(Boolean, nullable=False, default=False)
    supersedes_id = Column(Integer, ForeignKey("dossier_sections.id"), nullable=True)
    # Denormalised "this row is not the current one for its key". Written when
    # the successor is inserted; the row itself is never otherwise touched.
    superseded = Column(Boolean, nullable=False, default=False)
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)

    @property
    def sources(self) -> list:
        try:
            loaded = json.loads(self.sources_json or "[]")
        except (ValueError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []


class Thesis(Base):
    """What we believe, and what would prove it wrong (Spec M §3).

    The two database checks are the parts that can be expressed in DDL on both
    engines:

    * a probability, when present, is a probability;
    * ``active`` requires a stated probability, a ``resolution_at``, and a
      non-zero ``machine_checkable_invalidators`` count.

    The count is denormalised precisely so the second check can exist at all —
    a CHECK constraint cannot count rows in another table. The application
    keeps it true (``research_workspace.store``), and
    ``test_machine_checkable_required`` covers the code path; the constraint is
    the backstop that stops a hand-written ``UPDATE theses SET status='active'``
    from producing an unfalsifiable thesis.

    ``probability`` may be revised; ``original_probability`` is the number the
    Brier score uses forever (Spec M §6), and ``probability_history_json``
    keeps the revisions.
    """

    __tablename__ = "theses"
    __table_args__ = (
        Index("ix_theses_ticker_status", "ticker", "status"),
        Index("ix_theses_next_review", "next_review_at"),
        CheckConstraint(
            "status IN ('draft','active','weakened','invalidated','closed')",
            name="ck_theses_status",
        ),
        CheckConstraint(
            "probability IS NULL OR (probability >= 0.0 AND probability <= 1.0)",
            name="ck_theses_probability_range",
        ),
        CheckConstraint(
            "status <> 'active' OR (probability IS NOT NULL "
            "AND resolution_at IS NOT NULL "
            "AND machine_checkable_invalidators >= 1)",
            name="ck_theses_active_is_falsifiable",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String(20), nullable=False, index=True)
    slug = Column(String(160), nullable=False, default="")
    title = Column(String(300), nullable=False)
    claim = Column(Text, nullable=False, default="")
    direction = Column(String(20), nullable=False, default="long")
    horizon_days = Column(Integer, nullable=True)

    # --- the falsifiable part ---
    probability = Column(Float, nullable=True)
    original_probability = Column(Float, nullable=True)
    probability_history_json = Column(Text, nullable=False, default="[]")
    resolution_at = Column(Date, nullable=True)
    resolution_observable = Column(Text, nullable=False, default="")

    # --- the argument ---
    # A JSON list of {"n", "claim", "evidence": [...]} objects.
    argument_json = Column(Text, nullable=False, default="[]")
    # Required and non-empty before `active` (Spec M §3). Written by the
    # thesis-critic subagent (Spec P §4) and attributed, never merged into the
    # bull argument.
    bear_case = Column(Text, nullable=False, default="")
    bear_case_author = Column(String(120), nullable=False, default="")

    status = Column(String(20), nullable=False, default="draft")
    machine_checkable_invalidators = Column(Integer, nullable=False, default=0)

    # Spec L and Spec N are parallel phases: these are references, not foreign
    # keys, so the integration merge stays a no-op join.
    linked_position_ref = Column(String(120), nullable=False, default="")
    position_opened_at = Column(UtcDateTime, nullable=True)
    cohort_answer_ids_json = Column(Text, nullable=False, default="[]")

    next_review_at = Column(Date, nullable=True)
    author = Column(String(120), nullable=False, default="human")
    author_kind = Column(String(20), nullable=False, default="human")

    # --- resolution, for §6 scoring ---
    outcome = Column(String(20), nullable=True)  # "true" | "false"
    resolved_at = Column(UtcDateTime, nullable=True)
    # Spec M §6: invalidated-versus-quietly-abandoned is an honesty metric, so
    # the two closes are different rows, not one "closed" bucket.
    close_reason = Column(String(40), nullable=False, default="")

    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    updated_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    activated_at = Column(UtcDateTime, nullable=True)
    weakened_at = Column(UtcDateTime, nullable=True)
    closed_at = Column(UtcDateTime, nullable=True)

    @property
    def argument(self) -> list:
        try:
            loaded = json.loads(self.argument_json or "[]")
        except (ValueError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []

    @property
    def probability_history(self) -> list:
        try:
            loaded = json.loads(self.probability_history_json or "[]")
        except (ValueError, TypeError):
            return []
        return loaded if isinstance(loaded, list) else []


class ThesisInvalidator(Base):
    """The load-bearing table (Spec M §4).

    ``params_json`` carries the machine-checkable parameters for its type; the
    vocabulary and the evaluation are in ``research_workspace/invalidators.py``.
    ``post_hoc`` is set at write time by comparing ``created_at`` against the
    thesis's ``position_opened_at``: an invalidator added after entry is kept,
    flagged, and excluded from the honesty metrics rather than deleted.
    """

    __tablename__ = "thesis_invalidators"
    __table_args__ = (
        Index("ix_thesis_invalidators_thesis", "thesis_id", "status"),
        CheckConstraint(
            "type IN ('metric_threshold','price_level','event','time_decay',"
            "'qualitative')",
            name="ck_thesis_invalidators_type",
        ),
        CheckConstraint(
            "status IN ('armed','triggered','needs_human_review','retired')",
            name="ck_thesis_invalidators_status",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    thesis_id = Column(Integer, ForeignKey("theses.id"), nullable=False)
    description = Column(Text, nullable=False)
    type = Column(String(30), nullable=False)
    params_json = Column(Text, nullable=False, default="{}")
    machine_checkable = Column(Boolean, nullable=False, default=False)
    post_hoc = Column(Boolean, nullable=False, default=False)
    status = Column(String(30), nullable=False, default="armed")
    last_checked_at = Column(UtcDateTime, nullable=True)
    last_check_detail = Column(Text, nullable=False, default="")
    triggered_at = Column(UtcDateTime, nullable=True)
    triggered_reason = Column(Text, nullable=False, default="")
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
    author = Column(String(120), nullable=False, default="human")

    @property
    def params(self) -> dict:
        try:
            loaded = json.loads(self.params_json or "{}")
        except (ValueError, TypeError):
            return {}
        return loaded if isinstance(loaded, dict) else {}


class DecisionJournalEntry(Base):
    """Append-only decision record (Spec M §3), including the decision to pass.

    ``thesis_hash`` and ``cohort_evidence_hash`` freeze *what was believed and
    what was cited at the time*, so a later revision of either cannot rewrite
    the record of the decision. ``budget`` is Spec L §6.6's evidenced/
    discretionary split, recorded here because the journal is where "how do my
    judgment trades do versus my evidenced trades" is answered.

    ``outcome_*`` is filled in later by a job, never at write time.
    """

    __tablename__ = "decision_journal"
    __table_args__ = (
        Index("ix_decision_journal_occurred", "occurred_on"),
        Index("ix_decision_journal_tickers", "tickers"),
        CheckConstraint(
            "decision IN ('opened','added','trimmed','closed','passed')",
            name="ck_decision_journal_decision",
        ),
        CheckConstraint(
            "budget IN ('evidenced','discretionary')",
            name="ck_decision_journal_budget",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    occurred_on = Column(Date, nullable=False)
    # Comma-separated, uppercase. A decision can touch more than one name and
    # this table is read far more often than it is joined.
    tickers = Column(String(200), nullable=False, default="")
    decision = Column(String(20), nullable=False)
    thesis_id = Column(Integer, ForeignKey("theses.id"), nullable=True)
    thesis_hash = Column(String(64), nullable=False, default="")
    # Spec N identifies an answer by its setup hash and as-of date; the id
    # recorded here is that pair, and the hash is over the answer's canonical
    # JSON so a re-run that differs is detectable.
    cohort_answer_id = Column(String(120), nullable=False, default="")
    cohort_evidence_hash = Column(String(64), nullable=False, default="")
    budget = Column(String(20), nullable=False, default="discretionary")
    sizing_rationale = Column(Text, nullable=False, default="")
    expected_holding_days = Column(Integer, nullable=True)
    note_md = Column(Text, nullable=False, default="")
    author = Column(String(120), nullable=False, default="human")
    author_kind = Column(String(20), nullable=False, default="human")
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)

    # --- filled in later, by a job, never at write time ---
    outcome_note = Column(Text, nullable=False, default="")
    outcome_return_pct = Column(Float, nullable=True)
    outcome_exit_reason = Column(String(80), nullable=False, default="")
    outcome_matched_reason = Column(Boolean, nullable=True)
    outcome_recorded_at = Column(UtcDateTime, nullable=True)


class ResearchQuestion(Base):
    """What an agent could not resolve, written down instead of guessed at."""

    __tablename__ = "research_questions"
    __table_args__ = (
        Index("ix_research_questions_status", "status"),
        CheckConstraint(
            "status IN ('open','answered','abandoned')",
            name="ck_research_questions_status",
        ),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    question = Column(Text, nullable=False)
    ticker = Column(String(20), nullable=False, default="")
    status = Column(String(20), nullable=False, default="open")
    asked_by = Column(String(120), nullable=False, default="human")
    answer_md = Column(Text, nullable=False, default="")
    answered_at = Column(UtcDateTime, nullable=True)
    created_at = Column(UtcDateTime, nullable=False, default=utcnow_naive)
