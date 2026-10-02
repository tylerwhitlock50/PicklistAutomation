"""Allocation inputs, payload building and the promise-date write SQL."""
from datetime import date
from typing import Any, Optional

from picklist.config import ALLOC_DEMAND_FILE, ALLOC_PART_ID_MAX_LENGTH, ALLOC_SUPPLY_FILE
from picklist.domain import allocation
from picklist.erp import run_erp_query_file
from picklist.services.query_options import get_default_guns_query_options


# The app's only ERP write. UPDLOCK on the SELECT holds the row for the
# duration of the transaction; the UPDATE's old-value predicate is the
# optimistic-concurrency check against edits made before we loaded the screen.
# CAST(... AS date): the UI only ever saw date precision, so a stray time
# component in the column must not read as a conflict.
ALLOC_SELECT_LINE_SQL = """
SELECT PART_ID, CAST(PROMISE_DEL_DATE AS date) AS PROMISE_DEL_DATE
FROM dbo.CUST_ORDER_LINE WITH (UPDLOCK, ROWLOCK)
WHERE CUST_ORDER_ID = :so AND LINE_NO = :line
"""


ALLOC_UPDATE_SQL = """
UPDATE dbo.CUST_ORDER_LINE
SET PROMISE_DEL_DATE = :new_value
WHERE CUST_ORDER_ID = :so AND LINE_NO = :line
  AND ((CAST(PROMISE_DEL_DATE AS date) = :old_value)
       OR (PROMISE_DEL_DATE IS NULL AND :old_value IS NULL))
"""


def _clean_part_id(value: Optional[str]) -> str:
    return (value or "").strip().upper()[:ALLOC_PART_ID_MAX_LENGTH]


def _allocation_options() -> dict[str, Any]:
    """Lookahead + excluded-customer terms, same source as the guns picklist."""
    options = get_default_guns_query_options()
    return {
        "lookahead_days": options["lookahead_days"],
        "excluded_customers": tuple(options["excluded_customers"]),
    }


def _fetch_allocation_inputs(part_id: str) -> tuple[list[dict], list[dict]]:
    supply_df = run_erp_query_file(
        ALLOC_SUPPLY_FILE, {"part_id": part_id}, f"allocation supply for {part_id}"
    )
    demand_df = run_erp_query_file(
        ALLOC_DEMAND_FILE, {"part_id": part_id}, f"allocation demand for {part_id}"
    )
    return (
        supply_df.to_dict(orient="records"),
        demand_df.to_dict(orient="records"),
    )


def _build_allocation_payload(
    part_id: str,
    overrides: Optional[dict] = None,
) -> dict:
    options = _allocation_options()
    supply_rows, demand_rows = _fetch_allocation_inputs(part_id)
    payload = allocation.build_allocation(
        supply_rows,
        demand_rows,
        date.today(),
        options["lookahead_days"],
        excluded_customer_terms=options["excluded_customers"],
        overrides=overrides,
    )
    payload["part_id"] = part_id
    return payload


def _parse_iso_date_field(payload: dict, field: str) -> tuple[Optional[date], Optional[str]]:
    """(value, error). None is a legal value for both promise-del fields."""
    raw = payload.get(field)
    if raw in (None, ""):
        return None, None
    try:
        return date.fromisoformat(str(raw)), None
    except ValueError:
        return None, f"{field} must be a YYYY-MM-DD date."


class _AllocationSaveConflict(Exception):
    def __init__(self, current_value: Optional[date]):
        super().__init__("Promise Del Date changed since the screen was loaded.")
        self.current_value = current_value
