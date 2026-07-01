import importlib.util
import sqlite3

from config.onboarding import ENV_FIELDS, render_env_file
from config.settings import Settings
from database.models import Base


RETIRED_MODULES = (
    "agents.reddit_agent",
    "data.reddit_data",
)

RETIRED_ENV_FIELDS = (
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USER_AGENT",
)


def test_legacy_reddit_runtime_modules_are_retired():
    for module_name in RETIRED_MODULES:
        assert importlib.util.find_spec(module_name) is None


def test_reddit_credentials_are_not_part_of_supported_configuration_surface():
    assert not hasattr(Settings(), "reddit_client_id")
    assert not hasattr(Settings(), "reddit_client_secret")
    assert not hasattr(Settings(), "reddit_user_agent")

    onboarding_fields = {field.name for field in ENV_FIELDS}
    for field_name in RETIRED_ENV_FIELDS:
        assert field_name not in onboarding_fields
        assert f"{field_name}=" not in render_env_file({})


def test_old_reddit_sentiment_table_is_still_tolerated_by_schema_create_all(tmp_path):
    db_path = tmp_path / "old_swing_trader.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE reddit_sentiment (id INTEGER PRIMARY KEY, ticker_id INTEGER NOT NULL, date DATE NOT NULL)")
        conn.commit()

    engine = __import__("sqlalchemy").create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)

    with sqlite3.connect(db_path) as conn:
        table_names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    assert "reddit_sentiment" in table_names
