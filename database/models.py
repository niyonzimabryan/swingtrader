import json
from datetime import date
from utils.timeutils import utcnow_naive
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, Date,
    ForeignKey, Enum, UniqueConstraint, Index, create_engine
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
