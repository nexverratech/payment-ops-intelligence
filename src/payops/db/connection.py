"""Creates the shared SQLAlchemy engines (pools of database connections).

Opening a PostgreSQL connection is slow, so an engine keeps a few open and
reuses them. `pool_pre_ping` checks a connection is still alive before
handing it out (useful when the Docker container restarts).

Two engines exist:
* get_engine(settings)          -> owner account. Only for health checks and admin tasks.
* get_readonly_engine(settings) -> read-only account. Used for ALL analytics and agent queries.
  The database itself refuses writes from this account, so a bug or a
  misbehaving agent cannot change payment data.
"""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL

from payops.config import Settings


def get_engine(settings: Settings) -> Engine:
    """Engine for the owner account."""
    return _engine_for(settings.database_url(), _timeout(settings))


def get_readonly_engine(settings: Settings) -> Engine:
    """Engine for the read-only account (use this for every analytical query)."""
    return _engine_for(settings.database_url(readonly=True), _timeout(settings))


def _timeout(settings: Settings) -> int:
    return int(max(1, settings.health_check_timeout_seconds))


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
