"""Render one of each card to ``docs/examples/cards/`` from a fixture.

Offline and free of a clock: it reads ``tests/notifyfixture.py``, renders, and
writes. No database, no network, no ``now()`` — so a diff in the HTML and text
files is a real change to the rendering rather than noise.

    python -m scripts.render_example_cards
    python -m scripts.render_example_cards --check     # fail if the text is stale

``--check`` compares the **HTML and text** byte for byte and checks only that
each PNG is *present*. The PNGs are not byte-stable across environments:
matplotlib stamps its own version into the PNG metadata and the glyph raster
depends on the freetype build, so a strict comparison would fail on any machine
whose matplotlib differs from the one that last committed them — a false alarm
about a chart that is in fact identical. The HTML is pure Python string
building and has no such dependency, which is why it gets the strict half.

Not wired into CI: adding a job is its own change, and the test suite already
asserts the structural properties these files illustrate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "docs" / "examples" / "cards"

#: A stand-in for the signed URL. The examples are static files, so the chart is
#: written next to them and referenced by relative name; a real card links to
#: `https://<workspace>/cards/<uid>/chart.png?s=<hmac>`.
EXAMPLE_CARD_URL = "https://workspace.example.invalid/cards/EXAMPLE?s=EXAMPLE"


def _payloads() -> dict:
    from notify.cards import alert, digest, memo, proposal, scorecard
    from tests import notifyfixture as nf

    return {
        "proposal_approval": (
            proposal.build_payload(
                proposal=nf.proposal_row(),
                approvable=True,
                uid="EXAMPLE",
                created_at_utc="2026-09-13T13:47:02",
                chart=nf.chart(),
                cohort={
                    "status": "ok",
                    "depth": "full",
                    "n_events": 61,
                    "subject_qualifies": True,
                    "subject_reason": "AMD met every condition at its 2026-09-11 candidate.",
                    "as_of_utc": "2026-09-11T00:00:00",
                    "stale": False,
                    "warnings": [
                        "overlapping_events: 14 of 61 events overlap another event's holding window.",
                        "thin_sector_coverage: 3 sectors contribute fewer than 5 events each.",
                    ],
                },
                thesis={
                    "state": "active",
                    "conviction": "medium",
                    "as_of_utc": "2026-08-30T00:00:00",
                    "stale": True,
                    "summary": "Datacentre GPU share gain is under-modelled; the 2026 ramp is "
                    "contracted, not speculative.",
                },
                invalidators=[
                    "A quarter where datacentre revenue grows less than 15% sequentially.",
                    "A hyperscaler publicly re-committing capex to a competing accelerator.",
                ],
                bear_case={
                    "body": "The contracted ramp is a backlog figure, not a delivery figure, and "
                    "the supply constraint that makes it credible is the same one that "
                    "makes the 2027 comparison impossible to beat.",
                    "source": "thesis-critic, 2026-09-02",
                },
                exposure={
                    "rows": {
                        "semis exposure after this entry": "18.4% of equity",
                        "largest single name after this entry": "AMD, 6.1%",
                        "narrative tag overlap": "ai_infrastructure — 3 other positions",
                    },
                    "note": "Concentration and sector are measured over the combined book, "
                    "read-only accounts included (Spec L §5.1).",
                },
            )
        ),
        "proposal_refused": proposal.build_payload(
            proposal=nf.refused_proposal_row(),
            approvable=False,
            uid="EXAMPLE",
            created_at_utc="2026-09-13T13:47:02",
        ),
        "scan_memo": memo.build_scan_payload(
            scan_type="full",
            duration_text="4m 12s",
            total_scanned=412,
            escalated=18,
            memos_generated=2,
            memos=nf.memo_details(),
            uid="EXAMPLE",
            created_at_utc="2026-09-13T21:05:00",
            as_of_utc="2026-09-13T21:05:00",
        ),
        "memo_detail": memo.build_memo_payload(
            ticker="HIMS",
            score=0.81,
            classification="catalyst / guidance raise",
            recommendation="proceed",
            body="Raised FY guidance on the 09-12 call and named subscriber re-acceleration "
            "as the driver. Quoted from the company's own release: this is "
            "attacker-writable text and is shown as such.",
            body_trust="untrusted",
            body_source="HIMS press release, 2026-09-12",
            chart={
                "ticker": "HIMS",
                "title": "HIMS — last 12 sessions",
                "bars": list(_shifted_bars()),
                "levels": {"entry": 42.5, "stop": 38.0},
                "as_of_note": "split-adjusted closes through 2026-09-12",
                "caption": "Split-adjusted closes, drawn from the bars stored with this card.",
            },
            uid="EXAMPLE",
            created_at_utc="2026-09-13T21:05:00",
            as_of_utc="2026-09-12T20:00:00",
        ),
        "strategy_lab_scorecard": scorecard.build_payload(
            scoreboard=nf.scoreboard(), uid="EXAMPLE", created_at_utc="2026-09-13T22:00:00"
        ),
        "page_alert": alert.build_payload(
            event="position_unprotected",
            detail={
                "ticker": "AMD",
                "proposal_id": 42,
                "account": "robinhood-agentic",
                "as_of_utc": "2026-09-13T14:02:11",
                "recovery": (
                    "The entry filled and the protective stop was not read back inside the "
                    "120-second window. Open the broker app, confirm whether a stop exists at "
                    "$99.00, and place one by hand if it does not. Further entries are blocked "
                    "until this position reads back as protected."
                ),
                "filled_quantity": 34,
                "average_fill_price": 108.02,
                "stop_ref_id": "st-9f2c4b1a",
            },
            uid="EXAMPLE",
            created_at_utc="2026-09-13T14:02:11",
        ),
        "daily_digest": digest.from_markdown(
            title="Daily digest",
            markdown=_digest_markdown(),
            subject="Daily digest — Sep 13, 2026",
            headline="Friday 13 September 2026, 5 PM ET",
            eyebrow="daily digest",
            uid="EXAMPLE",
            created_at_utc="2026-09-13T21:00:00",
            as_of_utc="2026-09-13T21:00:00",
        ),
    }


def _shifted_bars():
    from tests import notifyfixture as nf

    return [{"date": bar["date"], "close": round(bar["close"] * 0.4, 2)} for bar in nf.BARS]


def _digest_markdown() -> str:
    return (
        "*📊 DAILY DIGEST — Sep 13, 2026*\n\n"
        "Portfolio: `$101,204\\.18`\n"
        "Today: 🟢 `\\+$1,204\\.18` \\(`\\+1\\.20%`\\)\n\n"
        "*POSITIONS*\n"
        "AMD  `\\+4\\.1%`  \\(`$6,140`\\)  day 7 of 20\n"
        "HIMS `\\-2\\.3%`  \\(`$3,980`\\)  day 2 of 20\n"
        "GIS  `\\+0\\.4%`  \\(`$5,010`\\)  day 14 of 20\n\n"
        "*TODAY'S ACTIVITY*\n"
        "Memos generated: `2`\n"
        "Orders filled: `1`\n"
        "Proposals: `1` proposed, `1` risk\\_rejected\n\n"
        "*ALERTS*\n"
        "GIS is at day 14 of a 20\\-day maximum hold\\.\n"
    )


def render_files() -> dict:
    """``{relative_path: bytes}`` for every example. Pure."""
    from notify.cards import base
    from notify.cards.chart import ChartUnavailable, render_png

    files: dict[str, bytes] = {}
    for name, payload in _payloads().items():
        chart = payload.get("chart") or {}
        chart_name = ""
        if chart.get("bars"):
            try:
                files[f"{name}.chart.png"] = render_png(chart)
                chart_name = f"{name}.chart.png"
            except ChartUnavailable:
                chart_name = ""
        rendered = base.render(payload, chart_url=chart_name, card_url=EXAMPLE_CARD_URL)
        files[f"{name}.email.html"] = rendered.html_email.encode("utf-8")
        files[f"{name}.page.html"] = rendered.html_page.encode("utf-8")
        files[f"{name}.txt"] = rendered.text.encode("utf-8")
    files["README.md"] = _readme(sorted(_payloads())).encode("utf-8")
    return files


def _readme(names) -> str:
    lines = [
        "# Example cards",
        "",
        "Generated by `python -m scripts.render_example_cards` from the fixtures in",
        "`tests/notifyfixture.py`. Every number in them is made up; they exist so a",
        "reviewer can open the rendering in a browser rather than read the HTML.",
        "",
        "Each card is three files:",
        "",
        "- `*.email.html` — what Resend sends. Table layout, inline CSS, no JavaScript,",
        "  no inline SVG, a light baseline with a `prefers-color-scheme` dark override.",
        "- `*.page.html` — what `/cards/<uid>` serves: the same payload, every block,",
        "  including the ones the email leaves out.",
        "- `*.txt` — the plain-text part of the email.",
        "",
        "A chart, where the card has one, is the sibling `*.chart.png`. In production it",
        "is served by the workspace at `/cards/<uid>/chart.png?s=<hmac>` and referenced",
        "by URL, because Gmail strips `data:` URIs and proxies remote images instead.",
        "",
        "Re-render after any change to `notify/cards/` and commit the diff. The HTML",
        "and text are byte-stable, so a diff in those is a real rendering change; the",
        "PNGs carry matplotlib's version in their metadata and will differ between",
        "environments even when the chart is identical, which is why",
        "`--check` compares the HTML strictly and the PNGs for presence only.",
        "",
        "| card | what it is |",
        "|---|---|",
    ]
    descriptions = {
        "proposal_approval": "a `propose_order` row awaiting approval, with the full risk math, "
        "the cohort evidence and its warnings, the thesis and its invalidators, the stored "
        "bear case, and the exposure impact",
        "proposal_refused": "the same card for a `risk_rejected` proposal — shown with its "
        "reason, and with nothing to approve",
        "scan_memo": "a scan-complete summary: the run's counts and one row per memo",
        "memo_detail": "one name's memo, including untrusted-origin text rendered distinctly",
        "strategy_lab_scorecard": "the Strategy Lab scoreboard, warnings and refusals verbatim",
        "page_alert": "a page — an unprotected position, with its recovery text verbatim",
        "daily_digest": "the 5 PM ET daily digest",
    }
    for name in names:
        lines.append(f"| [`{name}`]({name}.email.html) | {descriptions.get(name, '')} |")
    lines.append("")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the committed examples differ from a fresh render",
    )
    parser.add_argument("--out", default=str(OUTPUT_DIR))
    args = parser.parse_args(argv)

    out = Path(args.out)
    files = render_files()

    if args.check:
        stale = []
        for name, content in sorted(files.items()):
            path = out / name
            if not path.exists():
                stale.append(f"{name} (missing)")
            elif not name.endswith(".png") and path.read_bytes() != content:
                stale.append(name)
        if stale:
            print("stale example cards: " + ", ".join(stale))
            print("re-run: python -m scripts.render_example_cards")
            return 1
        print(f"{len(files)} example files are current (PNGs checked for presence only).")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    for name, content in sorted(files.items()):
        (out / name).write_bytes(content)
    print(f"wrote {len(files)} files to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
