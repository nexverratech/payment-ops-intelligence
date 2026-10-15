"""Creates the shared SQLAlchemy engine (a pool of database connections).

Opening a PostgreSQL connection is slow, so the engine keeps a few open and
reuses them. `pool_pre_ping` checks a connection is still alive before
handing it out (useful when the Docker container restarts).
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from payops.config import Settings


def get_engine(settings: Settings) -> Engine:
    """Return the engine for these settings, creating it only once."""
    return _engine_for(settings.database_url(), int(max(1, settings.health_check_timeout_seconds)))


@lru_cache(maxsize=4)
def _engine_for(url: URL, connect_timeout: int) -> Engine:
    # Cached by URL so the same pool is reused. (Settings objects themselves
    # are not hashable, so they cannot be the cache key.)
    return create_engine(
        url,
        pool_size=5,
        max_overflow=5,
        pool_pre_ping=True,
        # Give up quickly if the database is not running, instead of hanging.
        connect_args={"connect_timeout": connect_timeout},
    )
