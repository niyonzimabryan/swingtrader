"""Robinhood Agentic Trading MCP broker adapter."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

from database.token_store import build_oauth_provider, is_configured as token_store_is_configured
from execution.brokers.base import BrokerOrderRequest, BrokerOrderResult, BrokerOrderReview
from utils.logger import get_logger

log = get_logger("robinhood_broker")


class RobinhoodMCPError(RuntimeError):
    pass


class RobinhoodMCPBroker:
    name = "robinhood"
    supports_fractional = True
    supports_order_review = True
    live_trading = True

    def __init__(self, settings):
        self.settings = settings
        self.url = getattr(settings, "robinhood_mcp_url", "https://agent.robinhood.com/mcp/trading")
        self.account_number = getattr(settings, "robinhood_account_number", "")
        self._tools_cache: dict[str, dict] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.url)

    def get_accounts(self) -> list[dict]:
        raw = self._call_tool_sync("get_accounts", {})
        return _extract_list(raw, preferred_keys=("accounts", "results"))

    def get_account_info(self) -> dict:
        if not self.account_number:
            return {
                "broker": self.name,
                "configured": False,
                "error": "ROBINHOOD_ACCOUNT_NUMBER is not set.",
            }
        raw = self._call_tool_sync("get_portfolio", {"account_number": self.account_number})
        portfolio = _select_account_object(raw, self.account_number)
        return {
            "broker": self.name,
            "account_number": _mask_account(self.account_number),
            "equity": _first_number(portfolio, ("equity", "portfolio_value", "market_value", "total_value")) or 0.0,
            "cash": _first_number(portfolio, ("cash", "cash_available", "buying_power")) or 0.0,
            "buying_power": _first_number(portfolio, ("buying_power", "withdrawable_cash", "cash")) or 0.0,
            "portfolio_value": _first_number(portfolio, ("portfolio_value", "market_value", "total_value", "equity")) or 0.0,
            "pnl_today": _first_number(portfolio, ("pnl_today", "todays_pnl", "day_pnl")) or 0.0,
            "pnl_today_pct": _first_number(portfolio, ("pnl_today_pct", "todays_pnl_pct", "day_pnl_pct")) or 0.0,
            "raw": raw,
        }

    def get_positions_detail(self) -> list[dict]:
        if not self.account_number:
            return []
        raw = self._call_tool_sync("get_equity_positions", {"account_number": self.account_number})
        positions = _extract_list(raw, preferred_keys=("positions", "results", "equity_positions"))
        normalized = []
        for pos in positions:
            symbol = str(pos.get("symbol") or pos.get("ticker") or "").upper()
            if not symbol:
                continue
            qty = _to_float(pos.get("quantity") or pos.get("qty") or pos.get("shares"))
            avg_price = _to_float(pos.get("average_cost") or pos.get("avg_entry_price") or pos.get("average_price"))
            current = _to_float(pos.get("current_price") or pos.get("last_trade_price") or pos.get("price"))
            market_value = _to_float(pos.get("market_value")) or (qty * current if current else 0.0)
            pnl_abs = _to_float(pos.get("unrealized_pl") or pos.get("pnl_abs") or pos.get("unrealized_gain_loss"))
            pnl_pct = _to_float(pos.get("unrealized_plpc") or pos.get("pnl_pct") or pos.get("unrealized_gain_loss_pct"))
            if pnl_pct and abs(pnl_pct) <= 1:
                pnl_pct *= 100
            normalized.append(
                {
                    "ticker": symbol,
                    "qty": qty,
                    "entry_price": avg_price,
                    "current_price": current,
                    "market_value": market_value,
                    "pnl_abs": pnl_abs,
                    "pnl_pct": pnl_pct,
                    "side": pos.get("side", "long"),
                    "raw": pos,
                }
            )
        return normalized

    def get_orders(self, status: str | None = None) -> list[dict]:
        if not self.account_number:
            return []
        args = {"account_number": self.account_number}
        if status:
            args["state"] = status
        raw = self._call_tool_sync("get_equity_orders", args)
        return _extract_list(raw, preferred_keys=("orders", "results"))

    def get_quotes(self, symbols: list[str]) -> dict[str, dict]:
        raw = self._call_tool_sync("get_equity_quotes", {"symbols": [s.upper() for s in symbols]})
        quotes = _extract_list(raw, preferred_keys=("quotes", "results"))
        out = {}
        for quote in quotes:
            symbol = str(quote.get("symbol") or "").upper()
            if symbol:
                out[symbol] = quote
        return out

    def get_tradability(self, symbol: str) -> dict:
        if not self.account_number:
            return {"symbol": symbol, "tradable": False, "error": "ROBINHOOD_ACCOUNT_NUMBER is not set."}
        raw = self._call_tool_sync(
            "get_equity_tradability",
            {"account_number": self.account_number, "symbols": [symbol.upper()]},
        )
        rows = _extract_list(raw, preferred_keys=("results", "tradability", "instruments"))
        row = rows[0] if rows else raw
        return {
            "symbol": symbol.upper(),
            "tradable": _truthy(_first_present(row, ("tradable", "is_tradable", "can_trade")), default=True),
            "fractional": _truthy(
                _first_present(row, ("fractional", "fractional_trading", "supports_fractional")),
                default=False,
            ),
            "raw": raw,
        }

    def review_order(self, order: BrokerOrderRequest) -> BrokerOrderReview:
        warnings = []
        errors = []
        tradability = self.get_tradability(order.symbol)
        if not tradability.get("tradable", False):
            errors.append(f"{order.symbol} is not tradable in the selected Robinhood account.")
        if order.dollar_amount and not tradability.get("fractional", False):
            errors.append(f"{order.symbol} does not allow fractional/dollar orders in this account.")

        if errors:
            return BrokerOrderReview(
                broker=self.name,
                request=order,
                approved=False,
                warnings=warnings,
                errors=errors,
                raw={"tradability": tradability},
            )

        args = self._equity_order_args(order)
        raw = self._call_tool_sync("review_equity_order", args)
        warnings.extend(_extract_messages(raw, ("alerts", "warnings", "messages")))
        errors.extend(_extract_messages(raw, ("errors", "error")))
        approved = not errors
        return BrokerOrderReview(
            broker=self.name,
            request=order,
            approved=approved,
            warnings=warnings,
            errors=errors,
            estimated_notional=_first_number(raw, ("estimated_cost", "estimated_notional", "total", "notional"))
            or order.requested_notional,
            raw={"review": raw, "tradability": tradability},
        )

    def place_order(self, reviewed_order: BrokerOrderReview) -> BrokerOrderResult:
        if not bool(getattr(self.settings, "allow_live_trading", False)):
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error="Live trading is disabled. Set ALLOW_LIVE_TRADING=true and /mode live first.",
            )
        if not reviewed_order.approved:
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error="Robinhood order review did not approve this order.",
                raw=reviewed_order.raw,
            )
        args = self._equity_order_args(reviewed_order.request)
        args["ref_id"] = reviewed_order.request.client_context.get("ref_id") or str(uuid.uuid4())
        raw = self._call_tool_sync("place_equity_order", args)
        order_id = _first_string(raw, ("order_id", "id", "equity_order_id"))
        status = _first_string(raw, ("state", "status")) or "submitted"
        filled_qty = _first_number(raw, ("filled_quantity", "filled_qty", "quantity_filled"))
        filled_avg_price = _first_number(raw, ("average_price", "filled_avg_price", "avg_price"))
        filled_notional = _first_number(raw, ("filled_notional", "filled_amount"))
        if filled_notional is None and filled_qty and filled_avg_price:
            filled_notional = filled_qty * filled_avg_price
        return BrokerOrderResult(
            broker=self.name,
            success=True,
            order_id=order_id,
            status=status,
            order_strategy="robinhood_mcp",
            filled_qty=filled_qty,
            filled_avg_price=filled_avg_price,
            filled_notional=filled_notional,
            raw=raw,
        )

    def find_order_by_ref_id(self, ref_id: str) -> dict | None:
        """Best-effort reconciliation for ambiguous placement failures."""
        if not ref_id:
            return None
        for status in (None, "queued", "open", "filled", "cancelled"):
            try:
                orders = self.get_orders(status=status)
            except Exception:
                continue
            for order in orders:
                if _order_ref_id(order) == ref_id:
                    return order
        return None

    def get_order_status(self, order_id: str) -> dict:
        if not self.account_number:
            return {}
        raw = self._call_tool_sync(
            "get_equity_orders",
            {"account_number": self.account_number, "order_id": order_id},
        )
        orders = _extract_list(raw, preferred_keys=("orders", "results"))
        order = orders[0] if orders else raw
        filled_qty = _first_number(order, ("filled_quantity", "filled_qty", "quantity_filled", "cumulative_quantity")) or 0.0
        filled_avg_price = _first_number(order, ("average_price", "filled_avg_price", "avg_price")) or 0.0
        filled_notional = _first_number(order, ("filled_notional", "filled_amount"))
        if filled_notional is None and filled_qty and filled_avg_price:
            filled_notional = filled_qty * filled_avg_price
        return {
            "id": _first_string(order, ("order_id", "id")) or order_id,
            "status": _first_string(order, ("state", "status")) or "",
            "filled_qty": filled_qty,
            "filled_avg_price": filled_avg_price,
            "filled_notional": filled_notional,
            "symbol": _first_string(order, ("symbol", "ticker")) or "",
            "raw": order,
        }

    def cancel_order(self, order_id: str):
        if not self.account_number:
            return {"success": False, "error": "ROBINHOOD_ACCOUNT_NUMBER is not set."}
        raw = self._call_tool_sync(
            "cancel_equity_order",
            {"account_number": self.account_number, "order_id": order_id},
        )
        return {"success": True, "raw": raw}

    def close_position(self, ticker: str) -> dict:
        if not bool(getattr(self.settings, "allow_live_trading", False)):
            return {"success": False, "error": "Live trading is disabled."}
        positions = self.get_positions_detail()
        pos = next((p for p in positions if p.get("ticker") == ticker.upper()), None)
        if not pos:
            return {"success": False, "error": f"position not found for ticker {ticker.upper()}"}
        qty = pos.get("qty", 0)
        order = BrokerOrderRequest(
            symbol=ticker.upper(),
            side="sell",
            order_type="market",
            quantity=qty,
            market_hours="regular_hours",
            time_in_force="gfd",
            requested_notional=pos.get("market_value", 0),
        )
        review = self.review_order(order)
        if not review.approved:
            return {"success": False, "error": " | ".join(review.errors), "review": review.raw}
        result = self.place_order(review)
        return {"success": result.success, "order_id": result.order_id, "error": result.error, "raw": result.raw}

    def submit_limit_sell(self, ticker: str, qty: int, price: float) -> str:
        order = BrokerOrderRequest(
            symbol=ticker.upper(),
            side="sell",
            order_type="limit",
            quantity=qty,
            limit_price=price,
            time_in_force="gtc",
            requested_notional=qty * price,
        )
        review = self.review_order(order)
        if not review.approved:
            raise RobinhoodMCPError(" | ".join(review.errors))
        result = self.place_order(review)
        if not result.success:
            raise RobinhoodMCPError(result.error)
        return result.order_id

    def submit_limit_cover(self, ticker: str, qty: int, price: float) -> str:
        return self.submit_limit_sell(ticker, qty, price)

    def _equity_order_args(self, order: BrokerOrderRequest) -> dict:
        args = {
            "account_number": self.account_number,
            "symbol": order.symbol.upper(),
            "side": order.side,
            "type": order.order_type,
            "time_in_force": order.time_in_force or "gfd",
            "market_hours": order.market_hours or "regular_hours",
        }
        if order.quantity is not None:
            args["quantity"] = _format_decimal(order.quantity)
        if order.dollar_amount is not None:
            args["dollar_amount"] = _format_money(order.dollar_amount)
        if order.limit_price is not None:
            args["limit_price"] = _format_money(order.limit_price)
        if order.stop_price is not None:
            args["stop_price"] = _format_money(order.stop_price)
        return args

    def _call_tool_sync(self, name: str, arguments: dict) -> dict:
        try:
            return asyncio.run(self._call_tool(name, arguments))
        except ImportError as exc:
            raise RobinhoodMCPError("Install the MCP SDK with `pip install mcp`.") from exc
        except RobinhoodMCPError:
            raise
        except Exception as exc:
            log.error("robinhood_mcp_call_failed", tool=name, error=str(exc))
            raise RobinhoodMCPError(f"Robinhood MCP {name} failed: {exc}") from exc

    async def _call_tool(self, name: str, arguments: dict) -> dict:
        from mcp import ClientSession, types
        from mcp.client.streamable_http import streamablehttp_client

        auth = self._oauth_provider()
        # When OAuth storage is configured, let the MCP SDK own the Authorization
        # header and refresh flow. Static env headers remain supported for service
        # deployments that intentionally manage auth outside this process.
        headers = self._headers(include_auth_token=auth is None)
        async with streamablehttp_client(
            self.url,
            headers=headers,
            timeout=45,
            sse_read_timeout=45,
            auth=auth,
        ) as (read_stream, write_stream, _):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(
                    name,
                    arguments=arguments,
                    read_timeout_seconds=timedelta(seconds=45),
                )
        payload: dict[str, Any] = {}
        structured = getattr(result, "structuredContent", None)
        if structured:
            payload["structured"] = structured
            if isinstance(structured, dict):
                payload.update(structured)
        texts = []
        for item in getattr(result, "content", []) or []:
            if isinstance(item, types.TextContent):
                texts.append(item.text)
        if texts:
            payload["text"] = "\n".join(texts)
            parsed = _parse_json_text(payload["text"])
            if isinstance(parsed, dict):
                payload.update(parsed)
            elif isinstance(parsed, list):
                payload["results"] = parsed
        if getattr(result, "isError", False):
            raise RobinhoodMCPError(payload.get("text") or json.dumps(payload)[:500])
        return payload

    def _oauth_provider(self):
        if not token_store_is_configured(self.settings):
            return None

        async def redirect_handler(auth_url: str) -> None:
            raise RobinhoodMCPError(
                "Robinhood OAuth needs re-authentication. Run "
                "`python -m scripts.robinhood_auth --status`, then "
                "`python -m scripts.robinhood_auth` on a desktop browser. "
                f"Authorization URL from MCP: {auth_url}"
            )

        async def callback_handler() -> tuple[str, str | None]:
            raise RobinhoodMCPError(
                "Robinhood OAuth callback cannot be completed inside the bot process. "
                "Run `python -m scripts.robinhood_auth` to refresh the encrypted token store."
            )

        provider, _storage = build_oauth_provider(
            self.settings,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
            timeout=45,
        )
        return provider

    def token_store_status(self) -> dict:
        if not token_store_is_configured(self.settings):
            return {"configured": False, "reason": "TOKEN_ENCRYPTION_KEY is not set"}
        _provider, storage = build_oauth_provider(self.settings, timeout=45)
        status = storage.status()
        status["configured"] = True
        return status

    def _headers(self, *, include_auth_token: bool = True) -> dict[str, str]:
        headers: dict[str, str] = {}
        token = getattr(self.settings, "robinhood_mcp_auth_token", "")
        if token and include_auth_token:
            headers["Authorization"] = f"Bearer {token}"
        raw_headers = getattr(self.settings, "robinhood_mcp_headers_json", "")
        if raw_headers:
            try:
                headers.update(json.loads(raw_headers))
            except json.JSONDecodeError:
                log.warning("invalid_robinhood_headers_json")
        return headers


def _extract_list(raw: Any, preferred_keys: tuple[str, ...]) -> list[dict]:
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if not isinstance(raw, dict):
        return []
    for key in preferred_keys:
        value = raw.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            nested = _extract_list(value, preferred_keys)
            if nested:
                return nested
    for value in raw.values():
        if isinstance(value, list) and all(isinstance(item, dict) for item in value):
            return value
    return []


def _select_account_object(raw: Any, account_number: str) -> dict:
    if not isinstance(raw, dict):
        return {}

    candidates: list[dict] = []
    for key in ("portfolio", "account", "account_portfolio", "result", "data", "structured"):
        value = raw.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for key in ("portfolios", "accounts", "results"):
        value = raw.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))

    for candidate in candidates:
        if _matches_account(candidate, account_number):
            return candidate
    for candidate in candidates:
        if any(key in candidate for key in ("equity", "portfolio_value", "market_value", "cash", "buying_power")):
            return candidate
    return raw


def _matches_account(raw: dict, account_number: str) -> bool:
    if not account_number:
        return False
    acct = str(
        raw.get("account_number")
        or raw.get("account_id")
        or raw.get("account")
        or raw.get("number")
        or raw.get("id")
        or ""
    )
    return acct == account_number or acct.endswith(account_number[-4:])


def _parse_json_text(text: str) -> Any:
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _first_number(raw: Any, keys: tuple[str, ...]) -> float | None:
    if isinstance(raw, dict):
        for key in keys:
            if key in raw:
                value = _to_float(raw[key])
                if value is not None:
                    return value
        for value in raw.values():
            found = _first_number(value, keys)
            if found is not None:
                return found
    elif isinstance(raw, list):
        for item in raw:
            found = _first_number(item, keys)
            if found is not None:
                return found
    return None


def _first_string(raw: Any, keys: tuple[str, ...]) -> str:
    if isinstance(raw, dict):
        for key in keys:
            value = raw.get(key)
            if value is not None and value != "":
                return str(value)
        for value in raw.values():
            found = _first_string(value, keys)
            if found:
                return found
    elif isinstance(raw, list):
        for item in raw:
            found = _first_string(item, keys)
            if found:
                return found
    return ""


def _order_ref_id(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    for key in ("ref_id", "client_order_id", "client_id", "client_ref_id"):
        value = raw.get(key)
        if value:
            return str(value)
    nested = raw.get("order") or raw.get("equity_order")
    if isinstance(nested, dict):
        return _order_ref_id(nested)
    return ""


def _extract_messages(raw: dict, keys: tuple[str, ...]) -> list[str]:
    messages: list[str] = []
    for key in keys:
        value = raw.get(key)
        if isinstance(value, str):
            messages.append(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    messages.append(item)
                elif isinstance(item, dict):
                    msg = item.get("message") or item.get("detail") or item.get("text")
                    if msg:
                        messages.append(str(msg))
    return messages


def _truthy(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "enabled", "eligible", "tradable"}
    return bool(value)


def _first_present(raw: dict, keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in raw:
            return raw[key]
    return None


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return None


def _format_money(value: float) -> str:
    return f"{float(value):.2f}"


def _format_decimal(value: float) -> str:
    return f"{float(value):.6f}".rstrip("0").rstrip(".")


def _mask_account(account_number: str) -> str:
    if len(account_number) <= 4:
        return "****"
    return f"****{account_number[-4:]}"
