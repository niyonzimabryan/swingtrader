"""The SwingTrader investment workspace service (Spec K).

A **separate process** from the trading bot: one FastAPI app exposing
``/health``, a ``/v1`` REST namespace, and the official MCP SDK's
streamable-HTTP endpoint at ``/mcp``. The bot never imports this package, and
this package never imports ``execution`` or ``bot`` — an import-graph test
(``tests/test_no_execute_scope.py``) keeps both true, because the guarantee
"no agent can place an order" is worth more as a structural property than as a
convention.

Everything here is behind ``WORKSPACE_API_ENABLED``, which defaults to false.
"""
