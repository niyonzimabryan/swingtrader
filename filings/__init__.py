"""SEC filings evidence plane.

Phase 3a ships the *minimum SEC ingestion contract* (Spec N section 4.0): the
three free, credential-less feeds that the comparable-setups engine cannot be
built without.

* ``filings.client``       — the one place that talks to SEC over HTTP.
* ``filings.xbrl_aliases`` — explicit XBRL tag-alias maps and coverage alerts.
* ``filings.observations`` — writing and reading ``source_observations``.
* ``filings.sec_minimal``  — the three feeds, turned into observations.

Phase 4 adds 13D/G, Form 4, entity history and news on top of the same ledger.
Nothing in this package places an order, and nothing in it calls a model.
"""
