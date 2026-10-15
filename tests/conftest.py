"""Shared test setup.

1. Test settings are put into the environment BEFORE the app is imported,
   so tests never depend on (or modify) your real .env file.
2. A network guard blocks every connection to a non-local host for the whole
   test run. If any code tries to reach the internet, its test fails.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _read_dotenv(path: Path) -> dict[str, str]:
    """Minimal KEY=VALUE reader, so integration tests can reach your real database."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_dotenv = _read_dotenv(PROJECT_ROOT / ".env")


def _db_value(key: str, default: str) -> str:
    # Order: real environment variable, then your .env file, then a test default.
    return os.environ.get(key) or _dotenv.get(key) or default


# Environment variables take priority over .env in pydantic-settings,
# so setting them here fully controls what the app sees during tests.
_TEST_ENV = {
    "APP_ENV": "test",
    "LOG_LEVEL": "WARNING",
    "POSTGRES_HOST": _db_value("POSTGRES_HOST", "127.0.0.1"),
    "POSTGRES_PORT": _db_value("POSTGRES_PORT", "5432"),
    "POSTGRES_DB": _db_value("POSTGRES_DB", "payops"),
    "POSTGRES_USER": _db_value("POSTGRES_USER", "payops_admin"),
    "POSTGRES_PASSWORD": _db_value("POSTGRES_PASSWORD", "test-password-not-secret"),
    "READONLY_DB_USER": _db_value("READONLY_DB_USER", "payops_readonly"),
    "READONLY_DB_PASSWORD": _db_value("READONLY_DB_PASSWORD", "test-readonly-password"),
    # Empty = history disabled; unit tests use an in-memory store instead.
    "APP_DB_USER": _db_value("APP_DB_USER", "payops_app"),
    "APP_DB_PASSWORD": _db_value("APP_DB_PASSWORD", ""),
    "OLLAMA_BASE_URL": "http://127.0.0.1:11434",
    "OLLAMA_MODEL": "qwen3:4b",
    "HEALTH_CHECK_TIMEOUT_SECONDS": "2",
}
os.environ.update(_TEST_ENV)

from payops.netpolicy import install_network_guard  # noqa: E402  (must follow env setup)


@pytest.fixture(autouse=True, scope="session")
def block_outbound_network():
    uninstall = install_network_guard()
    yield
    uninstall()


@pytest.fixture(autouse=True)
def cheap_password_hashing(monkeypatch):
    """Production hashing costs ~0.5 s and 128 MiB per password (OWASP minimum for scrypt).
    Tests use the same code with a small N so the suite stays fast; test_passwords.py
    checks that production really uses the OWASP parameters."""
    from payops.security import passwords

    monkeypatch.setattr(passwords, "CURRENT", passwords.ScryptParams(log2_n=10, r=8, p=1))
