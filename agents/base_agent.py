"""
Base class for all signal agents.
Each agent is stateless — receives inputs, produces an AgentOutput.
All state lives in the database.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Optional
import uuid
import json
from database.db import get_session
from database.models import Signal, Ticker


@dataclass
class AgentOutput:
    agent_type: str
    ticker: Optional[str] = None
    score: float = 0.5
    confidence: float = 0.5
    direction: str = "neutral"  # bullish, bearish, neutral
    reasoning: str = ""
    raw_data: dict = field(default_factory=dict)
    run_id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self):
        """Persist signal to the database."""
        with get_session() as session:
            ticker_obj = None
            if self.ticker:
                ticker_obj = session.query(Ticker).filter_by(symbol=self.ticker).first()
            signal = Signal(
                ticker_id=ticker_obj.id if ticker_obj else None,
                agent_type=self.agent_type,
                run_id=self.run_id,
                score=self.score,
                confidence=self.confidence,
                direction=self.direction,
                reasoning=self.reasoning,
                raw_output=json.dumps(self.raw_data),
            )
            session.add(signal)


class BaseAgent(ABC):
    """Abstract base class for all signal agents."""

    agent_type: str = "base"

    def __init__(self, settings, anthropic_client=None):
        self.settings = settings
        self.client = anthropic_client
        self.run_id = str(uuid.uuid4())[:8]

    @abstractmethod
    def analyze(self, ticker: str = None, **kwargs) -> AgentOutput:
        """Run analysis and return an AgentOutput."""
        ...
