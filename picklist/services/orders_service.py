"""Order readiness data access: candidates, detail, documents, shipments, stock."""
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from flask import request
from sqlalchemy import text

from picklist.config import (
    ALLOC_PARTS_FILE,
    logger,
    ORDER_DETAIL_FILE,
    ORDER_DOCUMENTS_FILE,
    ORDER_PART_LOCATIONS_FILE,
    ORDER_SHIPMENTS_FILE,
    QUERY_FILES,
    READINESS_CANDIDATES_FILE,
    READINESS_LOOKAHEAD_DAYS,
    SHIPMENTS_LOOKUP_FILE,
    SHIPMENTS_LOOKUP_MAX_DAYS,
    STOCK_BY_PART_FILE,
)
from picklist.domain import ffl_docs, shipments, stock
from picklist.erp import get_erp_engine, run_erp_query_file
from picklist.services.allocation_service import _build_allocation_payload
from picklist.services.run_history import get_latest_successful_run, parse_run_timestamp
from picklist.services.settings_service import get_ffl_doc_config, get_release_gate_mode
from picklist.services.shipping_service import (
    _active_manual_holds,
    build_release_gate_payload,
    render_release_candidates_query,
)
from picklist.stores import pick_store, readiness_store, shipping_store
from picklist.timeutil import _today_local, resolve_timezone


def fetch_order_documents(order_id: str) -> list[dict]:
    df = run_erp_query_file(ORDER_DOCUMENTS_FILE, {"so": str(order_id).strip().upper()}, f"order documents {order_id}")
    return df.to_dict(orient="records")


def _readiness_doc_findings(orders: list[dict]) -> dict[str, list[dict]]:
    """Tier-2 FFL document comparison for the readiness refresh (flag-gated)."""
    cfg = get_ffl_doc_config()
    if not cfg["enabled"]:
        return {}
    ffl_docs.configure(
        path_map=cfg["path_map"],
        roots=cfg["roots"],
        ocr_enabled=True,
        ocr_dpi=cfg["ocr_dpi"],
        ocr_pages=cfg["ocr_pages"],
        max_docs_per_run=cfg["max_docs_per_run"],
        name_threshold=cfg["name_threshold"],
        addr_threshold=cfg["addr_threshold"],
        logger=logger,
    )
    return ffl_docs.findings_for_orders(
        orders,
        fetch_documents=fetch_order_documents,
        cache_get=readiness_store.get_doc_cache,
        cache_put=readiness_store.put_doc_cache,
    )


