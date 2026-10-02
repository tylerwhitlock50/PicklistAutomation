"""SQL Server (VISUAL) engines and query-file execution."""
import threading
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import create_engine, text

from picklist.config import logger
from picklist.db import get_config_value
from picklist.util import mask_connection_string


# One pooled engine per connection string for the process lifetime: building
# an engine per query paid engine construction plus a fresh ODBC login on
# every request. Keyed by connection string so a settings change takes effect
# without a restart.
_erp_engines: dict[str, Any] = {}


_erp_engines_lock = threading.Lock()


def get_erp_engine():
    mssql_conn_string = get_config_value(
        setting_key="mssql_connection_string",
        env_key="MSSQL_CONNECTION_STRING",
    )
    if not mssql_conn_string:
        raise ValueError(
            "MSSQL_CONNECTION_STRING is not set. Configure it in Settings or in your .env file."
        )
    with _erp_engines_lock:
        engine = _erp_engines.get(mssql_conn_string)
        if engine is None:
            logger.info(
                "Connecting to SQL Server using %s",
                mask_connection_string(mssql_conn_string),
            )
            engine = create_engine(mssql_conn_string, pool_pre_ping=True, pool_recycle=1800)
            _erp_engines[mssql_conn_string] = engine
    return engine


def get_erp_write_engine():
    """Engine for the one ERP write path (Promise Del Date saves).

    Uses mssql_write_connection_string / MSSQL_WRITE_CONNECTION_STRING when
    configured — intended to be a dedicated login whose only write right is a
    column-level UPDATE grant on CUST_ORDER_LINE.PROMISE_DEL_DATE — and falls
    back to the read connection until that login exists. Shares the engine
    cache, so a settings change takes effect without a restart.
    """
    write_conn_string = get_config_value(
        setting_key="mssql_write_connection_string",
        env_key="MSSQL_WRITE_CONNECTION_STRING",
    )
    if not write_conn_string:
        return get_erp_engine()
    with _erp_engines_lock:
        engine = _erp_engines.get(write_conn_string)
        if engine is None:
            logger.info(
                "Connecting to SQL Server (write) using %s",
                mask_connection_string(write_conn_string),
            )
            engine = create_engine(write_conn_string, pool_pre_ping=True, pool_recycle=1800)
            _erp_engines[write_conn_string] = engine
    return engine


def run_audit_sql_file(query_file: Path, description: str) -> pd.DataFrame:
    """Run one of the serialized-audit SQL files (no parameters) against SQL Server.

    Sent as a raw string, not sqlalchemy.text(), so literal colons in the file
    can never be misparsed as bind parameters.
    """
    if not query_file.exists():
        raise FileNotFoundError(f"Audit query file not found at: {query_file}")
    query = query_file.read_text(encoding="utf-8")
    engine = get_erp_engine()
    logger.info("Running %s", description)
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
        logger.info("%s returned %d rows.", description, len(df.index))
        return df


def run_erp_query_file(query_file: Path, params: dict, description: str) -> pd.DataFrame:
    """Run a parameterized read-only ERP query from a .sql file.

    Unlike run_audit_sql_file, user-supplied values go in as bound parameters
    (:name placeholders via sqlalchemy.text) — never string interpolation.
    """
    if not query_file.exists():
        raise FileNotFoundError(f"ERP query file not found at: {query_file}")
    query = query_file.read_text(encoding="utf-8")
    engine = get_erp_engine()
    logger.info("Running %s", description)
    with engine.connect() as connection:
        df = pd.read_sql_query(text(query), connection, params=params)
        logger.info("%s returned %d rows.", description, len(df.index))
        return df
