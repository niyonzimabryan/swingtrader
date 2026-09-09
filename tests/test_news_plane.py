"""Phase 4 — the news plane: timestamps, clustering, eligibility, consensus.

The Spec O section 6 tests this file is responsible for:

``test_news_cluster_counts_once``
``test_untimestamped_news_not_replay_eligible``
``test_fact_known_at_is_article_not_cluster``
``test_news_eligibility_matrix``
``test_news_derivatives_stay_in_postgres``
``test_consensus_from_news_is_deterministic``
``test_consensus_news_disagreement_recorded``

Everything runs offline against ``tests/fixtures/news/`` — Alpaca-shaped
payloads served through ``httpx.MockTransport``, so the client's throttle,
headers, pagination and decode all execute without a network.
"""

from __future__ import annotations

import ast
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path

import httpx

from database.db import get_session_factory
from database.models import NewsArticle, NewsCluster, SourceObservation
from filings.observations import (
    MIRROR_ALLOWED_KEY,
    mirror_allowed,
    observations_known_at,
)
from news import clustering, consensus_from_news, mirror_guard
from news.alpaca_news import (
    AlpacaNewsClient,
    AlpacaNewsSchemaError,
    article_from_payload,
)
from news.api import eligibility_report, news_timeline
from news.articles import (
    TIER_AGGREGATOR,
    TIER_ESTABLISHED,
    TIER_PRIMARY,
    TIER_UNATTRIBUTED,
    WARN_NO_PUBLISHER_TIMESTAMP,
    Article,
    canonical_url,
    tier_at_least,
    tier_for_publisher,
)
from news.eligibility import (
    USE_COVARIATE,
    USE_DATE_EVENT,
    USE_DOSSIER,
    USE_PROMOTION,
    USE_QUALIFY_COHORT,
    UnknownUse,
    eligible_for,
)
from news.finnhub_news import article_from_payload as finnhub_article
from news.ingest import (
    FACT_TYPE_CONSENSUS_EPS,
    FACT_TYPE_STORY,
    NewsPlaneDisabled,
    ingest_articles,
)
from tests.dbfixture import init_test_db

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "news"
REPO_ROOT = Path(__file__).resolve().parent.parent

SYMBOL = "FCTX"


