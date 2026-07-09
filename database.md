# SQL Server Connection Template

A reusable pattern for connecting to Microsoft SQL Server from Python. Based on how `fetch_picklist_from_mssql()` works in `app.py:746`, generalized so it can be lifted into any script or service.

## Dependencies

```
SQLAlchemy>=2.0
pyodbc>=5.1     # or pymssql if ODBC isn't available
pandas          # only if you want DataFrames back
```

Plus the **ODBC Driver for SQL Server** installed on the host (Microsoft ships drivers 17 and 18). On Linux/Docker, install via the `msodbcsql18` package; on Windows it's an MSI from Microsoft.

## Connection string

SQLAlchemy URL format for pyodbc:

```
mssql+pyodbc://USER:PASSWORD@HOST[:PORT]/DATABASE?driver=ODBC+Driver+18+for+SQL+Server&TrustServerCertificate=yes
```

Common query parameters:

| Param                     | When to use                                                          |
| ------------------------- | -------------------------------------------------------------------- |
| `driver=...`              | Required. URL-encode spaces as `+`.                                  |
| `TrustServerCertificate`  | `yes` for self-signed certs (typical on internal SQL Servers).       |
| `Encrypt=yes`             | Force TLS. Default in driver 18.                                     |
| `Authentication=ActiveDirectoryIntegrated` | Windows/Entra ID auth instead of user/password.    |
| `MARS_Connection=yes`     | If you need multiple active result sets on one connection.           |

Store the full string in an environment variable (e.g. `MSSQL_CONNECTION_STRING`). Never commit it.

## The template

Drop this in a `db.py` (or paste inline). It gives you:

- A **cached engine** (one per process, one per connection string).
- A **context manager** that hands out connections.
- A **credential-masking helper** for safe logging.
- A **DataFrame-shaped query helper** built on top.

```python
"""SQL Server connection helpers."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from typing import Iterator, Optional

import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

logger = logging.getLogger(__name__)

_engine_cache: dict[str, Engine] = {}


def _resolve_connection_string(override: Optional[str] = None) -> str:
    conn_string = override or os.getenv("MSSQL_CONNECTION_STRING", "").strip()
    if not conn_string:
        raise ValueError(
            "MSSQL_CONNECTION_STRING is not set. "
            "Pass it explicitly or set the env var."
        )
    return conn_string


def mask_connection_string(conn_string: str) -> str:
    """Return a log-safe version of a SQLAlchemy URL with the password redacted."""
    if "://" not in conn_string:
        return "<invalid-connection-string>"
    scheme, rest = conn_string.split("://", 1)
    if "@" not in rest:
        return f"{scheme}://{rest}"
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user, _ = creds.split(":", 1)
        return f"{scheme}://{user}:***@{host}"
    return f"{scheme}://***@{host}"


def get_engine(conn_string: Optional[str] = None) -> Engine:
    """Return a cached SQLAlchemy engine for the given connection string."""
    resolved = _resolve_connection_string(conn_string)
    engine = _engine_cache.get(resolved)
    if engine is None:
        logger.info("Creating SQL Server engine for %s", mask_connection_string(resolved))
        engine = create_engine(
            resolved,
            pool_pre_ping=True,   # drop dead connections automatically
            pool_recycle=1800,    # recycle every 30 min (avoids server-side idle timeouts)
            future=True,
        )
        _engine_cache[resolved] = engine
    return engine


@contextmanager
def connect(conn_string: Optional[str] = None) -> Iterator[Connection]:
    """Yield a live connection. Commits on success, rolls back on error."""
    engine = get_engine(conn_string)
    with engine.begin() as conn:
        yield conn


def query_df(
    sql: str,
    params: Optional[dict] = None,
    conn_string: Optional[str] = None,
) -> pd.DataFrame:
    """Run a SELECT and return the result as a DataFrame."""
    with connect(conn_string) as conn:
        return pd.read_sql_query(text(sql), conn, params=params)


def dispose_all() -> None:
    """Close pooled connections — call on shutdown if you care."""
    for engine in _engine_cache.values():
        engine.dispose()
    _engine_cache.clear()
```

## Usage

```python
from db import query_df, connect

# Simple read into a DataFrame
df = query_df("SELECT TOP 100 * FROM Orders WHERE CustomerID = :cid", {"cid": 42})

# Multi-statement / write — get a raw connection, transactions auto-commit on exit
with connect() as conn:
    conn.execute(text("UPDATE Orders SET Status = 'shipped' WHERE Id = :id"), {"id": 7})
    rows = conn.execute(text("SELECT COUNT(*) FROM Orders")).scalar_one()

# Loading a query from a .sql file
with open("sql/query_guns.sql", encoding="utf-8") as f:
    df = query_df(f.read())
```

## Why these defaults

- **Engine cache.** `create_engine` builds a pool. Calling it per request throws the pool away. One engine per process, keyed by connection string so a settings-table override still picks up changes.
- **`pool_pre_ping=True`.** SQL Server (and any firewall in front of it) drops idle connections silently. Pre-ping turns that into a one-roundtrip recovery instead of a failed query.
- **`pool_recycle=1800`.** Belt-and-braces for the same problem — refreshes connections before the server gives up on them.
- **`engine.begin()` over `engine.connect()`.** `begin()` opens a transaction and commits on clean exit. With `connect()` you have to remember to commit yourself.
- **`text(sql)` with named params.** Lets the driver bind parameters safely. Don't f-string user input into SQL.
- **No global connection.** Connections aren't thread-safe; pools are. Get one per unit of work.

## Optional add-ons

- **Settings-table fallback.** If you have a UI that lets users edit the connection string, write a `_resolve_connection_string` that checks the settings table first, then env. Keep the rest of the file unchanged.
- **Encrypted secrets.** Wrap the resolved string in Fernet decrypt if you store it encrypted at rest (see `decrypt_setting_value` in `app.py:376`).
- **Read-only role.** Append `&ApplicationIntent=ReadOnly` to route reads to a replica if you have AlwaysOn configured.
- **Connection retries.** SQLAlchemy doesn't retry failed queries; wrap `query_df` with `tenacity` if you need that.
