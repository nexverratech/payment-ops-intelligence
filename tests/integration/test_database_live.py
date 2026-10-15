"""Talks to the real PostgreSQL container. Skipped automatically if it is not running.

Start it first:  docker compose up -d db
tests/conftest.py passes your .env database values through to these tests.
"""

import pytest

from payops import health
from payops.config import get_settings

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def settings():
    s = get_settings()
    if health.check_database(s).status != "ok":
        pytest.skip("PostgreSQL is not reachable (run: docker compose up -d db)")
    return s


def test_database_health_is_ok(settings):
    result = health.check_database(settings)
    assert result.status == "ok"
    assert result.latency_ms is not None


def test_server_is_postgres_16_or_newer(settings):
    from sqlalchemy import text

    from payops.db.connection import get_engine

    with get_engine(settings).connect() as conn:
        version_num = int(conn.execute(text("SHOW server_version_num")).scalar_one())
    assert version_num >= 160000