def load_payload(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def load_articles(name: str) -> list[Article]:
    return [article_from_payload(entry) for entry in load_payload(name)["news"]]


class EnabledSettings:
    plane_news_enabled = True
    news_minhash_permutations = 128
    news_cluster_jaccard_threshold = 0.5
    news_shingle_size = 5
    alpaca_api_key = "key"
    alpaca_secret_key = "secret"


class DisabledSettings(EnabledSettings):
    plane_news_enabled = False


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


class ClusteringTests(unittest.TestCase):
    def test_news_cluster_counts_once(self):
        """Twenty republications produce one story.

        Spec O section 6. Counting a wire pickup twenty times manufactures
        momentum out of a single event, and every count-based feature — story
        volume, novelty, "unusual coverage" — inherits the error.
        """
        articles = load_articles("wire_pickup")
        self.assertEqual(len(articles), 20)

        clusters = clustering.cluster_articles(articles)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].member_count, 20)

    def test_cluster_known_at_is_the_earliest_publisher_timestamp(self):
        clusters = clustering.cluster_articles(load_articles("wire_pickup"))
        earliest = min(a.published_at for a in load_articles("wire_pickup"))
        self.assertEqual(clusters[0].known_at_utc, earliest)
        self.assertEqual(
            clusters[0].known_at_utc, datetime(2026, 5, 12, 11, 0, tzinfo=timezone.utc)
        )

    def test_clustering_is_deterministic(self):
        """Same input set, same cluster ids — however the input is ordered."""
        articles = load_articles("wire_pickup")
        first = clustering.cluster_articles(articles)
        second = clustering.cluster_articles(list(reversed(articles)))
        self.assertEqual(
            [c.cluster_id for c in first], [c.cluster_id for c in second]
        )
        self.assertEqual(
            [sorted(c.member_uids) for c in first],
            [sorted(c.member_uids) for c in second],
        )

    def test_different_stories_stay_apart(self):
        """A morning preview is not the evening's results piece."""
        clusters = clustering.cluster_articles(load_articles("earnings_preview_and_reaction"))
        self.assertEqual(len(clusters), 2)
        self.assertEqual(sorted(c.member_count for c in clusters), [1, 2])

    def test_tracking_parameters_do_not_make_a_new_article(self):
        self.assertEqual(
            canonical_url("https://www.example.invalid/story/?utm_source=x&utm_medium=y"),
            canonical_url("http://example.invalid/story#section"),
        )

    def test_lsh_recall_loss_cannot_split_a_true_pair(self):
        """The exact-Jaccard confirmation, which is why LSH only proposes.

        ``MinHashLSH`` missed this genuinely 0.56-similar pair at a 0.45 index
        threshold with 128 permutations. Deciding on the exact Jaccard of the
        shingle sets makes the outcome independent of the permutation count.
        """
        flash, reaction = [
            a for a in load_articles("earnings_preview_and_reaction")
            if a.provider_id in {"flash-01", "reaction-01"}
        ]
        left = set(clustering.shingles(flash.shingle_text(), 5))
        right = set(clustering.shingles(reaction.shingle_text(), 5))
        similarity = clustering.jaccard(left, right)
        self.assertGreater(similarity, clustering.DEFAULT_THRESHOLD)

        for permutations in (64, 128, 256):
            clusters = clustering.cluster_articles(
                [flash, reaction], permutations=permutations
            )
            self.assertEqual(len(clusters), 1, f"split at num_perm={permutations}")


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class TimestampTests(unittest.TestCase):
    def test_untimestamped_news_not_replay_eligible(self):
        """An article we cannot date cannot qualify a ``clean_pit`` cohort.

        Spec O section 6. Our own fetch time is not a publication time: an
        article stamped when we noticed it looks available the moment we
        started looking, which is lookahead shaped like diligence.
        """
        undated = next(
            a for a in load_articles("tiering") if a.provider_id == "tier-undated"
        )
        self.assertFalse(undated.has_timestamp)
        self.assertFalse(undated.replay_eligible)
        self.assertIn(WARN_NO_PUBLISHER_TIMESTAMP, undated.warnings)
        self.assertIsNotNone(undated.first_seen_at, "we still know when we saw it")

        for use in (USE_DATE_EVENT, USE_QUALIFY_COHORT, USE_COVARIATE):
            self.assertFalse(eligible_for(undated, use).allowed)
        self.assertTrue(eligible_for(undated, USE_DOSSIER).allowed)

    def test_finnhub_epoch_zero_is_undated_not_1970(self):
        """A ``datetime`` of 0 is a missing timestamp, not a 1970 publication.

        A 1970 stamp sorts first and becomes the cluster's ``known_at_utc`` —
        a colourful bug that quietly dates a story to before the company
        existed.
        """
        article = finnhub_article(
            {"headline": "x", "datetime": 0, "source": "Reuters", "url": "u"},
            symbol=SYMBOL,
        )
        self.assertIsNone(article.published_at)
        self.assertFalse(article.replay_eligible)

    def test_alpaca_missing_created_at_is_not_filled_in(self):
        article = article_from_payload(
            {"id": "x", "headline": "h", "created_at": None, "source": "Reuters"}
        )
        self.assertIsNone(article.published_at)

    def test_alpaca_shape_change_raises(self):
        with self.assertRaises(AlpacaNewsSchemaError):
            article_from_payload({"id": "x", "headline": "h"})
        with self.assertRaises(AlpacaNewsSchemaError):
            article_from_payload(
                {"id": "x", "headline": "h", "created_at": "not-a-time"}
            )


# ---------------------------------------------------------------------------
# The eligibility matrix
# ---------------------------------------------------------------------------


