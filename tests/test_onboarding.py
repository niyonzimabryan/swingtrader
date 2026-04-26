import tempfile
import unittest
from pathlib import Path

from config.onboarding import (
    ENV_FIELDS,
    completion_counts,
    merged_env_values,
    read_env_file,
    required_fields,
    write_env_file,
)


class OnboardingSchemaTests(unittest.TestCase):
    def test_required_fields_include_all_core_trading_and_data_keys_but_not_gemini(self):
        required = {field.name for field in required_fields()}

        self.assertIn("ANTHROPIC_API_KEY", required)
        self.assertIn("TELEGRAM_BOT_TOKEN", required)
        self.assertIn("TELEGRAM_CHAT_ID", required)
        self.assertIn("ALPACA_API_KEY", required)
        self.assertIn("ALPACA_SECRET_KEY", required)
        self.assertIn("ALPACA_BASE_URL", required)
        self.assertIn("FINNHUB_API_KEY", required)
        self.assertIn("FMP_API_KEY", required)
        self.assertIn("ALPHA_VANTAGE_API_KEY", required)
        self.assertIn("FRED_API_KEY", required)
        self.assertIn("DATABASE_URL", required)
        self.assertIn("SCHEDULER_ENABLED", required)

        self.assertNotIn("GEMINI_API_KEY", required)

    def test_completion_counts_ignore_optional_gemini(self):
        values = {
            field.name: field.default or "configured"
            for field in ENV_FIELDS
            if field.required
        }
        values["GEMINI_API_KEY"] = ""

        counts = completion_counts(values)

        self.assertEqual(counts["complete"], counts["required"])

    def test_write_env_file_preserves_unknown_values_and_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text("CUSTOM_FLAG=kept\nANTHROPIC_API_KEY=old\n", encoding="utf-8")

            write_env_file(
                {
                    "ANTHROPIC_API_KEY": "new-key",
                    "TELEGRAM_CHAT_ID": "123",
                },
                env_path,
            )
            values = read_env_file(env_path)
            merged = merged_env_values(env_path)

        self.assertEqual(values["CUSTOM_FLAG"], "kept")
        self.assertEqual(values["ANTHROPIC_API_KEY"], "new-key")
        self.assertEqual(values["TELEGRAM_CHAT_ID"], "123")
        self.assertEqual(merged["ALPACA_BASE_URL"], "https://paper-api.alpaca.markets")
        self.assertEqual(merged["DATABASE_URL"], "sqlite:///swing_trader.db")


if __name__ == "__main__":
    unittest.main()
