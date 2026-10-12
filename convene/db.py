"""Database connection.

SQLite by default (one file, nothing to install), or PostgreSQL by giving a
URL such as  postgresql+psycopg://user:password@localhost/convene.

For SQLite two settings matter:

* Foreign keys are switched on (SQLite leaves them off unless asked).
* Every transaction starts with BEGIN IMMEDIATE, which takes the write lock
  at the start. Without it, two people booking the last seat at the same
  moment could both read "1 seat left" and both get confirmed. With it, the
  second booking waits until the first has finished, then sees 0 seats.
  (On PostgreSQL the same job is done by SELECT ... FOR UPDATE on the event.)
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from .models import Base


def make_engine(url="sqlite:///convene.db"):
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"timeout": 30, "check_same_thread": False})

        @event.listens_for(engine, "connect")
        def _sqlite_setup(dbapi_conn, _record):
            dbapi_conn.isolation_level = None          # let us issue BEGIN ourselves
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys = ON")
            cur.execute("PRAGMA journal_mode = WAL")   # readers don't block the writer
            cur.close()

        @event.listens_for(engine, "begin")
        def _begin_immediate(conn):
            conn.exec_driver_sql("BEGIN IMMEDIATE")
        return engine
    # PostgreSQL (or another server database): check pooled connections are alive before use
    return create_engine(url, pool_pre_ping=True)


def init_db(url="sqlite:///convene.db"):
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    return engine


def session_factory(engine):
    # expire_on_commit=False: objects returned by the service keep their values
    # after the transaction ends, so callers can read them safely.
    return sessionmaker(engine, expire_on_commit=False)