class EligibilityTests(unittest.TestCase):
    def articles(self) -> dict:
        return {a.provider_id: a for a in load_articles("tiering")}

    def test_news_eligibility_matrix(self):
        """Each row of Spec O section 5.3 is enforced.

        An established-tier article qualifies a cohort fact; an aggregator-tier
        one does not; nothing from news reaches a promotion.
        """
        found = self.articles()
        primary = found["tier-primary"]
        established = found["tier-established"]
        aggregator = found["tier-aggregator"]
        undated = found["tier-undated"]

        self.assertEqual(primary.tier, TIER_PRIMARY)
        self.assertEqual(established.tier, TIER_ESTABLISHED)
        self.assertEqual(aggregator.tier, TIER_AGGREGATOR)
        self.assertEqual(undated.tier, TIER_UNATTRIBUTED)

        # Row 1: date an event — primary tier plus a timestamp.
        self.assertTrue(eligible_for(primary, USE_DATE_EVENT).allowed)
        self.assertFalse(eligible_for(established, USE_DATE_EVENT).allowed)
        self.assertFalse(eligible_for(aggregator, USE_DATE_EVENT).allowed)

        # Row 2: qualify a cohort — established or better, deterministic.
        self.assertTrue(eligible_for(established, USE_QUALIFY_COHORT).allowed)
        self.assertTrue(eligible_for(primary, USE_QUALIFY_COHORT).allowed)
        self.assertFalse(eligible_for(aggregator, USE_QUALIFY_COHORT).allowed)
        self.assertFalse(
            eligible_for(
                established, USE_QUALIFY_COHORT, deterministic_extraction=False
            ).allowed
        )

        # Row 3: covariate — a timestamp is enough, any tier.
        self.assertTrue(eligible_for(aggregator, USE_COVARIATE).allowed)
        self.assertFalse(eligible_for(undated, USE_COVARIATE).allowed)

        # Row 4: dossier evidence — any tier, rendered with its tier.
        for article in (primary, established, aggregator, undated):
            self.assertTrue(eligible_for(article, USE_DOSSIER).allowed)

        # Row 5: promotion evidence — never, for anything.
        for article in (primary, established, aggregator, undated):
            decision = eligible_for(article, USE_PROMOTION)
            self.assertFalse(decision.allowed)
            self.assertIn("never", decision.reason)

    def test_provenance_class_is_vendor_pit_where_the_matrix_says_so(self):
        found = self.articles()
        self.assertEqual(
            eligible_for(found["tier-primary"], USE_DATE_EVENT).provenance_class,
            "vendor_pit",
        )
        self.assertEqual(
            eligible_for(found["tier-established"], USE_QUALIFY_COHORT).provenance_class,
            "vendor_pit",
        )
        self.assertIsNone(eligible_for(found["tier-primary"], USE_DOSSIER).provenance_class)

    def test_an_unknown_publisher_is_unattributed_not_established(self):
        """Fail closed: a name we do not know cannot qualify a cohort."""
        tier, warnings = tier_for_publisher("Some Blog Nobody Has Heard Of")
        self.assertEqual(tier, TIER_UNATTRIBUTED)
        self.assertTrue(warnings)
        self.assertFalse(tier_at_least(tier, TIER_ESTABLISHED))

    def test_a_use_outside_the_matrix_raises(self):
        """A missing rule must not read as permission."""
        with self.assertRaises(UnknownUse):
            eligible_for(self.articles()["tier-primary"], "train_a_model")

    def test_eligibility_report_covers_every_row(self):
        report = eligibility_report(self.articles()["tier-established"])
        self.assertEqual(
            set(report),
            {USE_DATE_EVENT, USE_QUALIFY_COHORT, USE_COVARIATE, USE_DOSSIER, USE_PROMOTION},
        )


# ---------------------------------------------------------------------------
# The consensus extractor
# ---------------------------------------------------------------------------


