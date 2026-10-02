"""Small helpers shared across the package."""
from datetime import datetime
from decimal import Decimal
from typing import Optional


def parse_bool(value: Optional[str], default: bool = False) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def mask_connection_string(connection_string: str) -> str:
    if "://" not in connection_string:
        return "<invalid-connection-string>"

    scheme, rest = connection_string.split("://", maxsplit=1)
    if "@" in rest:
        credentials, host_part = rest.split("@", maxsplit=1)
        if ":" in credentials:
            username, _ = credentials.split(":", maxsplit=1)
            return f"{scheme}://{username}:***@{host_part}"
        return f"{scheme}://***@{host_part}"
    return f"{scheme}://{rest}"


def sql_quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _audit_json_safe(value):
    """Recursively convert analytics rows to JSON-friendly types."""
    if isinstance(value, dict):
        return {k: _audit_json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_audit_json_safe(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value
