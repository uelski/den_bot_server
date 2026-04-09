"""db.py — SQLAlchemy engine singleton for Postgres."""

import os

from sqlalchemy import create_engine

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(os.environ["POSTGRES_URL"], pool_size=5, max_overflow=2)
    return _engine
