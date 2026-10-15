"""Configuration rules that keep the app local and offline, and constants in sync with the schema."""

import re
import tomllib
from pathlib import Path

from payops.analytics.models import FAILURE_REASONS, PAYMENT_METHODS
from payops.config import Settings

ROOT = Path(__file__).resolve().parents[1]


def test_streamlit_config_is_local_and_sends_no_usage_statistics():
    cfg = tomllib.loads((ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert cfg["browser"]["gatherUsageStats"] is False
    assert cfg["server"]["address"] == "127.0.0.1"
    assert cfg["server"]["headless"] is True


def test_dashboard_api_address_must_be_local():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, postgres_password="x", readonly_db_password="y", api_base_url="https://api.example.com")


def _check_values(column: str) -> set[str]:
    schema = (ROOT / "src" / "payops" / "db" / "schema.sql").read_text(encoding="utf-8")
    block = re.search(rf"{column}\s+TEXT[^,]*?CHECK \({column} IN \((.*?)\)\)", schema, re.S).group(1)
    return set(re.findall(r"'([A-Z_]+)'", block))


def test_payment_methods_match_the_database_schema():
    assert set(PAYMENT_METHODS) == _check_values("payment_method")


def test_failure_reasons_match_the_database_schema():
    assert set(FAILURE_REASONS) == _check_values("failure_reason")
