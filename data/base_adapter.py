from abc import ABC, abstractmethod


class DataAdapter(ABC):
    """Abstract base class for all data source adapters."""

    @abstractmethod
    def fetch(self, ticker: str, **kwargs) -> dict:
        """Fetch data for a ticker. Returns a normalized dict."""
        ...

    def cache_key(self, ticker: str, **kwargs) -> str:
        """Return a cache key for deduplication."""
        return f"{self.__class__.__name__}:{ticker}"
