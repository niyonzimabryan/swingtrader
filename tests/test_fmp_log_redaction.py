import unittest
from unittest.mock import patch

from data.pattern_data import PatternDataAdapter


class _FakeFmpResponse:
    def raise_for_status(self):
        raise RuntimeError(
            "Client error for url "
            "https://financialmodelingprep.com/stable/earnings-surprises"
            "?apikey=SECRETSECRETSECRETSECRETSECRETSECRET12&symbol=NFLX"
        )


class FmpLogRedactionTests(unittest.TestCase):
    def test_pattern_data_fmp_failure_logs_redacted_query_key(self):
        adapter = PatternDataAdapter("SECRETSECRETSECRETSECRETSECRETSECRET12")

        with (
            patch("data.pattern_data.rate_limiter.acquire"),
            patch("data.pattern_data.httpx.get", return_value=_FakeFmpResponse()),
            patch("data.pattern_data.log.error") as log_error,
        ):
            self.assertIsNone(adapter._fmp_request("/earnings-surprises", {"symbol": "NFLX"}))

        error_text = log_error.call_args.kwargs["error"]
        self.assertIn("apikey=[REDACTED]", error_text)
        self.assertNotIn("SECRETSECRET", error_text)


if __name__ == "__main__":
    unittest.main()
