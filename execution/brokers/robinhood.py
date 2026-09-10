"""Robinhood Agentic Trading MCP broker adapter."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import timedelta
from typing import Any

from database.token_store import build_oauth_provider, is_configured as token_store_is_configured
from execution.brokers.base import (
    BrokerOrderRequest,
    BrokerOrderResult,
    BrokerOrderReview,
    OpenOrder,
)
from execution.brokers.capabilities import BrokerCapabilities
from portfolio.records import (
    AccountRecord,
    AccountSnapshot,
    BrokerSnapshot,
    CashRecord,
    HoldingRecord,
    OrderRecord,
    PendingSettlement,
    TaxLotRecord,
)
from portfolio.settlement import settlement_date
from utils.logger import get_logger
from utils.timeutils import utcnow_naive

log = get_logger("robinhood_broker")


class RobinhoodMCPError(RuntimeError):
    pass


#: What the Robinhood Agentic Trading MCP can actually do, verified from the
#: server's own ``tools/list`` schema on 2026-09-08 (Spec L §5.1). The dump is
#: not committed to this repository; re-run
#: ``python -m scripts.dump_robinhood_tool_schemas`` on the machine that holds
#: the token store to check any of these against the live server.
#:
#: ``can_place_attached_stop=False`` is the decisive entry and it is not a
#: placeholder: ``type`` is one of ``market``/``limit``/``stop_market``/
#: ``stop_limit`` and there is no bracket, OCO, OTO or attach-stop parameter
#: anywhere in the schema. A protective exit is a *separate* ``gtc``
#: ``stop_market`` order placed after the fill, which is
#: ``can_place_standalone_gtc_stop``.
#:
#: Declared statically rather than probed by attempting a write: the only way
#: to discover a placement capability empirically is to place something.
ROBINHOOD_CAPABILITIES = BrokerCapabilities(
    can_read_positions=True,
    can_read_tax_lots=True,
    can_read_orders=True,
    can_read_cash=True,
    can_place_equity_market=True,
    can_place_equity_limit=True,
    can_place_attached_stop=False,
    can_place_standalone_gtc_stop=True,
    can_place_bracket=False,
    supports_fractional=True,
    stops_regular_hours_only=True,
    stops_whole_shares_only=True,
    market_hours_only=False,
    supports_specified_lot_sale=True,
)

#: Read-only tools this adapter uses for the ledger. Listed so the sync path is
#: legible and so a reviewer can check it against Spec L §5.1 without reading
#: every method. No write tool appears here or is reachable from the sync.
LEDGER_READ_TOOLS = (
    "get_accounts",
    "get_portfolio",
    "get_equity_positions",
    "get_equity_tax_lots",
    "get_equity_orders",
    "get_option_positions",
    "get_realized_pnl",
    "get_pnl_trade_history",
)


class RobinhoodMCPBroker:
    name = "robinhood"
    supports_fractional = True
    supports_order_review = True
    live_trading = True
    #: The Strategy Lab venue this adapter *is*. Declared here so that a
    #: Strategy Lab paper arm cannot reach it even if someone registers it under
    #: the paper venue key — `strategy_lab.execution.bind_adapter` refuses on the
    #: declaration, not on the registration (Spec Q §11, §12 invariant 11).
    venue = "robinhood_live"

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

    # ------------------------------------------------------------------
    # The protective exit (Spec L §5.1). Two halves: place, then read back.
    # ------------------------------------------------------------------

    def place_stop(
        self,
        *,
        symbol: str,
        quantity: int,
        stop_price: float,
        ref_id: str,
        side: str = "sell",
    ) -> BrokerOrderResult:
        """The standalone ``gtc`` ``stop_market`` that protects a filled entry.

        Every parameter below is forced by the schema dumped from the server's
        own ``tools/list`` on 2026-09-08, not chosen: there is no bracket, OCO,
        OTO or attach-stop parameter anywhere in it, ``stop_market`` is
        regular-hours only, and ``stop_market`` is whole-share only. So a whole
        integer quantity is asserted here rather than rounded — a stop for 1.5
        shares of a 2-share position is not a protective exit, and rounding one
        into existence would hide the bug that produced it.

        ``ref_id`` is required, not defaulted: the fill-to-stop race is retried
        by the execution service, and a retry without the idempotency key is how
        one position acquires two stops.
        """
        if int(quantity) != float(quantity) or int(quantity) <= 0:
            raise RobinhoodMCPError(
                f"a protective stop is whole-share only; refusing quantity="
                f"{quantity!r} (Spec L §5.1)."
            )
        if not ref_id:
            raise RobinhoodMCPError(
                "a protective stop carries a ref_id: without the idempotency "
                "key a retried placement can create a second stop."
            )
        if float(stop_price) <= 0:
            raise RobinhoodMCPError(f"stop_price={stop_price!r} must be positive.")

        order = BrokerOrderRequest(
            symbol=symbol.upper(),
            side=side,
            order_type="stop_market",
            quantity=int(quantity),
            stop_price=float(stop_price),
            time_in_force="gtc",
            market_hours="regular_hours",
            client_context={"ref_id": ref_id, "purpose": "protective_exit"},
        )
        review = self.review_order(order)
        if not review.approved:
            return BrokerOrderResult(
                broker=self.name,
                success=False,
                error=" | ".join(review.errors) or "the broker did not approve the protective stop.",
                raw=review.raw,
            )
        return self.place_order(review)

    def read_open_orders(self, *, symbol: str | None = None) -> list[OpenOrder]:
        """Orders as the broker currently reports them, normalized.

        No status filter is passed upstream. The filter is applied here on
        :attr:`OpenOrder.is_open`, which treats an unrecognised state as *open*:
        a status string this adapter has never seen must not silently remove a
        stop from the protection check.
        """
        rows = self.get_orders()
        wanted = (symbol or "").strip().upper()
        out: list[OpenOrder] = []
        for raw in rows:
            row_symbol = (_first_string(raw, ("symbol", "ticker")) or "").upper()
            if wanted and row_symbol != wanted:
                continue
            out.append(
                OpenOrder(
                    broker=self.name,
                    order_id=_first_string(raw, ("order_id", "id")),
                    symbol=row_symbol,
                    side=_first_string(raw, ("side", "direction")),
                    order_type=_first_string(raw, ("type", "order_type")),
                    status=_first_string(raw, ("state", "status")),
                    quantity=_first_number(raw, ("quantity", "qty", "shares")),
                    stop_price=_first_number(raw, ("stop_price", "stop")),
                    limit_price=_first_number(raw, ("limit_price", "price")),
                    time_in_force=_first_string(raw, ("time_in_force", "tif")),
                    ref_id=_order_ref_id(raw),
                    raw=raw if isinstance(raw, dict) else {},
                )
            )
        return out

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

    # ------------------------------------------------------------------
    # Ledger read paths (Spec L §4). Read-only, across every account the token
    # can see. Nothing below reaches a placement, review, or cancel tool.
    # ------------------------------------------------------------------

    def capabilities(self) -> BrokerCapabilities:
        """The declared capability set, recorded on ``brokerage_accounts``."""
        return ROBINHOOD_CAPABILITIES

    def get_tax_lots(self, account_number: str | None = None) -> list[dict]:
        account = account_number or self.account_number
        if not account:
            return []
        raw = self._call_tool_sync("get_equity_tax_lots", {"account_number": account})
        return _extract_list(raw, preferred_keys=("tax_lots", "lots", "results"))

    def get_option_positions(self, account_number: str | None = None) -> list[dict]:
        """Option positions, read so they are never silently omitted.

        Options are out of scope for analysis and firmly in scope for *display*:
        a portfolio view that drops a short put is the worst failure this ledger
        has. Read only — the option write tools are never referenced anywhere in
        this repository (``test_no_option_or_crypto_write_tools``).
        """
        account = account_number or self.account_number
        if not account:
            return []
        raw = self._call_tool_sync("get_option_positions", {"account_number": account})
        return _extract_list(raw, preferred_keys=("positions", "option_positions", "results"))

    def get_realized_pnl(self, account_number: str | None = None) -> list[dict]:
        account = account_number or self.account_number
        if not account:
            return []
        raw = self._call_tool_sync("get_realized_pnl", {"account_number": account})
        return _extract_list(raw, preferred_keys=("realized_pnl", "results", "rows"))

    def get_pnl_trade_history(self, account_number: str | None = None) -> list[dict]:
        account = account_number or self.account_number
        if not account:
            return []
        raw = self._call_tool_sync("get_pnl_trade_history", {"account_number": account})
        return _extract_list(raw, preferred_keys=("trades", "results", "history"))

    def fetch_ledger_snapshot(self) -> BrokerSnapshot:
        """Every account the token can read, normalized for the portfolio sync.

        One account failing does not fail the run: its :class:`AccountSnapshot`
        carries ``error`` and the sync leaves that account's previous rows
        intact rather than treating an unreadable account as an empty one.
        """
        as_of = utcnow_naive()
        try:
            accounts = self.get_accounts()
        except Exception as exc:
            return BrokerSnapshot(broker=self.name, error=f"get_accounts failed: {exc}")

        snapshots: list[AccountSnapshot] = []
        for raw_account in accounts:
            number = _account_number_of(raw_account)
            if not number:
                continue
            record = _account_record(self.name, raw_account, number)
            try:
                snapshots.append(self._account_snapshot(record, number, as_of))
            except Exception as exc:
                log.error("robinhood_account_read_failed", account=_mask_account(number), error=str(exc))
                snapshots.append(
                    AccountSnapshot(
                        account=record,
                        as_of_utc=as_of,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
        return BrokerSnapshot(broker=self.name, accounts=tuple(snapshots))

    def _account_snapshot(self, record: AccountRecord, number: str, as_of) -> AccountSnapshot:
        equities = _extract_list(
            self._call_tool_sync("get_equity_positions", {"account_number": number}),
            preferred_keys=("positions", "results", "equity_positions"),
        )
        holdings = [_equity_holding(row) for row in equities]
        holdings = [h for h in holdings if h is not None]

        # Options are read separately and appended with `instrument_type` set.
        # A failure here must not silently drop them: it raises, and the sync
        # marks the account stale rather than writing a book without its
        # options in it.
        for row in self.get_option_positions(number):
            option = _option_holding(row)
            if option is not None:
                holdings.append(option)

        lots = [_tax_lot(row, record.booking_method) for row in self.get_tax_lots(number)]
        lots = [lot for lot in lots if lot is not None]

        portfolio = _select_account_object(
            self._call_tool_sync("get_portfolio", {"account_number": number}), number
        )
        cash = _cash_record(portfolio, self.get_pnl_trade_history(number))

        orders = [
            _order_record(row)
            for row in _extract_list(
                self._call_tool_sync("get_equity_orders", {"account_number": number}),
                preferred_keys=("orders", "results"),
            )
        ]
        orders = [order for order in orders if order is not None]

        return AccountSnapshot(
            account=record,
            as_of_utc=as_of,
            holdings=tuple(holdings),
            tax_lots=tuple(lots),
            cash=cash,
            orders=tuple(orders),
        )

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


# ---------------------------------------------------------------------------
# Ledger normalizers. Free functions so the recorded fixtures can be replayed
# through exactly the code the live adapter uses (tests/fixtures/robinhood/).
# ---------------------------------------------------------------------------

#: Robinhood's own flag for "the agent may place here" (Spec L §5.1). The API
#: rejects a placement against any other account, so the boundary is enforced
#: upstream as well as here.
AGENTIC_KEYS = ("agentic_allowed", "is_agentic", "agentic", "agent_placeable")

#: Robinhood's account-type vocabulary mapped onto Spec L §3's three values.
#: The Agentic account is a **cash** account by owner decision, so an unknown
#: or missing type resolves to `cash`: the constrained reading is the safe one
#: — it keeps T+1 settlement modelled rather than assuming margin.
_ACCOUNT_TYPE_MAP = {
    "cash": "cash",
    "margin": "margin",
    "instant": "margin",
    "gold": "margin",
    "ira": "ira",
    "roth_ira": "ira",
    "traditional_ira": "ira",
    "retirement": "ira",
}


def _account_number_of(raw: dict) -> str:
    return _first_string(raw, ("account_number", "account_id", "number", "id"))


def _account_type_of(raw: dict) -> str:
    value = str(_first_string(raw, ("type", "account_type", "brokerage_account_type")) or "").lower()
    for key, mapped in _ACCOUNT_TYPE_MAP.items():
        if key in value:
            return mapped
    return "cash"


def _account_record(broker: str, raw: dict, number: str) -> AccountRecord:
    """One ``get_accounts`` row, with the account number stored **masked**.

    The full number is a credential-like identifier that the ledger has no use
    for: it is needed to call the API and nowhere else, so it never reaches a
    row, a log line, or a fixture.
    """
    placeable = _truthy(_first_present(raw, AGENTIC_KEYS), default=False)
    account_type = _account_type_of(raw)
    return AccountRecord(
        broker=broker,
        external_account_id=_mask_account(number),
        label=_first_string(raw, ("nickname", "name", "label")) or _mask_account(number),
        account_type=account_type,
        currency=_first_string(raw, ("currency", "currency_code")) or "USD",
        # Specified-lot selling is available on plain sells, so a discretionary
        # sell can book STRICT; protective exits sell FIFO and the ledger
        # records which was used per lot (Spec L §5.1).
        booking_method="STRICT",
        agent_placeable=placeable,
        enabled=True,
    )


def _equity_holding(raw: dict) -> HoldingRecord | None:
    symbol = str(raw.get("symbol") or raw.get("ticker") or "").upper()
    if not symbol:
        return None
    quantity = _to_float(raw.get("quantity") or raw.get("qty") or raw.get("shares")) or 0.0
    average_cost = _to_float(_first_present(raw, ("average_cost", "average_price", "avg_entry_price")))
    price = _to_float(_first_present(raw, ("current_price", "last_trade_price", "price")))
    market_value = _to_float(raw.get("market_value"))
    if market_value is None and price is not None:
        market_value = quantity * price
    # Basis is null when the broker did not supply one. Multiplying a null
    # average cost by a quantity to get a zero basis is the exact failure
    # `test_unknown_basis_is_null_not_zero` exists for.
    cost_basis = _to_float(raw.get("cost_basis"))
    if cost_basis is None and average_cost is not None:
        cost_basis = average_cost * quantity
    return HoldingRecord(
        symbol=symbol,
        quantity=quantity,
        instrument_type="equity",
        average_cost=average_cost,
        cost_basis=cost_basis,
        last_price=price,
        market_value=market_value,
        notional=market_value,
        currency=_first_string(raw, ("currency", "currency_code")) or "USD",
        source="robinhood:get_equity_positions",
    )


def _option_holding(raw: dict) -> HoldingRecord | None:
    """An option position, stored with its notional and never dropped.

    Notional is the *contract* exposure — quantity x multiplier x strike where
    the multiplier is known — because the market value of a short put says
    almost nothing about what it commits the account to. Where the pieces are
    missing the market value is used and the detail travels with the row.
    """
    symbol = str(
        raw.get("chain_symbol") or raw.get("underlying_symbol") or raw.get("symbol") or ""
    ).upper()
    if not symbol:
        return None
    quantity = _to_float(raw.get("quantity") or raw.get("qty")) or 0.0
    direction = str(raw.get("position_type") or raw.get("side") or "long").lower()
    signed_quantity = -abs(quantity) if direction in {"short", "sell"} else quantity
    multiplier = _to_float(raw.get("trade_value_multiplier") or raw.get("multiplier")) or 100.0
    strike = _to_float(raw.get("strike_price") or raw.get("strike"))
    market_value = _to_float(raw.get("market_value"))
    notional = signed_quantity * multiplier * strike if strike is not None else market_value
    detail = {
        "option_type": _first_string(raw, ("option_type", "type", "right")),
        "strike_price": strike,
        "expiration_date": _first_string(raw, ("expiration_date", "expiry", "expires_at")),
        "position_type": direction,
        "multiplier": multiplier,
    }
    return HoldingRecord(
        symbol=symbol,
        quantity=signed_quantity,
        instrument_type="option",
        average_cost=_to_float(raw.get("average_price") or raw.get("average_open_price")),
        cost_basis=None,
        last_price=_to_float(raw.get("current_price") or raw.get("last_price")),
        market_value=market_value,
        notional=notional,
        currency="USD",
        instrument_detail={k: v for k, v in detail.items() if v not in (None, "")},
        source="robinhood:get_option_positions",
    )


def _tax_lot(raw: dict, booking_method: str) -> TaxLotRecord | None:
    symbol = str(raw.get("symbol") or raw.get("ticker") or "").upper()
    if not symbol:
        return None
    return TaxLotRecord(
        symbol=symbol,
        quantity=_to_float(raw.get("quantity") or raw.get("shares")) or 0.0,
        broker_lot_id=_first_string(raw, ("id", "lot_id", "tax_lot_id")) or None,
        open_date=_to_date(_first_present(raw, ("open_date", "acquired_date", "purchase_date"))),
        cost_basis=_to_float(_first_present(raw, ("cost_basis", "total_cost", "basis"))),
        term=_lot_term(raw),
        booking_method=booking_method,
        source="robinhood:get_equity_tax_lots",
    )


def _lot_term(raw: dict) -> str:
    value = str(_first_string(raw, ("term", "holding_period", "term_type")) or "").lower()
    if "short" in value:
        return "short"
    if "long" in value:
        return "long"
    # Never inferred from the open date: the holding-period rules have
    # exceptions this ledger does not model, and a wrong `long` is worse than
    # an honest `unknown`.
    return "unknown"


def _cash_record(portfolio: dict, trade_history) -> CashRecord:
    """Cash split into settled and unsettled, with the T+1 tranches.

    Robinhood exposes no cash-movement tool, so the pending tranches are
    reconstructed from recent sells in ``get_pnl_trade_history``: a sale on
    trade date T frees its proceeds on the next trading day. Each tranche is
    labelled with that derivation so nothing downstream mistakes it for a
    broker-reported settlement schedule.
    """
    settled = _to_float(_first_present(portfolio, ("settled_cash", "cash_available_for_withdrawal", "withdrawable_cash")))
    total_cash = _to_float(_first_present(portfolio, ("cash", "cash_balance", "buying_power")))
    unsettled = _to_float(_first_present(portfolio, ("unsettled_funds", "unsettled_cash")))
    if settled is None and total_cash is not None and unsettled is not None:
        settled = total_cash - unsettled
    elif settled is None:
        settled = total_cash

    pending: list[PendingSettlement] = []
    for row in trade_history or []:
        if str(row.get("side") or "").lower() not in {"sell", "sold"}:
            continue
        trade_date = _to_date(_first_present(row, ("date", "trade_date", "executed_at", "settled_at")))
        amount = _to_float(_first_present(row, ("net_amount", "proceeds", "amount", "notional")))
        if trade_date is None or amount is None:
            continue
        pending.append(
            PendingSettlement(
                amount=abs(amount),
                settles_on=settlement_date(trade_date),
                source="robinhood:get_pnl_trade_history (derived: T+1 from trade date)",
            )
        )

    return CashRecord(
        settled_cash=settled,
        unsettled_cash=unsettled,
        buying_power=_to_float(_first_present(portfolio, ("buying_power", "cash"))),
        currency="USD",
        pending_settlements=tuple(pending),
        source="robinhood:get_portfolio",
    )


def _order_record(raw: dict) -> OrderRecord | None:
    order_id = _first_string(raw, ("id", "order_id", "equity_order_id"))
    symbol = str(raw.get("symbol") or raw.get("ticker") or "").upper()
    if not order_id or not symbol:
        return None
    filled_quantity = _to_float(_first_present(raw, ("filled_quantity", "cumulative_quantity")))
    return OrderRecord(
        broker_order_id=order_id,
        symbol=symbol,
        side=str(raw.get("side") or "").lower(),
        quantity=_to_float(raw.get("quantity")),
        order_type=str(raw.get("type") or raw.get("order_type") or "").lower(),
        time_in_force=str(raw.get("time_in_force") or "").lower(),
        limit_price=_to_float(raw.get("limit_price") or raw.get("price")),
        stop_price=_to_float(raw.get("stop_price")),
        status=str(_first_string(raw, ("state", "status")) or "").lower(),
        submitted_at=_to_datetime(_first_present(raw, ("created_at", "submitted_at", "placed_at"))),
        filled_at=_to_datetime(_first_present(raw, ("last_transaction_at", "filled_at", "updated_at"))),
        filled_quantity=filled_quantity,
        average_fill_price=_to_float(_first_present(raw, ("average_price", "avg_price"))),
        ref_id=_order_ref_id(raw) or None,
        # Everything a *read* finds is external until the execution service
        # says otherwise. Phase 1 has no execution service, so every row is
        # external, which is exactly `test_external_orders_ingested`.
        origin="external",
        raw=raw,
        source="robinhood:get_equity_orders",
    )


def _to_date(value: Any):
    from datetime import date as _date, datetime as _datetime

    if value is None or value == "":
        return None
    if isinstance(value, _datetime):
        return value.date()
    if isinstance(value, _date):
        return value
    text = str(value)
    try:
        return _date.fromisoformat(text[:10])
    except ValueError:
        return None


def _to_datetime(value: Any):
    from datetime import datetime as _datetime, timezone as _timezone

    if value is None or value == "":
        return None
    if isinstance(value, _datetime):
        parsed = value
    else:
        text = str(value).replace("Z", "+00:00")
        try:
            parsed = _datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(_timezone.utc).replace(tzinfo=None)
    return parsed
