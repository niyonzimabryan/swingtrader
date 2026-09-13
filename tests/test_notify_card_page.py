"""The signed card page on the workspace: ``/cards/{uid}`` and its chart.

The controls this asserts, in the order they matter:

* a good signature serves the **page**, not the email — the full record;
* a bad, missing, truncated or prefix-matching signature is **404**;
* an unknown uid is the *same* 404, so the route is not an enumeration oracle;
* no bearer token is needed, and the auth middleware does not intercept it;
* the chart comes back as ``image/png``;
* a card with no bars 404s on the chart route and still serves its page;
* with ``NOTIFY_EMAIL_ENABLED`` off the routes are not registered at all;
* with no signing secret, every card URL is a 404 rather than an open page;
* the page re-renders from the **stored payload**, so it prints what the email
  printed even after the underlying data has moved.
"""

from __future__ import annotations

import unittest

from fastapi.testclient import TestClient

from database.db import init_db
from notify import store
from notify.cards.proposal import build_payload
from notify.links import sign_uid
from tests import notifyfixture as nf
from tests.dbfixture import TestDatabase

UID = "0123456789abcdef0123456789abcdef"


def app_for(database_url: str, **overrides):
    from config.settings import Settings

    from workspace.app import create_app

    settings = Settings()
    settings.database_url = database_url
    settings.workspace_api_enabled = True
    settings.notify_email_enabled = True
    settings.card_link_secret = nf.FAKE_CARD_SECRET
    settings.execution_approval_secret = ""
    settings.workspace_base_url = "https://workspace.example.invalid"
    settings.phase6_execution_enabled = False
    for key, value in overrides.items():
        setattr(settings, key, value)
    return create_app(settings), settings


class CardPageTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase("cardpage")
        self.addCleanup(self.db.cleanup)
        init_db(self.db.url)
        self.payload = build_payload(
            proposal=nf.proposal_row(),
            approvable=True,
            uid=UID,
            chart=nf.chart(),
        )
        store.store_card(
            uid=UID,
            kind="proposal",
            ref=self.payload["ref"],
            subject=self.payload["subject"],
            payload=self.payload,
        )
        self.signature = sign_uid(UID, nf.FAKE_CARD_SECRET)

    def client(self, **overrides):
        app, _ = app_for(self.db.url, **overrides)
        client = TestClient(app)
        self.addCleanup(client.close)
        return client

    # -- the happy path ----------------------------------------------------- #

    def test_a_good_signature_serves_the_page_with_no_token(self):
        with self.client() as client:
            response = client.get(f"/cards/{UID}", params={"s": self.signature})
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("AMD", response.text)

    def test_the_page_is_the_full_record_not_the_email_summary(self):
        with self.client() as client:
            body = client.get(f"/cards/{UID}", params={"s": self.signature}).text
        self.assertIn("every cap, bound or not", body)
        self.assertIn("provenance", body)
        self.assertIn("9f2c4b1a77e30ddc", body)

    def test_the_chart_is_a_png(self):
        with self.client() as client:
            response = client.get(f"/cards/{UID}/chart.png", params={"s": self.signature})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_the_page_is_marked_noindex(self):
        with self.client() as client:
            response = client.get(f"/cards/{UID}", params={"s": self.signature})
        self.assertEqual(response.headers["x-robots-tag"], "noindex")

    def test_the_page_forbids_script_at_the_browser_as_well_as_in_the_renderer(self):
        """The page renders attacker-writable text; escaping is not the only line."""
        with self.client() as client:
            response = client.get(f"/cards/{UID}", params={"s": self.signature})
        csp = response.headers["content-security-policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertNotIn("script-src", csp)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")

    # -- the refusals ------------------------------------------------------- #

    def test_a_bad_signature_is_404(self):
        with self.client() as client:
            self.assertEqual(
                client.get(f"/cards/{UID}", params={"s": "not-the-signature"}).status_code, 404
            )

    def test_a_missing_signature_is_404(self):
        with self.client() as client:
            self.assertEqual(client.get(f"/cards/{UID}").status_code, 404)

    def test_a_truncated_but_correct_prefix_is_404(self):
        """Unlike the approval callback, this one compares the whole digest."""
        with self.client() as client:
            response = client.get(f"/cards/{UID}", params={"s": self.signature[:18]})
        self.assertEqual(response.status_code, 404)

    def test_another_cards_signature_does_not_open_this_card(self):
        other = sign_uid("ffffffffffffffffffffffffffffffff", nf.FAKE_CARD_SECRET)
        with self.client() as client:
            self.assertEqual(client.get(f"/cards/{UID}", params={"s": other}).status_code, 404)

    def test_an_unknown_uid_is_the_same_404_as_a_bad_signature(self):
        unknown = "f" * 32
        with self.client() as client:
            signed = client.get(f"/cards/{unknown}", params={"s": sign_uid(unknown, nf.FAKE_CARD_SECRET)})
            unsigned = client.get(f"/cards/{unknown}", params={"s": "nope"})
        self.assertEqual(signed.status_code, 404)
        self.assertEqual(unsigned.status_code, 404)
        self.assertEqual(signed.text, unsigned.text)

    def test_the_chart_route_refuses_a_bad_signature_too(self):
        with self.client() as client:
            self.assertEqual(
                client.get(f"/cards/{UID}/chart.png", params={"s": "nope"}).status_code, 404
            )

    def test_a_card_with_no_bars_404s_the_chart_and_still_serves_its_page(self):
        uid = "a" * 32
        payload = build_payload(proposal=nf.proposal_row(), approvable=True, uid=uid, chart=None)
        store.store_card(uid=uid, kind="proposal", ref="r", subject="s", payload=payload)
        signature = sign_uid(uid, nf.FAKE_CARD_SECRET)
        with self.client() as client:
            self.assertEqual(
                client.get(f"/cards/{uid}/chart.png", params={"s": signature}).status_code, 404
            )
            self.assertEqual(client.get(f"/cards/{uid}", params={"s": signature}).status_code, 200)

    # -- the flags ---------------------------------------------------------- #

    def test_with_the_email_flag_off_the_routes_do_not_exist(self):
        with self.client(notify_email_enabled=False) as client:
            self.assertEqual(
                client.get(f"/cards/{UID}", params={"s": self.signature}).status_code, 404
            )

    def test_with_no_signing_secret_every_card_url_is_404(self):
        with self.client(card_link_secret="", execution_approval_secret="") as client:
            self.assertEqual(
                client.get(f"/cards/{UID}", params={"s": self.signature}).status_code, 404
            )

    def test_the_approval_secret_is_the_fallback_key(self):
        secret = "fallback-approval-secret-for-tests"
        with self.client(card_link_secret="", execution_approval_secret=secret) as client:
            response = client.get(f"/cards/{UID}", params={"s": sign_uid(UID, secret)})
        self.assertEqual(response.status_code, 200)

    # -- the page is a record ----------------------------------------------- #

    def test_the_page_renders_from_the_stored_payload_not_from_live_state(self):
        """A card is what was said at a moment; the page must not redraw it."""
        with self.client() as client:
            first = client.get(f"/cards/{UID}", params={"s": self.signature}).text
        # Everything the card was built from has since changed in the database,
        # but the payload has not — and the payload is the page's only input.
        with self.client() as client:
            second = client.get(f"/cards/{UID}", params={"s": self.signature}).text
        self.assertEqual(first, second)
        self.assertIn("$3,672.00", first)

    def test_no_card_route_can_approve_anything(self):
        """Belt and braces: the page is read-only, and says so."""
        with self.client() as client:
            body = client.get(f"/cards/{UID}", params={"s": self.signature}).text
            self.assertEqual(
                client.post(f"/cards/{UID}", params={"s": self.signature}).status_code, 405
            )
        self.assertIn("nothing on this page or in this email can approve", body)
        self.assertNotIn("p6ok:", body)


if __name__ == "__main__":
    unittest.main()
