"""The process-level home for the services the runtime shares.

Until now the long-lived objects `main.py` builds — the Phase 6
:class:`execution.lifecycle.ExecutionService`, the Strategy Lab
:class:`execution.strategy_lifecycle.StrategyExecutionService`, and the
venue → adapter map the two of them route on — lived in
``telegram.ext.Application.bot_data``. That was convenient rather than
intentional: the Telegram handlers needed them, ``bot_data`` was the dict the
handlers already had, and nothing else in the process wanted them.

Something else does now. With ``TELEGRAM_ENABLED=false`` there is no
``Application``, so there is no ``bot_data`` — and the owner approval poller
(Spec K §10) still has to reach exactly those three objects. So they live here,
in a plain container that knows nothing about any channel, and ``bot_data``
becomes a *view* of it rather than its home: ``main.py`` copies
:meth:`RuntimeContainer.as_bot_data` into ``app.bot_data`` when Telegram is on,
which is why every handler under ``bot/handlers/`` keeps working with no change
at all.

**Why ``orchestrator/`` and not ``portfolio/``.** The container holds a
reference to the execution services, so whichever package it lives in becomes a
package from which ``execution/`` is reachable. ``orchestrator`` is already one
of the four roots the workspace's import closure may never touch
(``tests/test_no_execute_scope.py``'s ``FORBIDDEN_ROOTS``), so putting it here
adds no edge to the import graph the workspace can see. Putting it in
``portfolio/`` — the one package both sides may import — would have.

The container holds references and nothing else: no lifecycle, no starting or
stopping, no lazily-built services. `main.py` owns construction and shutdown,
and this is the shelf it puts things on.
"""

from __future__ import annotations

#: The keys `main.py` used to write straight into ``app.bot_data``, and which
#: ``bot/handlers/`` still reads by name. Stable strings: a handler looks each
#: one up with ``context.bot_data.get(...)``.
EXECUTION_SERVICE = "execution_service"
STRATEGY_EXECUTION_SERVICE = "strategy_execution_service"
STRATEGY_LAB_ADAPTERS = "strategy_lab_adapters"

#: The container itself, also published into ``bot_data`` so a future handler
#: can reach the whole thing rather than a copied key.
RUNTIME = "runtime"


class RuntimeContainer:
    """Whatever the running process needs to hand to more than one caller.

    Deliberately dict-shaped as well as attribute-shaped. The attributes are for
    ``main.py`` and the poller, which know exactly what they want; the mapping
    protocol is for :meth:`as_bot_data` and for anything that wants to treat it
    the way ``bot_data`` was treated.
    """

    __slots__ = ("_data",)

    def __init__(self, **initial):
        self._data: dict = {}
        for key, value in initial.items():
            if value is not None:
                self._data[key] = value

    # -- mapping ------------------------------------------------------------ #

    def __getitem__(self, key: str):
        return self._data[key]

    def __setitem__(self, key: str, value) -> None:
        self._data[key] = value

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        return iter(self._data)

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    def keys(self):
        return self._data.keys()

    def items(self):
        return self._data.items()

    # -- the named services -------------------------------------------------- #

    @property
    def execution_service(self):
        """Phase 6's proposal → approval → placement service, or ``None``."""
        return self._data.get(EXECUTION_SERVICE)

    @execution_service.setter
    def execution_service(self, value) -> None:
        self._data[EXECUTION_SERVICE] = value

    @property
    def strategy_execution_service(self):
        """The Strategy Lab's own service. A *second* service, deliberately."""
        return self._data.get(STRATEGY_EXECUTION_SERVICE)

    @strategy_execution_service.setter
    def strategy_execution_service(self, value) -> None:
        self._data[STRATEGY_EXECUTION_SERVICE] = value

    @property
    def strategy_lab_adapters(self) -> dict:
        """``{venue: adapter}``. Empty when Phase 6 is off, never ``None``."""
        return self._data.get(STRATEGY_LAB_ADAPTERS) or {}

    @strategy_lab_adapters.setter
    def strategy_lab_adapters(self, value) -> None:
        self._data[STRATEGY_LAB_ADAPTERS] = dict(value or {})

    # -- the Telegram view ---------------------------------------------------- #

    def as_bot_data(self) -> dict:
        """What ``main.py`` copies into ``app.bot_data`` when Telegram is on.

        A copy rather than the container itself, because ``bot_data`` is
        Telegram's dict and a handler writes its own keys into it
        (``strategy_lab_pending_promotions``); aliasing the two would put
        Telegram's per-chat state inside the process container. Nothing here is
        replaced after startup, so a copy and a live view are the same thing in
        practice — and the container goes in under :data:`RUNTIME` as well, so a
        handler that ever does need the live object has it.
        """
        view = dict(self._data)
        view[RUNTIME] = self
        return view