def _order_documents_view(order_id: str) -> list[dict]:
    """Attachments for the order page, classified; never fails the page."""
    try:
        rows = fetch_order_documents(order_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Order documents lookup failed for %s: %s", order_id, exc)
        return []
    out = []
    for row in rows:
        document_id = str(row.get("DOCUMENT_ID") or "").strip()
        folder = str(row.get("DOC_FILE_PATH") or "").strip()
        created = row.get("CREATE_DATE")
        out.append({
            "document_id": document_id,
            "folder": folder,
            "kind": ffl_docs.classify_kind(document_id, folder),
            "description": str(row.get("DESCRIPTION") or "").strip() or None,
            "created": created.strftime("%Y-%m-%d") if hasattr(created, "strftime") else (str(created)[:10] if created else None),
        })
    return out


def _render_readiness_sql(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Readiness query not found at: {path}")
    template = path.read_text(encoding="utf-8")
    return render_release_candidates_query(
        template, query_options={"lookahead_days": READINESS_LOOKAHEAD_DAYS}
    )


def fetch_readiness_candidate_rows() -> list[dict]:
    query = _render_readiness_sql(READINESS_CANDIDATES_FILE)
    engine = get_erp_engine()
    logger.info("Running order readiness candidate query")
    with engine.connect() as connection:
        df = pd.read_sql_query(query, connection)
    logger.info("Order readiness candidate query returned %d rows.", len(df.index))
    return df.to_dict(orient="records")


def fetch_order_detail_rows(order_id: str) -> list[dict]:
    query = _render_readiness_sql(ORDER_DETAIL_FILE)
    engine = get_erp_engine()
    logger.info("Running order detail query for %s", order_id)
    with engine.connect() as connection:
        df = pd.read_sql_query(text(query), connection, params={"so": order_id})
    return df.to_dict(orient="records")


def fetch_order_part_locations(order_id: str) -> list[dict]:
    df = run_erp_query_file(
        ORDER_PART_LOCATIONS_FILE, {"so": order_id}, f"order part locations query ({order_id})"
    )
    return df.to_dict(orient="records")


def _picklist_orders_today() -> Optional[set[str]]:
    """Order ids on today's latest guns + components runs; None when no run today."""
    today = _today_local()
    orders: set[str] = set()
    found = False
    for query_type in QUERY_FILES:
        run, rows = get_latest_successful_run(query_type=query_type)
        if not run:
            continue
        try:
            run_day = parse_run_timestamp(run["run_timestamp"]).astimezone(resolve_timezone()).date()
        except Exception:  # noqa: BLE001
            continue
        if run_day != today:
            continue
        found = True
        for row in rows:
            order_id = str(row.get("Cust Order ID") or "").strip().upper()
            if order_id:
                orders.add(order_id)
    return orders if found else None


def _readiness_gate_decisions() -> dict[str, dict]:
    mode = get_release_gate_mode()
    if mode == "off":
        return {}
    payload = build_release_gate_payload()
    if not payload or payload.get("error"):
        return {}
    # Decisions always flow through so the order page can show them; only an
    # enforced gate turns a HOLD / ACCUMULATING decision into a readiness hold.
    return {
        str(row.get("order_id") or "").upper(): {**row, "mode": mode, "enforced": mode == "enforced"}
        for row in payload.get("decisions") or []
        if row.get("order_id")
    }


def _readiness_pick_status(order_id: str) -> dict[str, Any]:
    claimed = order_id in pick_store.claimed_orders()
    ready = [
        row for row in pick_store.ready_for_pack_orders(limit=500)
        if str(row.get("cust_order_id") or "").upper() == order_id
    ]
    return {
        "claimed": claimed,
        "ready_for_pack": bool(ready),
        "packlist_id": next((row.get("packlist_id") for row in ready if row.get("packlist_id")), None),
        "operator": next((row.get("operator") for row in ready if row.get("operator")), None),
    }


def _readiness_blocking_holds(order_id: str) -> list[dict]:
    return [
        row for row in readiness_store.open_holds(cust_order_id=order_id)
        if row.get("blocking")
    ]


def _apply_manual_hold_exclusions(df: pd.DataFrame) -> pd.DataFrame:
    """Drop picklist rows for orders under an active manual hold; note them on df.attrs."""
    order_column = "Cust Order ID"
    held = {str(h.get("cust_order_id") or "").upper(): h for h in _active_manual_holds()}
    df.attrs["manual_hold_exclusions"] = []
    if not held or df.empty or order_column not in df.columns:
        return df
    mask = df[order_column].map(lambda value: str(value).strip().upper() in held)
    if not mask.any():
        return df
    excluded_ids = sorted({str(v).strip().upper() for v in df.loc[mask, order_column]})
    trimmed = df.loc[~mask].reset_index(drop=True)
    trimmed.attrs.update(df.attrs)
    trimmed.attrs["manual_hold_exclusions"] = [
        {
            "order_id": order_id,
            "hold_kind": held[order_id].get("hold_kind"),
            "expires_at": str(held[order_id].get("expires_at") or "")[:10],
            "created_by": held[order_id].get("created_by"),
        }
        for order_id in excluded_ids
    ]
    logger.info("Manual holds excluded %d order(s) from the picklist: %s", len(excluded_ids), ", ".join(excluded_ids))
    return trimmed


def fetch_order_shipment_rows(order_id: str) -> list[dict]:
    df = run_erp_query_file(ORDER_SHIPMENTS_FILE, {"so": order_id}, f"order shipments ({order_id})")
    return df.to_dict(orient="records")


def fetch_shipment_lookup_rows(
    start_date: str, end_date: str, customer: str = "", so: str = ""
) -> list[dict]:
    customer_term = (customer or "").strip().upper()
    so_term = (so or "").strip().upper()
    df = run_erp_query_file(
        SHIPMENTS_LOOKUP_FILE,
        {
            "start_date": start_date,
            "end_date": end_date,
            "customer_pattern": f"%{customer_term}%" if customer_term else "%",
            "so_pattern": f"%{so_term}%" if so_term else "%",
        },
        f"shipment lookup {start_date}..{end_date}",
    )
    return df.to_dict(orient="records")


def fetch_stock_rows(part_id: str) -> list[dict]:
    df = run_erp_query_file(STOCK_BY_PART_FILE, {"part_id": part_id}, f"stock lookup ({part_id})")
    return df.to_dict(orient="records")


def build_stock_lookup(part_id: str) -> dict[str, Any]:
    part = (part_id or "").strip().upper()
    rows = fetch_stock_rows(part)
    allocation_payload: Optional[dict[str, Any]]
    try:
        allocation_payload = _build_allocation_payload(part)
    except Exception as exc:  # noqa: BLE001 - allocation is supplemental here
        logger.exception("Allocation lookup for stock page failed (%s)", part)
        allocation_payload = {"error": str(exc)}
    try:
        reservations = shipping_store.serial_reservations_for_gate()
    except Exception:  # noqa: BLE001
        logger.exception("Serial reservation read failed for stock page")
        reservations = []
    return stock.build_stock_payload(
        part,
        location_rows=rows,
        allocation=allocation_payload,
        reservations=reservations,
        manual_holds=_active_manual_holds(),
    )


def _search_parts(term: str) -> list[dict[str, Any]]:
    df = run_erp_query_file(
        ALLOC_PARTS_FILE,
        {"pattern": f"%{term}%", "prefix": f"{term}%"},
        f"part search '{term}'",
    )
    return [
        {
            "part_id": str(row.get("PART_ID") or ""),
            "description": row.get("DESCRIPTION") if row.get("DESCRIPTION") == row.get("DESCRIPTION") else None,
            "on_hand": int(row.get("ON_HAND") or 0),
            "open_demand": int(row.get("OPEN_DEMAND") or 0),
        }
        for row in df.to_dict(orient="records")
    ]


def _shipment_lookup_params() -> dict[str, Any]:
    today = _today_local()
    end_raw = (request.args.get("end") or "").strip()
    start_raw = (request.args.get("start") or "").strip()
    try:
        end_day = date.fromisoformat(end_raw) if end_raw else today
    except ValueError:
        end_day = today
    try:
        start_day = date.fromisoformat(start_raw) if start_raw else end_day - timedelta(days=7)
    except ValueError:
        start_day = end_day - timedelta(days=7)
    if start_day > end_day:
        start_day, end_day = end_day, start_day
    if (end_day - start_day).days > SHIPMENTS_LOOKUP_MAX_DAYS:
        start_day = end_day - timedelta(days=SHIPMENTS_LOOKUP_MAX_DAYS)
    return {
        "so": (request.args.get("so") or "").strip().upper(),
        "customer": (request.args.get("customer") or "").strip(),
        "serial": (request.args.get("serial") or "").strip().upper(),
        "start": start_day.isoformat(),
        "end": end_day.isoformat(),
        "end_exclusive": (end_day + timedelta(days=1)).isoformat(),
    }


def _shipment_lookup(params: dict[str, Any]) -> dict[str, Any]:
    if params["so"] and not params.get("customer"):
        # A specific order: ignore the date window so old shipments are found too.
        rows = fetch_order_shipment_rows(params["so"]) if "-" in params["so"] else fetch_shipment_lookup_rows(
            "1900-01-01", params["end_exclusive"], "", params["so"]
        )
    else:
        rows = fetch_shipment_lookup_rows(params["start"], params["end_exclusive"], params["customer"], params["so"])
    packlists = shipments.group_packlists(rows)
    return {"packlists": packlists, "summary": shipments.summarize(packlists), "params": params, "error": None}
