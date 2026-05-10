import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

# Provide a stub firecrawl module so the client can import lazily without the
# real dependency being installed in the test environment.
firecrawl_stub = sys.modules.setdefault("firecrawl", SimpleNamespace())


class _StubApp:
    def __init__(self, api_key, scrape_result=None, search_result=None, raise_on=None):
        self.api_key = api_key
        self._scrape_result = scrape_result
        self._search_result = search_result
        self._raise_on = raise_on or set()

    def scrape(self, url, formats=None):
        if "scrape" in self._raise_on:
            raise RuntimeError("boom")
        return self._scrape_result

    def search(self, query, limit=5):
        if "search" in self._raise_on:
            raise RuntimeError("boom")
        return self._search_result


def _install_stub_app(scrape_result=None, search_result=None, raise_on=None):
    def _factory(api_key):
        return _StubApp(api_key, scrape_result, search_result, raise_on)

    firecrawl_stub.FirecrawlApp = _factory
    return _factory


class FirecrawlClientTests(unittest.TestCase):
    def test_disabled_when_no_api_key(self):
        from utils.firecrawl_client import FirecrawlClient

        with patch.dict("os.environ", {}, clear=False):
            # Ensure environment doesn't accidentally provide a key
            client = FirecrawlClient(api_key="")
        self.assertFalse(client.is_available)
        self.assertIsNone(client.scrape("https://example.com"))
        self.assertIsNone(client.search("anything"))

    def test_scrape_returns_markdown_dict_response(self):
        from utils.firecrawl_client import FirecrawlClient

        _install_stub_app(scrape_result={"markdown": "# Body\n\nText."})
        client = FirecrawlClient(api_key="fc-test")
        self.assertTrue(client.is_available)
        out = client.scrape("https://example.com/post")
        self.assertEqual(out, "# Body\n\nText.")
        self.assertEqual(client.calls_used, 1)

    def test_scrape_handles_object_response(self):
        from utils.firecrawl_client import FirecrawlClient

        page = SimpleNamespace(markdown="objbody")
        _install_stub_app(scrape_result=page)
        client = FirecrawlClient(api_key="fc-test")
        self.assertEqual(client.scrape("https://example.com"), "objbody")

    def test_scrape_returns_none_on_exception(self):
        from utils.firecrawl_client import FirecrawlClient

        _install_stub_app(raise_on={"scrape"})
        client = FirecrawlClient(api_key="fc-test")
        self.assertIsNone(client.scrape("https://example.com"))

    def test_search_normalizes_results(self):
        from utils.firecrawl_client import FirecrawlClient

        _install_stub_app(search_result={
            "data": [
                {"url": "https://a", "title": "A", "description": "d", "markdown": "m"},
                {"url": "https://b"},
            ]
        })
        client = FirecrawlClient(api_key="fc-test")
        results = client.search("q")
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0]["url"], "https://a")
        self.assertEqual(results[1]["title"], "")

    def test_call_cap_blocks_further_calls(self):
        from utils.firecrawl_client import FirecrawlClient

        _install_stub_app(scrape_result={"markdown": "x"})
        client = FirecrawlClient(api_key="fc-test", max_calls=2)
        self.assertEqual(client.scrape("https://1"), "x")
        self.assertEqual(client.scrape("https://2"), "x")
        self.assertIsNone(client.scrape("https://3"))


if __name__ == "__main__":
    unittest.main()
