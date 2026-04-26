import unittest
from types import SimpleNamespace

from utils.web_search_client import WebSearchClient


class WebSearchClientTests(unittest.TestCase):
    def test_gemini_json_parser_extracts_json_from_wrapped_response(self):
        client = WebSearchClient.__new__(WebSearchClient)
        result = client._parse_json(
            """Research complete.
```json
{"ticker": "AAPL", "score": 0.72}
```"""
        )

        self.assertEqual(result, {"ticker": "AAPL", "score": 0.72})

    def test_gemini_grounding_metadata_extracts_queries_and_sources(self):
        client = WebSearchClient.__new__(WebSearchClient)

        class _Metadata:
            def model_dump(self, exclude_none=True):
                return {
                    "web_search_queries": ["AAPL earnings guidance", "AAPL analyst revisions"],
                    "grounding_chunks": [
                        {"web": {"title": "Apple Investor Relations", "uri": "https://www.apple.com/investor/"}},
                        {"web": {"title": "SEC filing", "uri": "https://www.sec.gov/"}},
                    ],
                }

        response = SimpleNamespace(
            candidates=[SimpleNamespace(grounding_metadata=_Metadata())]
        )

        grounding = client._extract_grounding(response)

        self.assertTrue(grounding["grounded"])
        self.assertEqual(grounding["queries"], ["AAPL earnings guidance", "AAPL analyst revisions"])
        self.assertEqual(grounding["sources"][0]["uri"], "https://www.apple.com/investor/")

    def test_gemini_search_directive_forces_grounded_current_search(self):
        client = WebSearchClient.__new__(WebSearchClient)
        directive = client._with_search_directive("Research AAPL", max_searches=8)

        self.assertIn("Use Google Search grounding", directive)
        self.assertIn("up to 8 distinct search-query clusters", directive)
        self.assertIn("Do not rely on model memory", directive)


if __name__ == "__main__":
    unittest.main()
