"""The price plane's flag and its one construction point.

`PRICE_PLANE_ENABLED` defaults to false (README §3, "every spec ships behind a
flag defaulting off"). The flag gates the *entry points* — the backfill, the
universe job, the audit script — not the library: the tests build planes
directly, because a flag that also switched off the code under test would only
be testing the flag.
"""

from __future__ import annotations

from pathlib import Path

from data.prices.base import PricePlane, PricePlaneConfigError

SOURCE_FIXTURE = "fixture"
SOURCE_SHARADAR = "sharadar"
KNOWN_SOURCES = (SOURCE_FIXTURE, SOURCE_SHARADAR)


def get_settings():
    from config.settings import Settings

    return Settings()


def is_enabled(settings=None) -> bool:
    return bool(getattr(settings or get_settings(), "price_plane_enabled", False))


def require_enabled(settings=None) -> None:
    if not is_enabled(settings):
        raise PricePlaneConfigError(
            "PRICE_PLANE_ENABLED is false. The price plane is additive and off by "
            "default; set PRICE_PLANE_ENABLED=true to run a backfill, the universe "
            "job or the delisting audit."
        )


def build_plane(source: str | None = None, settings=None, fixture_root: Path | str | None = None) -> PricePlane:
    """The one place a `PricePlane` implementation is chosen."""
    settings = settings or get_settings()
    source = (source or getattr(settings, "price_plane_source", SOURCE_FIXTURE)).strip().lower()

    if source == SOURCE_FIXTURE:
        from data.prices.fixture_plane import FixturePricePlane

        return FixturePricePlane(fixture_root)
    if source == SOURCE_SHARADAR:
        from data.prices.sharadar import SharadarPricePlane

        # `sharadar_api_key` is the direct-API name; `nasdaq_data_link_api_key`
        # is read too so a `Settings` that has not been renamed yet still
        # works. Either way, `SharadarPricePlane` also falls back to reading
        # `SHARADAR_API_KEY` / `NASDAQ_DATA_LINK_API_KEY` from the environment
        # directly if `Settings` carries neither attribute.
        key = (
            getattr(settings, "sharadar_api_key", "")
            or getattr(settings, "nasdaq_data_link_api_key", "")
            or None
        )
        return SharadarPricePlane(key)
    raise PricePlaneConfigError(f"unknown price plane source {source!r}; expected one of {KNOWN_SOURCES}")
