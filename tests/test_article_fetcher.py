import unittest
from types import SimpleNamespace
from unittest.mock import patch

from utils.article_fetcher import ArticleFetcher


class _FakeFirecrawl:
    def __init__(self, body=None, available=True):
        self._body = body
        self.is_available = available

    def scrape(self, url):
        return self._body


def _httpx_response(status=200, text="<html><body>" + "the article body " * 60 + "</body></html>"):
    return SimpleNamespace(status_code=status, text=text)


class _FakeClient:
    def __init__(self, response):
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None):
        return self._response


class ArticleFetcherTests(unittest.TestCase):
    def test_returns_unavailable_when_url_missing(self):
        f = ArticleFetcher(firecrawl_client=None, archive_enabled=False)
        text, status = f.fetch("")
        self.assertIsNone(text)
        self.assertEqual(status, "unavailable")

    def test_firecrawl_first_when_available(self):
        firecrawl = _FakeFirecrawl(body="# Hello body")
        f = ArticleFetcher(firecrawl_client=firecrawl, archive_enabled=True, min_archive_interval_s=0)
        text, status = f.fetch("https://example.com/article")
        self.assertEqual(text, "# Hello body")
        self.assertEqual(status, "firecrawl")

    def test_falls_back_to_archive_when_firecrawl_empty(self):
        firecrawl = _FakeFirecrawl(body=None)
        f = ArticleFetcher(firecrawl_client=firecrawl, archive_enabled=True, min_archive_interval_s=0)
        with patch("utils.article_fetcher.httpx.Client", return_value=_FakeClient(_httpx_response())):
            text, status = f.fetch("https://example.com/article")
        self.assertEqual(status, "archive")
        self.assertIn("article body", text)

    def test_returns_paywalled_when_both_routes_fail(self):
        firecrawl = _FakeFirecrawl(body=None)
        f = ArticleFetcher(firecrawl_client=firecrawl, archive_enabled=True, min_archive_interval_s=0)
        with patch("utils.article_fetcher.httpx.Client", return_value=_FakeClient(_httpx_response(status=404, text=""))):
            text, status = f.fetch("https://example.com/article")
        self.assertIsNone(text)
        self.assertEqual(status, "paywalled")

    def test_unavailable_when_nothing_configured(self):
        f = ArticleFetcher(firecrawl_client=None, archive_enabled=False)
        text, status = f.fetch("https://example.com/article")
        self.assertIsNone(text)
        self.assertEqual(status, "unavailable")

    def test_archive_thin_body_treated_as_failure(self):
        firecrawl = _FakeFirecrawl(body=None)
        f = ArticleFetcher(firecrawl_client=firecrawl, archive_enabled=True, min_archive_interval_s=0)
        with patch("utils.article_fetcher.httpx.Client", return_value=_FakeClient(_httpx_response(text="<html>tiny</html>"))):
            text, status = f.fetch("https://example.com/article")
        self.assertIsNone(text)
        self.assertEqual(status, "paywalled")


if __name__ == "__main__":
    unittest.main()