class ConsensusTests(unittest.TestCase):
    def test_consensus_from_news_is_deterministic(self):
        """The same articles always yield the same figure, from a regex grammar.

        Spec N section 4.0. A model reading the number out of the sentence
        would be parsing rather than producing a statistic, but it would not be
        reproducible — and a consensus that changes when the model is upgraded
        silently relabels every cohort built on it.
        """
        articles = load_articles("consensus_disagreement")
        readings = [
            consensus_from_news.read_consensus(articles),
            consensus_from_news.read_consensus(articles),
            consensus_from_news.read_consensus(list(reversed(articles))),
        ]
        self.assertEqual(readings[0].as_dict(), readings[1].as_dict())
        self.assertEqual(
            readings[0].as_dict(), readings[2].as_dict(), "order must not matter"
        )

        # And the extractor itself is a pure function of text.
        text = "Analysts expect EPS of $1.23 for the quarter."
        self.assertEqual(
            consensus_from_news.extract_from_text(text),
            consensus_from_news.extract_from_text(text),
        )

    def test_consensus_news_disagreement_recorded(self):
        """Two publishers, two figures: both kept, with the spread.

        Zacks, FactSet and Refinitiv consensus differ and articles cite
        whichever their author uses. Averaging them would invent a number no
        source published.
        """
        cluster = clustering.cluster_articles(load_articles("consensus_disagreement"))[0]
        reading = consensus_from_news.read_consensus(cluster.members)

        self.assertEqual(reading.distinct_values, (1.18, 1.23))
        self.assertAlmostEqual(reading.spread, 0.05, places=6)
        self.assertTrue(reading.disagreement)
        self.assertIn(
            consensus_from_news.WARN_CONSENSUS_DISAGREEMENT, reading.warnings
        )
        self.assertEqual(reading.total_articles, 2)
        # The tie-break: equal citation counts, so the earliest article wins.
        self.assertEqual(reading.value, 1.23)
        self.assertEqual(
            reading.known_at_utc, datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
        )
        self.assertEqual(len({m.article_uid for m in reading.mentions}), 2)

    def test_revenue_figures_are_not_read_as_eps(self):
        """``$4.1 billion consensus`` is not a $4.1 EPS estimate."""
        self.assertEqual(
            consensus_from_news.extract_from_text(
                "Revenue of $4.1 billion topped the $4.0 billion consensus."
            ),
            [],
        )

    def test_an_undated_article_cannot_carry_a_consensus(self):
        undated = next(
            a for a in load_articles("tiering") if a.provider_id == "tier-undated"
        )
        self.assertTrue(consensus_from_news.extract_from_article(undated))
        self.assertIsNone(consensus_from_news.read_consensus([undated]))


# ---------------------------------------------------------------------------
# The ledger rows
# ---------------------------------------------------------------------------


class NewsLedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("news_plane")
        self.session = get_session_factory()()
        self.addCleanup(self.db.cleanup)
        self.addCleanup(self.session.close)

    def ingest(self, name: str, **kwargs):
        return ingest_articles(
            self.session, load_articles(name), settings=EnabledSettings(), **kwargs
        )

    def rows(self, fact_type=None):
        return observations_known_at(
            self.session,
            fact_type=fact_type,
            cutoff=datetime(2030, 1, 1, tzinfo=timezone.utc),
        )

    def test_plane_flag_defaults_off(self):
        with self.assertRaises(NewsPlaneDisabled):
            ingest_articles(
                self.session, load_articles("wire_pickup"), settings=DisabledSettings()
            )

    def test_twenty_republications_write_one_story_row(self):
        coverage = self.ingest("wire_pickup")
        self.assertEqual(coverage.articles_stored, 20)
        self.assertEqual(coverage.clusters, 1)
        self.assertEqual(coverage.story_rows, 1)
        self.assertEqual(len(self.rows(FACT_TYPE_STORY)), 1)
        self.assertEqual(self.session.query(NewsArticle).count(), 20)
        self.assertEqual(self.session.query(NewsCluster).count(), 1)

    def test_fact_known_at_is_article_not_cluster(self):
        """A consensus figure carries its own article's timestamp.

        Spec O sections 5.1 and 6. In the fixture the 16:30 wire release states
        the actual EPS and no consensus; the 16:45 reaction piece states the
        consensus. Both are one story, so the story's ``known_at_utc`` is
        16:30 — and stamping the *figure* with 16:30 would make it available
        fifteen minutes before it was published.
        """
        self.ingest("earnings_preview_and_reaction")

        story = [
            r for r in self.rows(FACT_TYPE_STORY) if (r.payload.get("member_count") or 0) > 1
        ]
        self.assertEqual(len(story), 1)
        consensus = self.rows(FACT_TYPE_CONSENSUS_EPS)
        self.assertEqual(len(consensus), 1)

        self.assertEqual(story[0].known_at_utc, datetime(2026, 7, 28, 16, 30))
        self.assertEqual(consensus[0].known_at_utc, datetime(2026, 7, 28, 16, 45))
        self.assertGreater(consensus[0].known_at_utc, story[0].known_at_utc)
        self.assertEqual(consensus[0].value_numeric, 1.23)
        self.assertEqual(
            consensus[0].payload["known_at_source"],
            "publisher_timestamp_of_source_article",
        )
        self.assertEqual(
            story[0].payload["known_at_source"],
            "publisher_timestamp_min_across_cluster",
        )

        # And the point-in-time consequence: at 16:40 the story is knowable and
        # the consensus is not.
        at_1640 = observations_known_at(
            self.session, cutoff=datetime(2026, 7, 28, 16, 40, tzinfo=timezone.utc)
        )
        types = {r.fact_type for r in at_1640}
        self.assertIn(FACT_TYPE_STORY, types)
        self.assertNotIn(FACT_TYPE_CONSENSUS_EPS, types)

    def test_an_aggregator_figure_is_extracted_and_not_written(self):
        """Section 5.3 row 2: an aggregator does not qualify a cohort fact."""
        self.ingest("tiering")
        consensus = self.rows(FACT_TYPE_CONSENSUS_EPS)
        publishers = {r.payload["mentions"][0]["publisher"] for r in consensus}
        self.assertIn("Reuters", publishers)
        self.assertNotIn("Seeking Alpha", publishers)

    def test_undated_article_is_quarantined_not_dropped(self):
        self.ingest("tiering")
        stored = self.session.query(NewsArticle).filter(
            NewsArticle.article_uid.isnot(None)
        ).all()
        undated = [a for a in stored if a.published_at_utc is None]
        self.assertEqual(len(undated), 1)
        self.assertFalse(undated[0].replay_eligible)
        self.assertEqual(undated[0].provenance_class, "archival_reconstructed")

        timeline = news_timeline(
            self.session, ticker=SYMBOL, as_of=datetime(2030, 1, 1, tzinfo=timezone.utc),
            include_quarantined=True,
        )
        self.assertTrue(timeline["quarantined_articles"])
        self.assertNotIn(
            undated[0].headline, [s["headline"] for s in timeline["stories"]]
        )

    def test_ingest_is_idempotent(self):
        first = self.ingest("wire_pickup")
        second = self.ingest("wire_pickup")
        self.assertGreater(first.written, 0)
        self.assertEqual(second.written, 0)
        self.assertEqual(second.articles_stored, 0)

    def test_novelty_is_computed_from_structured_facts(self):
        self.ingest("wire_pickup")
        cluster = self.session.query(NewsCluster).one()
        self.assertIsNotNone(cluster.novelty_score)
        basis = json.loads(cluster.novelty_basis)
        self.assertIn("new_facts", basis)
        self.assertIn("money:1.2e+09", basis["new_facts"])

    def test_news_timeline_returns_a_provenance_block(self):
        self.ingest("wire_pickup")
        answer = news_timeline(
            self.session, ticker=SYMBOL, as_of=datetime(2030, 1, 1, tzinfo=timezone.utc)
        )
        self.assertTrue(answer["stories"])
        self.assertIn("data_quality", answer["provenance"])
        self.assertTrue(
            any("mirror_allowed=false" in note for note in answer["provenance"]["notes"])
        )


