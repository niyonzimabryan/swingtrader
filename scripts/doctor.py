"""
Environment doctor for Swing Trader onboarding.

Usage:
    python -m scripts.doctor
    python -m scripts.doctor --skip-live
    python -m scripts.doctor --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from config.onboarding import (
    FIELD_BY_NAME,
    EnvField,
    ENV_FIELDS,
    completion_counts,
    is_configured_value,
    merged_env_values,
    required_fields,
)


@dataclass
class CheckResult:
    name: str
    status: str
    message: str
    detail: str = ""
    group: str = ""

    @property
    def ok(self) -> bool:
        return self.status in {"pass", "info", "warn"}


def _configured(values: dict[str, str], field: EnvField) -> bool:
    return is_configured_value(values.get(field.name, ""), field)


def check_required_presence(values: dict[str, str]) -> list[CheckResult]:
    results = []
    for field in required_fields():
        if _configured(values, field):
            results.append(
                CheckResult(
                    field.name,
                    "pass",
                    f"{field.label} is configured.",
                    group=field.group,
                )
            )
        else:
            results.append(
                CheckResult(
                    field.name,
                    "fail",
                    f"{field.label} is required.",
                    "Add this value in the setup wizard or .env.",
                    group=field.group,
                )
            )
    return results


def check_optional_presence(values: dict[str, str]) -> list[CheckResult]:
    results = []
    for field in ENV_FIELDS:
        if field.required:
            continue
        if _configured(values, field):
            if field.default and values.get(field.name, "") == field.default:
                continue
            results.append(
                CheckResult(
                    field.name,
                    "info",
                    f"{field.label} add-on is configured.",
                    group=field.group,
                )
            )
        elif field.name == "GEMINI_API_KEY":
            results.append(
                CheckResult(
                    field.name,
                    "warn",
                    "Gemini add-on is skipped.",
                    "Setup can complete without Gemini; screening/deep research enhancements stay disabled.",
                    group=field.group,
                )
            )
    return results


def check_database(values: dict[str, str]) -> CheckResult:
    database_url = values.get("DATABASE_URL", "")
    if not database_url:
        return CheckResult("DATABASE_URL", "fail", "Database URL is missing.", group="runtime")
    if not database_url.startswith("sqlite:///"):
        return CheckResult(
            "DATABASE_URL",
            "warn",
            "Database URL is not SQLite.",
            "The local wizard only verifies SQLite paths; run the app to verify other engines.",
            group="runtime",
        )

    raw_path = database_url.replace("sqlite:///", "", 1)
    db_path = Path(raw_path)
    parent = db_path.parent if db_path.parent != Path("") else Path(".")
    try:
        parent.mkdir(parents=True, exist_ok=True)
        probe = parent / ".swingtrader_db_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return CheckResult(
            "DATABASE_URL",
            "pass",
            "SQLite database path is writable.",
            str(db_path),
            group="runtime",
        )
    except Exception as exc:
        return CheckResult(
            "DATABASE_URL",
            "fail",
            "SQLite database path is not writable.",
            str(exc),
            group="runtime",
        )


async def _get_json(client: httpx.AsyncClient, url: str, **kwargs) -> tuple[bool, dict | list | str]:
    try:
        response = await client.get(url, timeout=12, **kwargs)
        try:
            payload = response.json()
        except Exception:
            payload = response.text[:300]
        return response.status_code < 400, payload
    except Exception as exc:
        return False, str(exc)


async def validate_telegram(values: dict[str, str]) -> list[CheckResult]:
    token = values.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = values.get("TELEGRAM_CHAT_ID", "")
    if not token:
        return []
    async with httpx.AsyncClient() as client:
        ok, payload = await _get_json(client, f"https://api.telegram.org/bot{token}/getMe")
        if ok and isinstance(payload, dict) and payload.get("ok"):
            username = payload.get("result", {}).get("username", "")
            results = [
                CheckResult(
                    "TELEGRAM_BOT_TOKEN",
                    "pass",
                    "Telegram bot token is valid.",
                    f"@{username}" if username else "",
                    group="telegram",
                )
            ]
        else:
            return [
                CheckResult(
                    "TELEGRAM_BOT_TOKEN",
                    "fail",
                    "Telegram bot token failed validation.",
                    _compact_payload(payload),
                    group="telegram",
                )
            ]

        if chat_id:
            ok, payload = await _get_json(
                client,
                f"https://api.telegram.org/bot{token}/getChat",
                params={"chat_id": chat_id},
            )
            results.append(
                CheckResult(
                    "TELEGRAM_CHAT_ID",
                    "pass" if ok and isinstance(payload, dict) and payload.get("ok") else "fail",
                    "Telegram chat is reachable." if ok else "Telegram chat ID failed validation.",
                    _compact_payload(payload) if not ok else "",
                    group="telegram",
                )
            )
        return results


async def validate_alpaca(values: dict[str, str]) -> list[CheckResult]:
    api_key = values.get("ALPACA_API_KEY", "")
    secret_key = values.get("ALPACA_SECRET_KEY", "")
    base_url = values.get("ALPACA_BASE_URL", "")
    if not api_key or not secret_key or not base_url:
        return []
    parsed = urlparse(base_url)
    if "paper-api.alpaca.markets" not in parsed.netloc:
        return [
            CheckResult(
                "ALPACA_BASE_URL",
                "fail",
                "Alpaca must use the paper trading endpoint for open-source setup.",
                base_url,
                group="trading",
            )
        ]

    headers = {"APCA-API-KEY-ID": api_key, "APCA-API-SECRET-KEY": secret_key}
    async with httpx.AsyncClient() as client:
        ok, payload = await _get_json(client, f"{base_url.rstrip('/')}/v2/account", headers=headers)
    if ok and isinstance(payload, dict):
        status = payload.get("status", "account")
        return [
            CheckResult(
                "ALPACA_API_KEY",
                "pass",
                "Alpaca paper account is reachable.",
                f"status={status}",
                group="trading",
            )
        ]
    return [
        CheckResult(
            "ALPACA_API_KEY",
            "fail",
            "Alpaca paper credentials failed validation.",
            _compact_payload(payload),
            group="trading",
        )
    ]


async def validate_market_data(values: dict[str, str]) -> list[CheckResult]:
    checks = []
    async with httpx.AsyncClient() as client:
        if values.get("FINNHUB_API_KEY"):
            ok, payload = await _get_json(
                client,
                "https://finnhub.io/api/v1/quote",
                params={"symbol": "AAPL", "token": values["FINNHUB_API_KEY"]},
            )
            checks.append(
                CheckResult(
                    "FINNHUB_API_KEY",
                    "pass" if ok and _looks_like_finnhub_quote(payload) else "fail",
                    "Finnhub quote endpoint is reachable."
                    if ok
                    else "Finnhub API key failed validation.",
                    _compact_payload(payload) if not ok else "",
                    group="market_data",
                )
            )

        if values.get("FMP_API_KEY"):
            ok, payload = await _get_json(
                client,
                "https://financialmodelingprep.com/stable/profile",
                params={"symbol": "AAPL", "apikey": values["FMP_API_KEY"]},
            )
            checks.append(
                CheckResult(
                    "FMP_API_KEY",
                    "pass" if ok and not _payload_has_error(payload) else "fail",
                    "FMP profile endpoint is reachable." if ok else "FMP API key failed validation.",
                    _compact_payload(payload) if not ok or _payload_has_error(payload) else "",
                    group="market_data",
                )
            )

        if values.get("ALPHA_VANTAGE_API_KEY"):
            ok, payload = await _get_json(
                client,
                "https://www.alphavantage.co/query",
                params={
                    "function": "GLOBAL_QUOTE",
                    "symbol": "IBM",
                    "apikey": values["ALPHA_VANTAGE_API_KEY"],
                },
            )
            checks.append(
                CheckResult(
                    "ALPHA_VANTAGE_API_KEY",
                    "pass" if ok and not _payload_has_error(payload) else "fail",
                    "Alpha Vantage quote endpoint is reachable."
                    if ok
                    else "Alpha Vantage API key failed validation.",
                    _compact_payload(payload) if not ok or _payload_has_error(payload) else "",
                    group="market_data",
                )
            )

        if values.get("FRED_API_KEY"):
            ok, payload = await _get_json(
                client,
                "https://api.stlouisfed.org/fred/series",
                params={"series_id": "GDP", "api_key": values["FRED_API_KEY"], "file_type": "json"},
            )
            checks.append(
                CheckResult(
                    "FRED_API_KEY",
                    "pass" if ok and not _payload_has_error(payload) else "fail",
                    "FRED series endpoint is reachable." if ok else "FRED API key failed validation.",
                    _compact_payload(payload) if not ok or _payload_has_error(payload) else "",
                    group="market_data",
                )
            )
    return checks


async def validate_gemini(values: dict[str, str]) -> list[CheckResult]:
    if not values.get("GEMINI_API_KEY"):
        return []
    # Avoid a dependency-specific call here; google-genai surfaces validation only on generation.
    return [
        CheckResult(
            "GEMINI_API_KEY",
            "info",
            "Gemini add-on key is present.",
            "Run a scan to verify quota/model access.",
            group="gemini",
        )
    ]


async def run_doctor(
    env_path: str | Path = ".env",
    skip_live: bool = False,
    provided_values: dict[str, str] | None = None,
) -> list[CheckResult]:
    values = merged_env_values(env_path)
    if provided_values:
        values.update({key: value for key, value in provided_values.items() if key in FIELD_BY_NAME})

    results = []
    results.extend(check_required_presence(values))
    results.extend(check_optional_presence(values))
    results.append(check_database(values))

    if not skip_live:
        results.extend(await validate_telegram(values))
        results.extend(await validate_alpaca(values))
        results.extend(await validate_market_data(values))
        results.extend(await validate_gemini(values))
    return results


def summarize_results(results: list[CheckResult]) -> dict:
    return {
        "pass": sum(1 for result in results if result.status == "pass"),
        "fail": sum(1 for result in results if result.status == "fail"),
        "warn": sum(1 for result in results if result.status == "warn"),
        "info": sum(1 for result in results if result.status == "info"),
    }


def _looks_like_finnhub_quote(payload) -> bool:
    return isinstance(payload, dict) and any(key in payload for key in ("c", "d", "pc"))


def _payload_has_error(payload) -> bool:
    if isinstance(payload, dict):
        keys = {key.lower() for key in payload}
        if {"error message", "note", "information"} & keys:
            return True
        text = json.dumps(payload).lower()
        return "invalid api" in text or "apikey" in text and "invalid" in text
    if isinstance(payload, str):
        lowered = payload.lower()
        return "invalid api" in lowered or "error" in lowered
    return False


def _compact_payload(payload) -> str:
    if isinstance(payload, (dict, list)):
        return json.dumps(payload)[:500]
    return str(payload)[:500]


def _print_human(results: list[CheckResult], values: dict[str, str]) -> None:
    counts = completion_counts(values)
    print(f"Required setup: {counts['complete']}/{counts['required']} configured")
    print("")
    symbols = {"pass": "OK", "fail": "FAIL", "warn": "WARN", "info": "INFO"}
    for result in results:
        marker = symbols.get(result.status, result.status.upper())
        print(f"[{marker}] {result.name}: {result.message}")
        if result.detail:
            print(f"       {result.detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate Swing Trader onboarding configuration.")
    parser.add_argument("--env", default=".env", help="Path to .env file.")
    parser.add_argument("--skip-live", action="store_true", help="Only check presence/local filesystem.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    values = merged_env_values(args.env)
    results = asyncio.run(run_doctor(args.env, skip_live=args.skip_live))
    if args.json:
        print(
            json.dumps(
                {
                    "summary": summarize_results(results),
                    "completion": completion_counts(values),
                    "results": [asdict(result) for result in results],
                },
                indent=2,
            )
        )
    else:
        _print_human(results, values)
    return 1 if any(result.status == "fail" for result in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
