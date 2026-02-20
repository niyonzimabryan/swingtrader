import json
from datetime import datetime, date
from sqlalchemy import (
    Column, Integer, String, Float, Boolean, Text, DateTime, Date,
    ForeignKey, Enum, UniqueConstraint, Index, create_engine
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Ticker(Base):
    __tablename__ = "tickers"

    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String(10), unique=True, nullable=False, index=True)
    name = Column(String(200), default="")
    sector = Column(String(100), default="")
    market_cap = Column(Float, default=0)
    in_universe = Column(Boolean, default=True)
    added_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    price_data = relationship("PriceData", back_populates="ticker", cascade="all, delete-orphan")
    catalysts = relationship("Catalyst", back_populates="ticker", cascade="all, delete-orphan")
    fundamentals = relationship("FundamentalData", back_populates="ticker", cascade="all, delete-orphan")
    signals = relationship("Signal", back_populates="ticker", cascade="all, delete-orphan")
    trades = relationship("Trade", back_populates="ticker", cascade="all, delete-orphan")
    memos = relationship("Memo", back_populates="ticker", cascade="all, delete-orphan")
    reddit_sentiments = relationship("RedditSentiment", back_populates="ticker", cascade="all, delete-orphan")


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
    detected_at = Column(DateTime, default=datetime.utcnow)
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
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

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
    created_at = Column(DateTime, default=datetime.utcnow)

    ticker = relationship("Ticker", back_populates="signals")


class Trade(Base):
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    memo_id = Column(Integer, ForeignKey("memos.id"), nullable=True)
    direction = Column(String(10), default="long")  # long, short
    entry_price = Column(Float, default=0)
    exit_price = Column(Float, nullable=True)
    entry_date = Column(DateTime, nullable=True)
    exit_date = Column(DateTime, nullable=True)
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
    operator_notes = Column(Text, default="")
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    ticker = relationship("Ticker", back_populates="trades")
    memo = relationship("Memo", back_populates="trade")


class Memo(Base):
    __tablename__ = "memos"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    composite_score = Column(Float, default=0)
    classification = Column(String(20), default="")  # high_conviction, moderate, low, no_action
    direction = Column(String(10), default="long")
    full_text = Column(Text, default="")
    trade_params = Column(Text, default="{}")  # JSON: entry, stop, targets, size
    signal_breakdown = Column(Text, default="{}")  # JSON: per-agent scores
    opus_critique = Column(Text, default="")
    thesis = Column(Text, default="")
    bear_case = Column(Text, default="")
    status = Column(String(20), default="pending")  # pending, approved, rejected, watchlisted, expired
    operator_notes = Column(Text, default="")
    telegram_message_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    responded_at = Column(DateTime, nullable=True)

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
    created_at = Column(DateTime, default=datetime.utcnow)


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
    created_at = Column(DateTime, default=datetime.utcnow)


class RedditSentiment(Base):
    __tablename__ = "reddit_sentiment"
    __table_args__ = (
        UniqueConstraint("ticker_id", "date", name="uq_reddit_ticker_date"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticker_id = Column(Integer, ForeignKey("tickers.id"), nullable=False)
    date = Column(Date, nullable=False)
    mention_volume = Column(String(20), default="normal")  # high, normal, low
    mention_volume_zscore = Column(Float, default=0)
    sentiment = Column(String(20), default="neutral")  # bullish, bearish, mixed, neutral
    sentiment_shift = Column(String(30), default="stable")  # newly_bullish, increasingly_bearish, stable, reversing
    contrarian_flag = Column(Boolean, default=False)
    reasoning = Column(Text, default="")
    raw_data = Column(Text, default="{}")  # JSON

    ticker = relationship("Ticker", back_populates="reddit_sentiments")