# ---------------------------------------------------------------------------
# The mirror rule
# ---------------------------------------------------------------------------


class MirrorTests(unittest.TestCase):
    def setUp(self):
        self.db = init_test_db("news_mirror")
        self.session = get_session_factory()()
        self.addCleanup(self.db.cleanup)
        self.addCleanup(self.session.close)

    def test_news_derivatives_stay_in_postgres(self):
        """The mirror refuses any news body, novelty score, or derived feature.

        Spec O sections 5 and 6. The ``research/`` mirror does not exist yet —
        it is Spec M's — so the rule ships as the guard plus this test, because
        the alternative is a rule in a document that whoever builds the mirror
        has to remember.
        """
        ingest_articles(
            self.session, load_articles("wire_pickup"), settings=EnabledSettings()
        )
        rows = self.session.query(SourceObservation).all()
        self.assertTrue(rows)
        for row in rows:
            self.assertFalse(mirror_allowed(row), row.fact_type)
            self.assertIs(row.payload[MIRROR_ALLOWED_KEY], False)
            with self.assertRaises(mirror_guard.MirrorRefused):
                mirror_guard.require_mirrorable(row.payload, context=row.fact_type)

        # The three ways a leak could get out, all closed.
        for payload in (
            {"body": "the article text"},
            {"novelty_score": 0.75},
            {"features": [{"cluster_id": "abc", "novelty_score": 0.2}]},
            {"table": "news_articles", "rows": []},
            {"source": "alpaca_news", "value": 1},
            {MIRROR_ALLOWED_KEY: False, "value": 1},
        ):
            with self.assertRaises(mirror_guard.MirrorRefused):
                mirror_guard.require_mirrorable(payload)

        # A citation — URL and date, Spec K section 3.3 — is allowed.
        mirror_guard.require_mirrorable(
            {"url": "https://example.invalid/story", "date": "2026-05-12"}
        )

    def test_a_non_news_row_is_still_mirrorable(self):
        """The marker is absent on Phase 3a's SEC rows, and absent means allowed."""
        from filings.observations import Observation, write_observations

        write_observations(self.session, [
            Observation(
                source="sec_xbrl_companyfacts",
                entity_cik="0000320193",
                fact_type="revenue",
                valid_at=datetime(2026, 3, 31, tzinfo=timezone.utc),
                known_at_utc=datetime(2026, 5, 1, 21, 3, 2, tzinfo=timezone.utc),
                known_at_source="acceptanceDateTime",
                precision="second",
                provenance_class="vendor_pit",
                source_url="https://www.sec.gov/x-index.htm",
                source_trust="primary_regulator",
                replay_eligible=True,
                value_numeric=1.0,
            )
        ])
        row = self.session.query(SourceObservation).one()
        self.assertTrue(mirror_allowed(row))

    def test_a_staged_file_is_scanned_as_text_too(self):
        """A CSV header row leaks as surely as a JSON key."""
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "export.csv"
            path.write_text("ticker,headline,novelty_score\nFCTX,x,0.5\n", encoding="utf-8")
            check = mirror_guard.check_file(path)
            self.assertFalse(check.allowed)
            self.assertTrue(check.reasons)

            clean = Path(directory) / "clean.csv"
            clean.write_text("ticker,url,date\nFCTX,https://x.invalid,2026-05-12\n",
                             encoding="utf-8")
            self.assertTrue(mirror_guard.check_file(clean).allowed)


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------


