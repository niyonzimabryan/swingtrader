"""
Onboarding configuration for the open-source setup flow.

This module is intentionally independent from Settings so docs, doctor checks,
and the local setup wizard all agree on the same required keys.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from dotenv import dotenv_values


@dataclass(frozen=True)
class EnvField:
    name: str
    label: str
    group: str
    description: str
    required: bool = True
    secret: bool = True
    default: str = ""
    placeholder: str = ""
    signup_url: str = ""
    docs_url: str = ""

    def to_public_dict(self, value: str = "") -> dict:
        payload = asdict(self)
        payload["configured"] = is_configured_value(value, self)
        payload["masked_value"] = mask_value(value) if payload["configured"] else ""
        return payload


ENV_GROUPS = [
    {
        "id": "core",
        "label": "Core AI",
        "summary": "Claude is the primary analysis engine and web-search provider.",
    },
    {
        "id": "telegram",
        "label": "Telegram",
        "summary": "Creates the operator command surface and memo approval channel.",
    },
    {
        "id": "trading",
        "label": "Paper Trading",
        "summary": "Connects the bot to an Alpaca paper brokerage account.",
    },
    {
        "id": "market_data",
        "label": "Market Data",
        "summary": "Required data providers for news, fundamentals, backups, and macro.",
    },
    {
        "id": "runtime",
        "label": "Runtime",
        "summary": "Local database and scheduler defaults for the first run.",
    },
    {
        "id": "gemini",
        "label": "Gemini Search",
        "summary": "Recommended grounded search for discovery, web research, screening, and deep research.",
    },
    {
        "id": "observability",
        "label": "Observability",
        "summary": "Optional tracing and cost visibility.",
    },
    {
        "id": "advanced",
        "label": "Advanced",
        "summary": "Future or legacy integrations that are not required for setup.",
    },
]


ENV_FIELDS = [
    EnvField(
        name="ANTHROPIC_API_KEY",
        label="Anthropic API key",
        group="core",
        description="Required. Claude runs the core analysis, scoring, memo writing, and Anthropic web search.",
        placeholder="sk-ant-...",
        signup_url="https://console.anthropic.com/settings/keys",
        docs_url="https://docs.anthropic.com/",
    ),
    EnvField(
        name="TELEGRAM_BOT_TOKEN",
        label="Telegram bot token",
        group="telegram",
        description="Required. Create a dedicated bot with BotFather and paste its token here.",
        placeholder="123456789:ABC...",
        signup_url="https://t.me/BotFather",
        docs_url="https://core.telegram.org/bots/features#botfather",
    ),
    EnvField(
        name="TELEGRAM_CHAT_ID",
        label="Telegram chat ID",
        group="telegram",
        description="Required. The wizard can discover this after you message your new bot.",
        placeholder="123456789",
        signup_url="https://core.telegram.org/bots/api#getupdates",
        secret=False,
    ),
    EnvField(
        name="ALPACA_API_KEY",
        label="Alpaca paper API key",
        group="trading",
        description="Required. Paper trading account key.",
        placeholder="PK...",
        signup_url="https://app.alpaca.markets/paper/dashboard/overview",
    ),
    EnvField(
        name="ALPACA_SECRET_KEY",
        label="Alpaca paper secret key",
        group="trading",
        description="Required. Paper trading account secret.",
        placeholder="secret...",
        signup_url="https://app.alpaca.markets/paper/dashboard/overview",
    ),
    EnvField(
        name="ALPACA_BASE_URL",
        label="Alpaca base URL",
        group="trading",
        description="Required. Keep this on the paper endpoint unless you intentionally change the code.",
        secret=False,
        default="https://paper-api.alpaca.markets",
        placeholder="https://paper-api.alpaca.markets",
        signup_url="https://docs.alpaca.markets/",
    ),
    EnvField(
        name="FINNHUB_API_KEY",
        label="Finnhub API key",
        group="market_data",
        description="Required. News, earnings calendar, analyst recommendations, and price targets.",
        placeholder="finnhub...",
        signup_url="https://finnhub.io/dashboard",
    ),
    EnvField(
        name="FMP_API_KEY",
        label="Financial Modeling Prep API key",
        group="market_data",
        description="Required. Fundamentals and pattern-data fallback coverage.",
        placeholder="fmp...",
        signup_url="https://site.financialmodelingprep.com/developer/docs",
    ),
    EnvField(
        name="ALPHA_VANTAGE_API_KEY",
        label="Alpha Vantage API key",
        group="market_data",
        description="Required. Backup financial data provider.",
        placeholder="alpha...",
        signup_url="https://www.alphavantage.co/support/#api-key",
    ),
    EnvField(
        name="FRED_API_KEY",
        label="FRED API key",
        group="market_data",
        description="Required. Macro rates, yield curve, and credit spread inputs.",
        placeholder="fred...",
        signup_url="https://fred.stlouisfed.org/docs/api/api_key.html",
    ),
    EnvField(
        name="DATABASE_URL",
        label="Database URL",
        group="runtime",
        description="Required. SQLite is the supported local default.",
        required=True,
        secret=False,
        default="sqlite:///swing_trader.db",
        placeholder="sqlite:///swing_trader.db",
    ),
    EnvField(
        name="SCHEDULER_ENABLED",
        label="Start scheduler automatically",
        group="runtime",
        description="Required runtime switch. Start with false while testing Telegram and paper trading.",
        required=True,
        secret=False,
        default="false",
        placeholder="false",
    ),
    EnvField(
        name="WEB_SEARCH_PROVIDER",
        label="Grounded search provider",
        group="gemini",
        description="Defaults to Gemini for discovery and web research. Falls back to Anthropic only if Gemini is not configured.",
        required=False,
        secret=False,
        default="gemini",
        placeholder="gemini",
    ),
    EnvField(
        name="GEMINI_API_KEY",
        label="Gemini API key",
        group="gemini",
        description="Optional add-on for open-source setup; when present, powers Gemini Pro discovery/web research, Flash screening, and deep research.",
        required=False,
        placeholder="AIza...",
        signup_url="https://aistudio.google.com/app/apikey",
    ),
    EnvField(
        name="GEMINI_SEARCH_MODEL",
        label="Gemini grounded search model",
        group="gemini",
        description="Gemini model used by generic grounded search when a stage does not specify its own model.",
        required=False,
        secret=False,
        default="gemini-3.1-pro-preview",
        placeholder="gemini-3.1-pro-preview",
    ),
    EnvField(
        name="GEMINI_DISCOVERY_MODEL",
        label="Gemini discovery model",
        group="gemini",
        description="Gemini Pro model used to search for fresh trade ideas.",
        required=False,
        secret=False,
        default="gemini-3.1-pro-preview",
        placeholder="gemini-3.1-pro-preview",
    ),
    EnvField(
        name="GEMINI_WEB_RESEARCH_MODEL",
        label="Gemini web research model",
        group="gemini",
        description="Gemini Pro model used to scrutinize catalyst-qualified tickers.",
        required=False,
        secret=False,
        default="gemini-3.1-pro-preview",
        placeholder="gemini-3.1-pro-preview",
    ),
    EnvField(
        name="GEMINI_FLASH_MODEL",
        label="Gemini Flash model",
        group="gemini",
        description="Optional add-on setting used when Gemini is configured.",
        required=False,
        secret=False,
        default="gemini-2.0-flash",
        placeholder="gemini-2.0-flash",
    ),
    EnvField(
        name="LANGFUSE_PUBLIC_KEY",
        label="Langfuse public key",
        group="observability",
        description="Optional. Enables LLM tracing and cost observability.",
        required=False,
        placeholder="pk-lf-...",
        signup_url="https://cloud.langfuse.com/",
    ),
    EnvField(
        name="LANGFUSE_SECRET_KEY",
        label="Langfuse secret key",
        group="observability",
        description="Optional. Pair with the Langfuse public key.",
        required=False,
        placeholder="sk-lf-...",
        signup_url="https://cloud.langfuse.com/",
    ),
    EnvField(
        name="LANGFUSE_BASE_URL",
        label="Langfuse base URL",
        group="observability",
        description="Optional. US cloud endpoint by default.",
        required=False,
        secret=False,
        default="https://us.cloud.langfuse.com",
        placeholder="https://us.cloud.langfuse.com",
    ),
    EnvField(
        name="OPENAI_API_KEY",
        label="OpenAI API key",
        group="advanced",
        description="Optional future provider path. Not required for the current setup.",
        required=False,
        placeholder="sk-...",
        signup_url="https://platform.openai.com/api-keys",
    ),
    EnvField(
        name="REDDIT_CLIENT_ID",
        label="Reddit client ID",
        group="advanced",
        description="Optional legacy sentiment adapter. Current research flow does not require it.",
        required=False,
        placeholder="client id...",
        signup_url="https://www.reddit.com/prefs/apps",
    ),
    EnvField(
        name="REDDIT_CLIENT_SECRET",
        label="Reddit client secret",
        group="advanced",
        description="Optional legacy sentiment adapter secret.",
        required=False,
        placeholder="client secret...",
        signup_url="https://www.reddit.com/prefs/apps",
    ),
    EnvField(
        name="REDDIT_USER_AGENT",
        label="Reddit user agent",
        group="advanced",
        description="Optional legacy sentiment adapter setting.",
        required=False,
        secret=False,
        default="SwingTrader/1.0",
        placeholder="SwingTrader/1.0",
    ),
]


FIELD_BY_NAME = {field.name: field for field in ENV_FIELDS}


def required_fields() -> list[EnvField]:
    return [field for field in ENV_FIELDS if field.required]


def optional_fields() -> list[EnvField]:
    return [field for field in ENV_FIELDS if not field.required]


def read_env_file(path: str | Path = ".env") -> dict[str, str]:
    env_path = Path(path)
    if not env_path.exists():
        return {}
    return {key: value or "" for key, value in dotenv_values(env_path).items()}


def default_env_values() -> dict[str, str]:
    return {field.name: field.default for field in ENV_FIELDS if field.default}


def merged_env_values(path: str | Path = ".env") -> dict[str, str]:
    values = default_env_values()
    values.update(read_env_file(path))
    return values


def is_placeholder_value(value: str) -> bool:
    normalized = (value or "").strip().lower()
    if not normalized:
        return False
    return (
        normalized.startswith("your_")
        or normalized.startswith("replace_")
        or normalized in {"changeme", "todo", "tbd", "xxx", "..."}
    )


def is_configured_value(value: str, field: EnvField) -> bool:
    value = (value or "").strip()
    if not value or is_placeholder_value(value):
        return False
    if value == field.default and field.default:
        return True
    return True


def completion_counts(values: dict[str, str]) -> dict[str, int]:
    required = required_fields()
    complete = sum(1 for field in required if is_configured_value(values.get(field.name, ""), field))
    return {"complete": complete, "required": len(required)}


def public_schema(values: dict[str, str] | None = None) -> dict:
    values = values or {}
    return {
        "groups": ENV_GROUPS,
        "fields": [field.to_public_dict(values.get(field.name, "")) for field in ENV_FIELDS],
        "completion": completion_counts(values),
    }


def mask_value(value: str) -> str:
    value = value or ""
    if len(value) <= 4:
        return "****" if value else ""
    if len(value) <= 10:
        return f"{value[:2]}****{value[-2:]}"
    return f"{value[:4]}******{value[-4:]}"


def render_env_file(values: dict[str, str], unknown_values: dict[str, str] | None = None) -> str:
    unknown_values = unknown_values or {}
    lines = [
        "# ============================================",
        "# Swing Trader - Environment Configuration",
        "# Generated by scripts.setup_wizard",
        "# ============================================",
        "",
    ]

    for group in ENV_GROUPS:
        group_fields = [field for field in ENV_FIELDS if field.group == group["id"]]
        if not group_fields:
            continue
        lines.append(f"# --- {group['label']} ---")
        for field in group_fields:
            value = values.get(field.name, field.default)
            if value or field.required or field.default:
                lines.append(f"{field.name}={quote_env_value(value)}")
        lines.append("")

    extras = {
        key: value
        for key, value in unknown_values.items()
        if key not in FIELD_BY_NAME and value is not None
    }
    if extras:
        lines.append("# --- Existing custom values preserved ---")
        for key in sorted(extras):
            lines.append(f"{key}={quote_env_value(extras[key])}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def quote_env_value(value: str) -> str:
    value = "" if value is None else str(value)
    if not value:
        return ""
    needs_quotes = any(char.isspace() for char in value) or "#" in value or value.startswith(("'", '"'))
    if not needs_quotes:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def write_env_file(
    values: dict[str, str],
    path: str | Path = ".env",
    preserve_unknown: bool = True,
) -> None:
    env_path = Path(path)
    existing = read_env_file(env_path) if preserve_unknown else {}
    merged = default_env_values()
    merged.update({key: value for key, value in existing.items() if key in FIELD_BY_NAME})
    merged.update({key: value for key, value in values.items() if key in FIELD_BY_NAME})
    env_path.write_text(render_env_file(merged, existing if preserve_unknown else {}), encoding="utf-8")


def unknown_env_values(values: dict[str, str]) -> dict[str, str]:
    return {key: value for key, value in values.items() if key not in FIELD_BY_NAME}


def group_fields(group_id: str) -> Iterable[EnvField]:
    return (field for field in ENV_FIELDS if field.group == group_id)
