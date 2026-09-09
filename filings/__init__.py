"""SEC filings evidence plane.

Phase 3a ships the *minimum SEC ingestion contract* (Spec N section 4.0): the
three free, credential-less feeds that the comparable-setups engine cannot be
built without.

* ``filings.client``       — the one place that talks to SEC over HTTP.
* ``filings.xbrl_aliases`` — explicit XBRL tag-alias maps and coverage alerts.
* ``filings.observations`` — writing and reading ``source_observations``.
* ``filings.sec_minimal``  — the three feeds, turned into observations.

Phase 4 builds the rest of the filings plane (Spec O section 3) on that same
client and that same seam, rather than beside them:

* ``filings.form4``           — insider transactions, with the codes kept apart.
* ``filings.ownership``       — Schedules 13D/G. **13F is out of scope.**
* ``filings.eight_k``         — the 8-K item index beyond Item 2.02.
* ``filings.entities``        — CIK/ticker/name history with validity ranges.
* ``filings.openfigi_client`` — the one place that talks to OpenFIGI.
* ``filings.cusip``           — CUSIP to ticker, failures surfaced not dropped.
* ``filings.tracked_investors`` — the owner-maintained manager list.
* ``filings.plane``           — the Phase 4 ingest, behind ``PLANE_FILINGS_ENABLED``.
* ``filings.api``             — ``filings_recent`` and ``insider_activity``.
* ``filings.provenance``      — the provenance block every plane read returns.

The macro plane lives in ``macro/`` and the news plane in ``news/``; both write
through ``filings.observations`` because that is where the point-in-time rules
are enforced.

Nothing in this package places an order, and nothing in it calls a model.
"""