class AlpacaClientTests(unittest.TestCase):
    def transport(self, pages):
        state = {"index": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            page = pages[min(state["index"], len(pages) - 1)]
            state["index"] += 1
            return httpx.Response(200, json=page)

        return httpx.MockTransport(handler)

    def test_pagination_follows_the_token_and_stops(self):
        payload = load_payload("wire_pickup")
        first = {"news": payload["news"][:10], "next_page_token": "abc"}
        second = {"news": payload["news"][10:], "next_page_token": None}
        with AlpacaNewsClient(
            "k", "s", transport=self.transport([first, second]), sleeper=lambda _s: None
        ) as client:
            articles = client.fetch([SYMBOL])
        self.assertEqual(len(articles), 20)
        self.assertEqual(client.request_count, 2)

    def test_a_repeated_page_token_does_not_loop(self):
        payload = load_payload("wire_pickup")
        page = {"news": payload["news"][:2], "next_page_token": "same"}
        with AlpacaNewsClient(
            "k", "s", transport=self.transport([page]), sleeper=lambda _s: None
        ) as client:
            articles = client.fetch([SYMBOL], max_pages=10)
        self.assertEqual(len(articles), 4, "one repeat, then it stops")

    def test_missing_news_array_raises(self):
        with AlpacaNewsClient(
            "k", "s", transport=self.transport([{"items": []}]), sleeper=lambda _s: None
        ) as client:
            with self.assertRaises(AlpacaNewsSchemaError):
                client.fetch([SYMBOL])

    def test_credentials_are_required(self):
        with self.assertRaises(Exception):
            AlpacaNewsClient("", "")


# ---------------------------------------------------------------------------
# No model anywhere in the news plane
# ---------------------------------------------------------------------------


class NewsImportGraphTests(unittest.TestCase):
    def test_no_model_client_in_the_news_plane(self):
        """Spec N section 9: a model may never produce a statistic.

        The novelty score compares extracted facts and the consensus extractor
        is a regex grammar. Neither is a promise if a model client is one
        import away, so the import graph is asserted.
        """
        forbidden = {
            "anthropic", "openai", "google", "langfuse", "firecrawl",
            "google.genai", "agents", "orchestrator", "bot", "tools", "execution",
        }
        for path in sorted((REPO_ROOT / "news").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                names = []
                if isinstance(node, ast.Import):
                    names = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
                    names = [node.module]
                for name in names:
                    self.assertNotIn(
                        name.split(".")[0], forbidden, f"{path.name} imports {name}"
                    )


if __name__ == "__main__":
    unittest.main()
