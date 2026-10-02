"""Picklist query selection, option parsing and SQL rendering."""
import json
import re
from typing import Any, Optional

from picklist.config import (
    DEFAULT_QUERY_TYPE,
    GUNS_BASE_EXCLUDED_CUSTOMERS,
    GUNS_DEFAULT_LOOKAHEAD_DAYS,
    GUNS_EXCLUDED_CUSTOMERS_TOKEN,
    GUNS_LOOKAHEAD_TOKEN,
    GUNS_MAX_ADDITIONAL_CUSTOMERS,
    GUNS_MAX_LOOKAHEAD_DAYS,
    QUERY_FILES,
    RELEASE_GATE_FILTER_TOKEN,
)
from picklist.util import sql_quote_literal


def get_query_type(value: Optional[str]) -> str:
    if value in QUERY_FILES:
        return value
    return DEFAULT_QUERY_TYPE


def normalize_customer_term(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip()).upper()


def get_default_guns_query_options() -> dict[str, Any]:
    return {
        "lookahead_days": GUNS_DEFAULT_LOOKAHEAD_DAYS,
        "base_excluded_customers": list(GUNS_BASE_EXCLUDED_CUSTOMERS),
        "additional_excluded_customers": [],
        "excluded_customers": list(GUNS_BASE_EXCLUDED_CUSTOMERS),
        "has_overrides": False,
    }


def parse_guns_lookahead_days(raw_value: Any) -> int:
    if raw_value is None or raw_value == "":
        return GUNS_DEFAULT_LOOKAHEAD_DAYS

    try:
        lookahead_days = int(str(raw_value).strip())
    except (TypeError, ValueError) as exc:
        raise ValueError("Lookahead days must be a whole number.") from exc

    if lookahead_days < 1 or lookahead_days > GUNS_MAX_LOOKAHEAD_DAYS:
        raise ValueError(
            f"Lookahead days must be between 1 and {GUNS_MAX_LOOKAHEAD_DAYS}."
        )
    return lookahead_days


def parse_additional_excluded_customers(raw_value: Any) -> list[str]:
    if raw_value is None or raw_value == "" or raw_value == []:
        return []

    parsed_value = raw_value
    if isinstance(raw_value, str):
        try:
            parsed_value = json.loads(raw_value)
        except json.JSONDecodeError:
            parsed_value = [item.strip() for item in raw_value.split(",")]

    if not isinstance(parsed_value, list):
        raise ValueError("Excluded customers must be provided as a list.")

    base_terms = {normalize_customer_term(value) for value in GUNS_BASE_EXCLUDED_CUSTOMERS}
    normalized_terms: list[str] = []
    seen_terms: set[str] = set()

    for item in parsed_value:
        if not isinstance(item, str):
            raise ValueError("Excluded customers must be text values.")
        normalized = normalize_customer_term(item)
        if not normalized or normalized in base_terms or normalized in seen_terms:
            continue
        normalized_terms.append(normalized)
        seen_terms.add(normalized)

    if len(normalized_terms) > GUNS_MAX_ADDITIONAL_CUSTOMERS:
        raise ValueError(
            f"You can exclude up to {GUNS_MAX_ADDITIONAL_CUSTOMERS} additional customers."
        )

    return normalized_terms


def build_guns_query_options(
    raw_lookahead_days: Any = None,
    raw_additional_customers: Any = None,
) -> dict[str, Any]:
    lookahead_days = parse_guns_lookahead_days(raw_lookahead_days)
    additional_customers = parse_additional_excluded_customers(raw_additional_customers)
    excluded_customers = [*GUNS_BASE_EXCLUDED_CUSTOMERS, *additional_customers]
    return {
        "lookahead_days": lookahead_days,
        "base_excluded_customers": list(GUNS_BASE_EXCLUDED_CUSTOMERS),
        "additional_excluded_customers": additional_customers,
        "excluded_customers": excluded_customers,
        "has_overrides": (
            lookahead_days != GUNS_DEFAULT_LOOKAHEAD_DAYS
            or bool(additional_customers)
        ),
    }


def parse_query_run_options(query_type: str, payload: Any) -> dict[str, Any]:
    if query_type != "guns":
        return {}

    raw_additional_customers = payload.get("guns_excluded_customers")
    if raw_additional_customers is None or raw_additional_customers == "":
        raw_additional_customers = payload.get("guns_excluded_customers_json")

    return build_guns_query_options(
        raw_lookahead_days=payload.get("guns_lookahead_days"),
        raw_additional_customers=raw_additional_customers,
    )


def render_guns_query(
    query_template: str,
    query_options: Optional[dict[str, Any]] = None,
) -> str:
    resolved_options = get_default_guns_query_options()
    if query_options:
        resolved_options = {**resolved_options, **query_options}

    excluded_customer_rows = "\n    UNION ALL\n    ".join(
        f"SELECT {sql_quote_literal(customer)} AS CUSTOMER_TERM"
        for customer in resolved_options["excluded_customers"]
    )
    return (
        query_template.replace(GUNS_LOOKAHEAD_TOKEN, str(resolved_options["lookahead_days"]))
        .replace(GUNS_EXCLUDED_CUSTOMERS_TOKEN, excluded_customer_rows)
    )


def load_query(query_type: str, query_options: Optional[dict[str, Any]] = None) -> str:
    query_file = QUERY_FILES[query_type]
    if not query_file.exists():
        raise FileNotFoundError(f"Query file not found at: {query_file}")
    query_template = query_file.read_text(encoding="utf-8")
    if query_type == "guns":
        return render_guns_query(query_template, query_options=query_options)
    return query_template


def apply_release_gate_filter(query: str, gate_payload: Optional[dict]) -> str:
    """Inject the released order set before the SQL allocation CTE runs."""
    if RELEASE_GATE_FILTER_TOKEN not in query:
        return query
    if not gate_payload or gate_payload.get("mode") != "enforced":
        return query.replace(RELEASE_GATE_FILTER_TOKEN, "")
    released = sorted(
        {
            str(order_id).strip().upper()
            for order_id in gate_payload.get("released_orders") or []
            if str(order_id).strip()
        }
    )
    if not released:
        clause = "AND 1 = 0  -- release gate: no orders currently eligible"
    else:
        # Accepted trade-off: escaped literals in an IN list (read-only query,
        # bounded by the open-order count; both picklist queries already run
        # with OPTION (RECOMPILE), so plan-reuse loss is moot).
        literals = ", ".join(sql_quote_literal(order_id) for order_id in released)
        clause = f"AND co.ID IN ({literals})  -- release gate policy v{gate_payload.get('policy_version', 1)}"
    return query.replace(RELEASE_GATE_FILTER_TOKEN, clause)
