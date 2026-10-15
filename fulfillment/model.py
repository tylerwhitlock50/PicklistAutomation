"""App-owned persistence; PostgreSQL is required outside tests/demo.

The warehouse-sized command workload uses one transactional mutex. This makes
allocation across orders, bins and serials atomic without deadlock-prone lock
ordering. No process-local lock is used for correctness.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import (JSON, Boolean, Column, Integer, MetaData, Numeric,
                        String, Table, Text, create_engine, select, text)

metadata = MetaData()
SCHEMA_VERSION = 3
mutex = Table("fulfillment_mutex", metadata, Column("id", Integer, primary_key=True))
schema = Table("fulfillment_schema", metadata, Column("version", Integer, primary_key=True))
users = Table("fulfillment_users", metadata,
              Column("id", String(80), primary_key=True), Column("name", String(120), nullable=False),
              Column("pin_hash", Text, nullable=False), Column("role", String(20), nullable=False),
              Column("enabled", Boolean, nullable=False, default=True))
sessions = Table("fulfillment_sessions", metadata,
                 Column("token_hash", String(64), primary_key=True), Column("user_id", String(80), nullable=False),
                 Column("expires", String(40), nullable=False))
attempts = Table("fulfillment_login_attempts", metadata,
                 Column("key", String(160), primary_key=True), Column("count", Integer, nullable=False),
                 Column("window", String(40), nullable=False))
works = Table("fulfillments", metadata,
              Column("id", String(40), primary_key=True), Column("state", String(40), nullable=False),
              Column("version", Integer, nullable=False), Column("document", JSON, nullable=False),
              Column("updated_at", String(40), nullable=False))
# Claim keys include order, cart, operator, trace identity, packlist and tracking.
claims = Table("fulfillment_claims", metadata,
               Column("key", String(400), primary_key=True), Column("work_id", String(40), nullable=False))
events = Table("fulfillment_events", metadata,
               Column("id", String(40), primary_key=True), Column("work_id", String(40)),
               Column("at", String(40), nullable=False), Column("actor", String(80), nullable=False),
               Column("action", String(80), nullable=False), Column("detail", JSON, nullable=False))
requests = Table("fulfillment_requests", metadata,
                 Column("key", String(220), primary_key=True), Column("digest", String(64), nullable=False),
                 Column("response", JSON, nullable=False), Column("status", Integer, nullable=False))
labels = Table("fulfillment_labels", metadata,
               Column("tracking", String(80), primary_key=True), Column("document", JSON, nullable=False),
               Column("received_at", String(40), nullable=False))
settings = Table("fulfillment_settings", metadata,
                 Column("key", String(100), primary_key=True), Column("value", JSON, nullable=False))


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


class Database:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True,
                                    **({"connect_args": {"check_same_thread": False, "timeout": 30}}
                                       if url.startswith("sqlite") else {}))

    def initialize(self):
        # Explicit CLI migration, never run by web workers.
        metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            if not conn.execute(select(mutex)).first():
                conn.execute(mutex.insert().values(id=1))
            if not conn.execute(select(schema)).first():
                conn.execute(schema.insert().values(version=SCHEMA_VERSION))
            else:
                current = conn.execute(select(schema.c.version)).scalar_one()
                if current not in (1, 2, SCHEMA_VERSION):
                    raise RuntimeError("Unsupported Dispatch schema version; use the matching application.")
                if current != SCHEMA_VERSION:
                    conn.execute(schema.update().values(version=SCHEMA_VERSION))
            if get_setting(conn, "administration") is None:
                from fulfillment.admin import initial_state
                put_setting(conn, "administration", initial_state())
            if self.engine.dialect.name == "postgresql":
                from fulfillment.audit.store import initialize
                initialize(conn)

    @contextmanager
    def transaction(self):
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                conn.execute(text("BEGIN IMMEDIATE"))
            else:
                conn.begin()
                conn.execute(select(mutex).where(mutex.c.id == 1).with_for_update()).one()
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise


def put_setting(conn, key, value):
    conn.execute(settings.delete().where(settings.c.key == key))
    conn.execute(settings.insert().values(key=key, value=value))


def get_setting(conn, key, default=None):
    row = conn.execute(select(settings.c.value).where(settings.c.key == key)).first()
    return row[0] if row else default
