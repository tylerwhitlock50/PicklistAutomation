"""Order readiness rules: "why can't this ship, and who owns fixing it?"

Pure module (no Flask, no database). ``evaluate_readiness`` takes the rows
returned by sql/readiness_candidates.sql (one per open physical line) plus a
few local facts (release-gate decisions, today's picklist, manual holds) and
produces, per order, a state (BLOCKED / ATTENTION / READY) and a list of
holds, each with a reason code from ``HOLD_REASONS`` and an owning team.

The reason registry mirrors the style of ``release_gate.REASON_CODES``:
append-only, every code has a human label, and the owner team is what routes
a hold to Inside Sales, Finance, Shipping or Production instead of a Teams
message.
"""

from __future__ import annotations

import datetime as dt
import re
from collections import OrderedDict
from typing import Any, Iterable, Mapping, Optional

# --------------------------------------------------------------------------- registry

STATE_BLOCKED = "BLOCKED"
STATE_ATTENTION = "ATTENTION"
STATE_READY = "READY"
ORDER_STATES = (STATE_BLOCKED, STATE_ATTENTION, STATE_READY)

OWNER_SALES = "sales"
OWNER_FINANCE = "finance"
OWNER_SHIPPING = "shipping"
OWNER_PRODUCTION = "production"
OWNER_COMPLIANCE = "compliance"
OWNER_LABELS = {
    OWNER_SALES: "Inside Sales",
    OWNER_FINANCE: "Finance",
    OWNER_SHIPPING: "Shipping",
    OWNER_PRODUCTION: "Production",
    OWNER_COMPLIANCE: "Compliance",
}

# code -> {label, owner, blocking, tier}. Append-only.
HOLD_REASONS: "OrderedDict[str, dict[str, Any]]" = OrderedDict(
    [
        ("order_not_released", {"label": "Firmed, not released (status F)", "owner": OWNER_SALES, "blocking": True, "tier": 1}),
        ("order_hold", {"label": "Order on hold (status H)", "owner": OWNER_SALES, "blocking": True, "tier": 1}),
        ("credit_status_hold", {"label": "Credit status not approved", "owner": OWNER_FINANCE, "blocking": True, "tier": 1}),
        ("credit_limit_would_exceed", {"label": "Shipping would exceed credit limit", "owner": OWNER_FINANCE, "blocking": True, "tier": 1}),
        # Firearm order with no license anywhere: the one hold Sales must never let sit.
        ("ffl_missing", {"label": "Firearm order with no FFL on the ship-to or customer master", "owner": OWNER_SALES, "blocking": True, "tier": 1, "critical": True}),
        ("ffl_expired", {"label": "FFL on file has expired, update the ship-to record", "owner": OWNER_SALES, "blocking": True, "tier": 1}),
        ("ffl_expires_before_promise", {"label": "FFL expires before the promise date", "owner": OWNER_SALES, "blocking": False, "tier": 1}),
        ("ffl_unparseable_expiry", {"label": "FFL expiration is not a readable date", "owner": OWNER_SALES, "blocking": False, "tier": 1}),
        # Retired 2026-10-02: the ship-to FFL is authoritative and a differing master
        # FFL is not a problem. Kept so stored hold history still resolves a label.
        ("ffl_master_shipto_mismatch", {"label": "Customer-master FFL differs from ship-to FFL", "owner": OWNER_SALES, "blocking": False, "tier": 1, "retired": True}),
        ("ffl_doc_missing", {"label": "No FFL / EZ Check attached to the order", "owner": OWNER_SALES, "blocking": False, "tier": 1}),
        ("ship_to_vs_ffl_name_mismatch", {"label": "Ship-to name does not match FFL licensee or trade name", "owner": OWNER_SALES, "blocking": True, "tier": 2}),
        ("ship_to_vs_ffl_premise_mismatch", {"label": "Ship-to address does not match FFL premise", "owner": OWNER_SALES, "blocking": True, "tier": 2}),
        ("ffl_record_differs_from_doc", {"label": "Ship-to FFL record differs from the attached license (expiry or number)", "owner": OWNER_SALES, "blocking": False, "tier": 2}),
        ("ship_to_missing", {"label": "No ship-to address on the order", "owner": OWNER_SALES, "blocking": True, "tier": 1}),
        ("ship_via_missing", {"label": "Ship via is blank on the order and the customer master", "owner": OWNER_SALES, "blocking": False, "tier": 1}),
        ("rma_excluded", {"label": "RMA order, handled outside the picklist", "owner": OWNER_SHIPPING, "blocking": True, "tier": 1}),
        ("excluded_class", {"label": "International / employee / excluded account, handled outside the picklist", "owner": OWNER_SHIPPING, "blocking": True, "tier": 1}),
        ("no_supply", {"label": "No pickable stock for this part", "owner": OWNER_PRODUCTION, "blocking": True, "tier": 1}),
        ("partial_supply", {"label": "Only part of the open quantity is pickable", "owner": OWNER_PRODUCTION, "blocking": False, "tier": 1}),
        ("not_on_picklist", {"label": "Ready but missing from today's picklist", "owner": OWNER_SHIPPING, "blocking": False, "tier": 1}),
        ("gate_hold", {"label": "Release gate is holding this order", "owner": OWNER_SHIPPING, "blocking": False, "tier": 1}),
        ("manual_hold", {"label": "Manual hold from an approved exception", "owner": OWNER_SALES, "blocking": True, "tier": 1}),
    ]
)

FIREARM_DOC_KINDS = ("ffl_ez_check", "ffl_master")

DEFAULT_CONFIG: dict[str, Any] = {
    # SHIP_CREDIT_LIMIT_CTL codes that mean VISUAL checks the limit at ship time.
    # Observed live: C (check, 40.9k customers), N (none), O (unconfirmed, 77).
    "credit_check_codes": ("C",),
    # Require an FFL / EZ Check attachment on firearms orders.
    "require_ffl_doc": True,
    # Days before expiry that counts as "expires before promise" when the
    # promise date is unknown.
    "ffl_expiry_guard_days": 0,
    # The ship-to FFL is authoritative. When the ship-to has no FFL number at all,
    # fall back to the customer-master FFL. An expired or unreadable ship-to FFL is
    # never papered over by the master record; it has to be fixed on the ship-to.
    "master_ffl_fallback": True,
}


# --------------------------------------------------------------------------- FFL helpers (ported from sql-toolbox ffl.py)

_DATE_FORMATS: tuple[str, ...] = (
    "%Y-%m-%d",
    "%m/%d/%Y",
    "%m-%d-%Y",
    "%m/%d/%y",
    "%m-%d-%y",
    "%d-%b-%Y",
    "%d-%b-%y",
    "%b %d %Y",
    "%B %d %Y",
)
_DATE_RE = re.compile(r"(?P<m>\d{1,2})[/\-.](?P<d>\d{1,2})[/\-.](?P<y>\d{2,4})")
_MONTH_YEAR_RE = re.compile(r"^(?P<m>\d{1,2})[/\-](?P<y>\d{4})$")
_NUMBER_NOISE_RE = re.compile(r"[\s\-_.]+")


def parse_expiration(raw: Any) -> Optional[dt.date]:
    """Parse the free-form USER_5 FFL expiration. Returns None on failure, never raises."""
    if raw is None:
        return None
    if isinstance(raw, dt.datetime):
        return raw.date()
    if isinstance(raw, dt.date):
        return raw
    text = str(raw).strip()
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    match = _DATE_RE.search(text)
    if match:
        month = int(match.group("m"))
        day = int(match.group("d"))
        year_raw = match.group("y")
        year = int(year_raw)
        if len(year_raw) == 2:
            year += 2000 if year < 70 else 1900
        try:
            return dt.date(year, month, day)
        except ValueError:
            return None
    match = _MONTH_YEAR_RE.match(text)
    if match:
        # "2-2027": ATF licenses expire on the 1st of the month.
        try:
            return dt.date(int(match.group("y")), int(match.group("m")), 1)
        except ValueError:
            return None
    return None


def _ffl_tokens(raw: Any) -> list[str]:
    if not raw:
        return []
    cleaned = _NUMBER_NOISE_RE.sub(" ", str(raw)).strip().upper()
    return [token for token in cleaned.split(" ") if token]


def _is_mask(token: str) -> bool:
    return bool(token) and set(token) == {"X"}


def _flatten(raw: Any) -> str:
    if not raw:
        return ""
    return _NUMBER_NOISE_RE.sub("", str(raw)).upper()


def ffl_numbers_match(left: Any, right: Any) -> bool:
    """Token-wise FFL number compare; EZ Check all-X segments are wildcards."""
    tokens_left, tokens_right = _ffl_tokens(left), _ffl_tokens(right)
    if not tokens_left or not tokens_right:
        return False
    if len(tokens_left) != len(tokens_right):
        return _flatten(left) == _flatten(right)
    for a, b in zip(tokens_left, tokens_right):
        if _is_mask(a) or _is_mask(b):
            continue
        if a != b:
            return False
    return True


# --------------------------------------------------------------------------- row normalization


def _text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "nat"} else text


def _num(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:  # NaN
        return default
    return number


def _flag(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "y", "yes", "true", "t"}
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return bool(value)


def _as_date(value: Any) -> Optional[dt.date]:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return parse_expiration(text)


def _iso(value: Optional[dt.date]) -> Optional[str]:
    return value.isoformat() if value else None


def _get(row: Mapping[str, Any], key: str) -> Any:
    if key in row:
        return row[key]
    lower = key.lower()
    for candidate, value in row.items():
        if str(candidate).lower() == lower:
            return value
    return None


def normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Lower-case, typed view of one SQL row."""
    return {
        "order_id": _text(_get(row, "CUST_ORDER_ID")).upper(),
        "line_no": _text(_get(row, "LINE_NO")),
        "customer_id": _text(_get(row, "CUSTOMER_ID")),
        "customer_name": _text(_get(row, "CUSTOMER_NAME")),
        "order_status": _text(_get(row, "ORDER_STATUS")).upper(),
        "line_status": _text(_get(row, "LINE_STATUS")).upper() or "A",
        "order_date": _as_date(_get(row, "ORDER_DATE")),
        "ship_to_id": _text(_get(row, "SHIP_TO_ID")),
        "ship_to_addr_no": _text(_get(row, "SHIP_TO_ADDR_NO")),
        "part_id": _text(_get(row, "PART_ID")).upper(),
        "product_code": _text(_get(row, "PRODUCT_CODE")),
        "part_description": _text(_get(row, "PART_DESCRIPTION")),
        "item_type": (_text(_get(row, "ITEM_TYPE")).lower() or "guns"),
        "order_qty": _num(_get(row, "ORDER_QTY")),
        "shipped_qty": _num(_get(row, "SHIPPED_QTY")),
        "open_qty": _num(_get(row, "OPEN_QTY")),
        "open_value": _num(_get(row, "OPEN_VALUE")),
        "available_qty": _num(_get(row, "AVAILABLE_QTY")),
        "promise_ship": _as_date(_get(row, "PROMISE_SHIP_DATE")),
        "promise_del": _as_date(_get(row, "PROMISE_DEL_DATE")),
        "desired_ship": _as_date(_get(row, "DESIRED_SHIP_DATE")),
        "ship_via": _text(_get(row, "SHIP_VIA")),
        "master_ship_via": _text(_get(row, "MASTER_SHIP_VIA")),
        "fob": _text(_get(row, "FREE_ON_BOARD")),
        "salesrep_id": _text(_get(row, "SALESREP_ID")),
        "po_ref": _text(_get(row, "CUSTOMER_PO_REF")),
        "discount_code": _text(_get(row, "DISCOUNT_CODE")),
        "shipto": {
            "name": _text(_get(row, "SHIPTO_NAME")),
            "addr_1": _text(_get(row, "SHIPTO_ADDR_1")),
            "addr_2": _text(_get(row, "SHIPTO_ADDR_2")),
            "addr_3": _text(_get(row, "SHIPTO_ADDR_3")),
            "city": _text(_get(row, "SHIPTO_CITY")),
            "state": _text(_get(row, "SHIPTO_STATE")),
            "zip": _text(_get(row, "SHIPTO_ZIP")),
            "country": _text(_get(row, "SHIPTO_COUNTRY")),
            "active": _text(_get(row, "SHIPTO_ACTIVE")).upper() != "N",
            "ffl_number": _text(_get(row, "SHIPTO_FFL_NUMBER")),
            "ffl_expiry_raw": _text(_get(row, "SHIPTO_FFL_EXPIRY_RAW")),
            "present": bool(_text(_get(row, "SHIPTO_NAME")) or _text(_get(row, "SHIPTO_ADDR_1"))),
        },
        "master": {
            "ffl_number": _text(_get(row, "MASTER_FFL_NUMBER")),
            "ffl_expiry_raw": _text(_get(row, "MASTER_FFL_EXPIRY_RAW")),
        },
        "credit": {
            "status": _text(_get(row, "CREDIT_STATUS")).upper(),
            "limit": _num(_get(row, "CREDIT_LIMIT")),
            "limit_ctl": _text(_get(row, "CREDIT_LIMIT_CTL")).upper(),
            "ship_ctl": _text(_get(row, "SHIP_CREDIT_LIMIT_CTL")).upper(),
            "open_recv": _num(_get(row, "TOTAL_OPEN_RECV")),
            "open_shipped": _num(_get(row, "TOTAL_OPEN_SHIPPED")),
            "open_orders": _num(_get(row, "TOTAL_OPEN_ORDERS")),
        },
        "docs": {
            "attachments": int(_num(_get(row, "ATTACHMENT_COUNT"))),
            "ffl_ez_check": int(_num(_get(row, "FFL_EZ_CHECK_COUNT"))),
            "ffl_master": int(_num(_get(row, "FFL_MASTER_COUNT"))),
        },
        "is_rma": _flag(_get(row, "IS_RMA")),
        "is_international": _flag(_get(row, "IS_INTERNATIONAL")),
        "is_employee": _flag(_get(row, "IS_EMPLOYEE")),
        "excluded_customer": _flag(_get(row, "EXCLUDED_CUSTOMER")),
    }


# --------------------------------------------------------------------------- bins


def classify_bin(warehouse: Any, location: Any) -> str:
    wh = _text(warehouse).upper()
    loc = _text(location).upper()
    if wh == "MAIN":
        return "main"
    if wh == "DISTRIBUTION":
        return "distribution_stock" if "STOCK" in loc else "distribution"
    if wh == "SHIPPING":
        if "STAGE" in loc:
            return "stage"
        if "INTERNATIONAL" in loc:
            return "international"
        prefix = loc[:3]
        if prefix == "R10":
            return "rack10"
        if prefix == "R11":
            return "r11_components"
        if "R01" <= prefix <= "R09":
            return "pickable"
        return "shipping_other"
    return "other"


BIN_CLASS_LABELS = {
    "pickable": "Pickable racks (R01-R09)",
    "r11_components": "Rack 11 components",
    "rack10": "Rack 10 (held / special)",
    "stage": "Stage",
    "international": "International cage",
    "shipping_other": "Other shipping bins",
    "distribution": "Distribution",
    "distribution_stock": "Distribution stock",
    "main": "MAIN",
    "other": "Other",
}


def group_locations(rows: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """part_id -> {"bins": [...], "by_class": {class: qty}, "pickable_qty": n}."""
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        part = _text(_get(row, "PART_ID")).upper()
        if not part:
            continue
        warehouse = _text(_get(row, "WAREHOUSE_ID"))
        location = _text(_get(row, "LOCATION_ID"))
        qty = _num(_get(row, "QTY"))
        if qty <= 0:
            continue
        kind = classify_bin(warehouse, location)
        entry = out.setdefault(part, {"bins": [], "by_class": {}, "pickable_qty": 0.0})
        entry["bins"].append({"warehouse": warehouse, "location": location, "qty": qty, "class": kind})
        entry["by_class"][kind] = entry["by_class"].get(kind, 0.0) + qty
        if kind == "pickable":
            entry["pickable_qty"] += qty
    for entry in out.values():
        entry["bins"].sort(key=lambda b: (b["class"] != "pickable", b["warehouse"], b["location"]))
    return out


# --------------------------------------------------------------------------- evaluation


def _hold(order_id: str, code: str, *, line_no: Optional[str] = None, detail: Optional[dict] = None,
          owner: Optional[str] = None, blocking: Optional[bool] = None) -> dict[str, Any]:
    meta = HOLD_REASONS[code]
    return {
        "order_id": order_id,
        "line_no": line_no,
        "reason_code": code,
        "label": meta["label"],
        "owner_team": owner or meta["owner"],
        "owner_label": OWNER_LABELS.get(owner or meta["owner"], (owner or meta["owner"]).capitalize()),
        "blocking": meta["blocking"] if blocking is None else bool(blocking),
        "critical": bool(meta.get("critical")),
        "tier": meta["tier"],
        "detail": detail or {},
    }


def _ffl_summary(head: dict[str, Any], today: dt.date, promise: Optional[dt.date], config: dict) -> dict[str, Any]:
    """Pick the FFL record the rules run against.

    The ship-to FFL is the main record: if it has a number it is used, even when
    it is expired or unreadable (Sales has to update the ship-to). Only when the
    ship-to has no number at all does the customer master stand in. Whether the
    two numbers agree is reported for the detail page but is not a hold.
    """
    shipto = head["shipto"]
    master = head["master"]
    shipto_expiry = parse_expiration(shipto["ffl_expiry_raw"]) if shipto["ffl_expiry_raw"] else None
    master_expiry = parse_expiration(master["ffl_expiry_raw"]) if master["ffl_expiry_raw"] else None
    source = "shipto" if shipto["ffl_number"] else ("master" if (config["master_ffl_fallback"] and master["ffl_number"]) else None)
    effective_number = shipto["ffl_number"] if source == "shipto" else (master["ffl_number"] if source == "master" else "")
    effective_raw = shipto["ffl_expiry_raw"] if source == "shipto" else (master["ffl_expiry_raw"] if source == "master" else "")
    effective_expiry = shipto_expiry if source == "shipto" else (master_expiry if source == "master" else None)
    numbers_agree: Optional[bool] = None
    if shipto["ffl_number"] and master["ffl_number"]:
        numbers_agree = ffl_numbers_match(shipto["ffl_number"], master["ffl_number"])
    master_current = bool(master["ffl_number"] and master_expiry and master_expiry >= today)
    return {
        "source": source,
        "number": effective_number,
        "expiry_raw": effective_raw,
        "expiry": _iso(effective_expiry),
        "days_to_expiry": (effective_expiry - today).days if effective_expiry else None,
        "expired": bool(effective_expiry and effective_expiry < today),
        "expires_before_promise": bool(effective_expiry and promise and effective_expiry < promise),
        "unparseable": bool(effective_raw and effective_expiry is None),
        "shipto_number": shipto["ffl_number"],
        "shipto_expiry_raw": shipto["ffl_expiry_raw"],
        "shipto_expiry": _iso(shipto_expiry),
        "master_number": master["ffl_number"],
        "master_expiry_raw": master["ffl_expiry_raw"],
        "master_expiry": _iso(master_expiry),
        # Master record that could be copied onto the ship-to when the ship-to is stale.
        "master_current": master_current,
        "numbers_agree": numbers_agree,
        "doc_count": head["docs"]["ffl_ez_check"] + head["docs"]["ffl_master"],
    }


def _credit_summary(head: dict[str, Any], order_open_value: float, config: dict) -> dict[str, Any]:
    credit = head["credit"]
    exposure = credit["open_recv"] + credit["open_shipped"]
    checks_at_ship = credit["ship_ctl"] in set(config["credit_check_codes"])
    limit = credit["limit"]
    projected = exposure + order_open_value
    would_exceed = bool(checks_at_ship and limit > 0 and projected > limit)
    return {
        "status": credit["status"],
        "approved": credit["status"] == "A",
        "limit": limit,
        "ship_ctl": credit["ship_ctl"],
        "checks_at_ship": checks_at_ship,
        "open_recv": credit["open_recv"],
        "open_shipped": credit["open_shipped"],
        "open_orders": credit["open_orders"],
        "exposure": exposure,
        "order_open_value": order_open_value,
        "projected": projected,
        "headroom": (limit - exposure) if limit > 0 else None,
        "would_exceed": would_exceed,
        "over_by": (projected - limit) if would_exceed else 0.0,
    }


def evaluate_readiness(
    rows: Iterable[Mapping[str, Any]],
    *,
    today: dt.date,
    gate_decisions: Optional[Mapping[str, Mapping[str, Any]]] = None,
    picklist_orders: Optional[Iterable[str]] = None,
    picklist_horizon: Optional[dt.date] = None,
    manual_holds: Iterable[Mapping[str, Any]] = (),
    doc_findings: Optional[Mapping[str, Iterable[Mapping[str, Any]]]] = None,
    config: Optional[Mapping[str, Any]] = None,
    evaluated_at: Optional[dt.datetime] = None,
) -> dict[str, Any]:
    """Evaluate every order in ``rows``.

    ``picklist_orders`` is the set of order IDs on today's latest picklist run
    (None when no run exists today, which suppresses ``not_on_picklist``).
    ``gate_decisions`` maps order id -> release-gate decision dict.
    ``manual_holds`` are active rows from the request store (Phase 3).
    ``doc_findings`` maps order id -> tier-2 findings from ffl_docs (Phase 4).
    """
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)
    gate_map = {str(k).upper(): dict(v) for k, v in (gate_decisions or {}).items()}
    picklist_set = (
        {str(o).strip().upper() for o in picklist_orders} if picklist_orders is not None else None
    )
    manual_map: dict[str, list[dict[str, Any]]] = {}
    for hold in manual_holds:
        order_id = _text(_get(hold, "cust_order_id")).upper()
        if order_id:
            manual_map.setdefault(order_id, []).append(dict(hold))
    findings_map = {str(k).upper(): list(v) for k, v in (doc_findings or {}).items()}
    stamp = evaluated_at or dt.datetime.now(dt.timezone.utc)

    grouped: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
    for raw in rows:
        line = normalize_row(raw)
        if not line["order_id"]:
            continue
        grouped.setdefault(line["order_id"], []).append(line)

    orders: list[dict[str, Any]] = []
    all_holds: list[dict[str, Any]] = []
    by_reason: dict[str, int] = {}
    by_owner_orders: dict[str, set[str]] = {}

    for order_id, lines in grouped.items():
        head = lines[0]
        open_lines = [ln for ln in lines if ln["line_status"] == "A" and ln["open_qty"] > 0]
        firearms = any(ln["item_type"] == "guns" for ln in open_lines)
        order_open_value = sum(ln["open_value"] for ln in open_lines)
        order_open_qty = sum(ln["open_qty"] for ln in open_lines)
        promise_candidates = [ln["promise_ship"] for ln in (open_lines or lines) if ln["promise_ship"]]
        promise_ship = min(promise_candidates) if promise_candidates else None
        promise_del_candidates = [ln["promise_del"] for ln in (open_lines or lines) if ln["promise_del"]]
        promise_del = min(promise_del_candidates) if promise_del_candidates else None
        desired_ship = head["desired_ship"]
        effective_due = promise_del or promise_ship or desired_ship or today

        holds: list[dict[str, Any]] = []
        status = head["order_status"]
        closed = status == "C" or not open_lines

        ffl = _ffl_summary(head, today, promise_ship, cfg)
        credit = _credit_summary(head, order_open_value, cfg)
        excluded = head["is_international"] or head["is_employee"] or head["excluded_customer"]

        if not closed:
            # --- order status
            if status == "H":
                holds.append(_hold(order_id, "order_hold", detail={"status": status}))
            elif status == "F":
                holds.append(_hold(order_id, "order_not_released", detail={"status": status}))

            # --- class / RMA (handled outside the picklist entirely)
            if head["is_rma"]:
                holds.append(_hold(order_id, "rma_excluded", detail={"salesrep_id": head["salesrep_id"], "po_ref": head["po_ref"]}))
            if excluded:
                reasons = [
                    name for name, on in (
                        ("international", head["is_international"]),
                        ("employee", head["is_employee"]),
                        ("excluded_customer", head["excluded_customer"]),
                    ) if on
                ]
                holds.append(_hold(
                    order_id, "excluded_class",
                    owner=OWNER_COMPLIANCE if head["is_international"] else OWNER_SHIPPING,
                    detail={"reasons": reasons, "discount_code": head["discount_code"]},
                ))

            # RMA / international / employee / excluded accounts are worked outside the
            # picklist entirely; piling supply, FFL and ship-via holds on them would only
            # create noise nobody can clear. Status and class holds are enough.
            handled_outside = head["is_rma"] or excluded

            # --- credit
            if not handled_outside and not credit["approved"]:
                holds.append(_hold(order_id, "credit_status_hold", detail={"credit_status": credit["status"] or "(none)"}))
            if not handled_outside and credit["would_exceed"]:
                holds.append(_hold(order_id, "credit_limit_would_exceed", detail={
                    "limit": credit["limit"], "exposure": credit["exposure"],
                    "order_open_value": order_open_value, "projected": credit["projected"],
                    "over_by": credit["over_by"], "ship_ctl": credit["ship_ctl"],
                }))

            # --- ship-to / ship via
            if not handled_outside and not head["shipto"]["present"] and not head["ship_to_id"]:
                holds.append(_hold(order_id, "ship_to_missing"))
            if not handled_outside and not head["ship_via"] and not head["master_ship_via"]:
                holds.append(_hold(order_id, "ship_via_missing"))

            # --- FFL (firearms orders only; international/employee handled above)
            # Ship-to FFL first. Stale or unreadable on the ship-to -> fix the ship-to,
            # with a pointer to the master record when it is current. No FFL on
            # either record is the critical case for a firearm shipment.
            if firearms and not excluded and not head["is_rma"]:
                if not ffl["number"]:
                    holds.append(_hold(order_id, "ffl_missing", detail={
                        "shipto_name": head["shipto"]["name"], "ship_to_id": head["ship_to_id"],
                        "customer_id": head["customer_id"], "fix": "Add the FFL number and expiration to the ship-to address in VISUAL.",
                    }))
                else:
                    master_hint = {
                        "master_number": ffl["master_number"], "master_expiry": ffl["master_expiry"],
                        "master_current": ffl["master_current"],
                    }
                    if ffl["source"] == "shipto" and ffl["master_current"] and ffl["numbers_agree"] is not False:
                        master_hint["fix"] = "Customer master has a current FFL; copy its expiration onto the ship-to."
                    elif ffl["source"] == "shipto" and ffl["master_current"]:
                        master_hint["fix"] = "Customer master has a current FFL under a different number; confirm which license the ship-to holds and update the ship-to."
                    elif ffl["source"] == "master":
                        master_hint["fix"] = "Ship-to has no FFL and the customer-master FFL it fell back to is stale; get the current license and add it to the ship-to."
                    else:
                        master_hint["fix"] = "Get the renewed license from the dealer and update the ship-to."
                    if ffl["unparseable"]:
                        holds.append(_hold(order_id, "ffl_unparseable_expiry", detail={
                            "expiry_raw": ffl["expiry_raw"], "source": ffl["source"], **master_hint,
                        }))
                    elif ffl["expired"]:
                        holds.append(_hold(order_id, "ffl_expired", detail={
                            "expiry": ffl["expiry"], "expiry_raw": ffl["expiry_raw"],
                            "days_ago": -ffl["days_to_expiry"], "source": ffl["source"], **master_hint,
                        }))
                    elif ffl["expires_before_promise"]:
                        holds.append(_hold(order_id, "ffl_expires_before_promise", detail={
                            "expiry": ffl["expiry"], "promise_ship": _iso(promise_ship), "source": ffl["source"],
                        }))
                if cfg["require_ffl_doc"] and ffl["doc_count"] == 0:
                    holds.append(_hold(order_id, "ffl_doc_missing", detail={
                        "attachments": head["docs"]["attachments"],
                    }))
                for finding in findings_map.get(order_id, []):
                    code = _text(_get(finding, "reason_code"))
                    if code in HOLD_REASONS and not _flag(_get(finding, "passed")):
                        holds.append(_hold(order_id, code, detail=dict(_get(finding, "detail") or {})))

            # --- supply (per open line)
            for ln in (open_lines if not handled_outside else []):
                if ln["available_qty"] <= 0:
                    holds.append(_hold(order_id, "no_supply", line_no=ln["line_no"], detail={
                        "part_id": ln["part_id"], "open_qty": ln["open_qty"], "available_qty": 0.0,
                    }))
                elif ln["available_qty"] < ln["open_qty"]:
                    holds.append(_hold(order_id, "partial_supply", line_no=ln["line_no"], detail={
                        "part_id": ln["part_id"], "open_qty": ln["open_qty"], "available_qty": ln["available_qty"],
                    }))

            # --- manual holds (Phase 3)
            for manual in manual_map.get(order_id, []):
                holds.append(_hold(order_id, "manual_hold", detail={
                    "hold_kind": _text(_get(manual, "hold_kind")),
                    "reason": _text(_get(manual, "reason")),
                    "expires_at": _text(_get(manual, "expires_at")),
                    "request_id": _get(manual, "request_id"),
                }))

            # --- release gate passthrough
            gate = gate_map.get(order_id)
            if gate and _text(gate.get("decision")).upper() in {"HOLD", "ACCUMULATING"}:
                holds.append(_hold(order_id, "gate_hold", detail={
                    "decision": _text(gate.get("decision")).upper(),
                    "reason_code": _text(gate.get("reason_code")),
                    "label": _text(gate.get("label")),
                    "next_release_date": _text(gate.get("next_release_date")) or None,
                }))

            # --- picklist presence (only when ready and inside the picklist window)
            blocking_now = any(h["blocking"] for h in holds)
            if (
                picklist_set is not None
                and status == "R"
                and not blocking_now
                and (picklist_horizon is None or effective_due <= picklist_horizon)
                and order_id not in picklist_set
            ):
                holds.append(_hold(order_id, "not_on_picklist", detail={"due": _iso(effective_due)}))

        blocking = any(h["blocking"] for h in holds)
        state = STATE_BLOCKED if blocking else (STATE_ATTENTION if holds else STATE_READY)
        owner_teams = sorted({h["owner_team"] for h in holds})
        for h in holds:
            by_reason[h["reason_code"]] = by_reason.get(h["reason_code"], 0) + 1
            by_owner_orders.setdefault(h["owner_team"], set()).add(order_id)

        gate = gate_map.get(order_id)
        orders.append({
            "order_id": order_id,
            "customer_id": head["customer_id"],
            "customer_name": head["customer_name"],
            "order_status": status,
            "closed": closed,
            "state": state,
            "blocking_count": sum(1 for h in holds if h["blocking"]),
            "hold_count": len(holds),
            "owner_teams": owner_teams,
            "owner_labels": [OWNER_LABELS.get(t, t) for t in owner_teams],
            "holds": holds,
            "firearms": firearms,
            "item_types": sorted({ln["item_type"] for ln in lines}),
            "order_date": _iso(head["order_date"]),
            "promise_ship": _iso(promise_ship),
            "promise_del": _iso(promise_del),
            "desired_ship": _iso(desired_ship),
            "due": _iso(effective_due),
            "open_qty": order_open_qty,
            "open_value": round(order_open_value, 2),
            "line_count": len(lines),
            "open_line_count": len(open_lines),
            "ship_via": head["ship_via"] or head["master_ship_via"],
            "ship_via_source": "order" if head["ship_via"] else ("customer" if head["master_ship_via"] else None),
            "fob": head["fob"],
            "salesrep_id": head["salesrep_id"],
            "po_ref": head["po_ref"],
            "discount_code": head["discount_code"],
            "ship_to_id": head["ship_to_id"],
            "ship_to": head["shipto"],
            "ffl": ffl,
            "credit": credit,
            "docs": head["docs"],
            "flags": {
                "is_rma": head["is_rma"],
                "is_international": head["is_international"],
                "is_employee": head["is_employee"],
                "excluded_customer": head["excluded_customer"],
            },
            "gate": {
                "decision": _text(gate.get("decision")).upper(),
                "reason_code": _text(gate.get("reason_code")),
                "label": _text(gate.get("label")),
                "next_release_date": _text(gate.get("next_release_date")) or None,
            } if gate else None,
            "on_picklist": (order_id in picklist_set) if picklist_set is not None else None,
            "lines": [
                {
                    "line_no": ln["line_no"],
                    "part_id": ln["part_id"],
                    "product_code": ln["product_code"],
                    "description": ln["part_description"],
                    "item_type": ln["item_type"],
                    "line_status": ln["line_status"],
                    "order_qty": ln["order_qty"],
                    "shipped_qty": ln["shipped_qty"],
                    "open_qty": ln["open_qty"],
                    "open_value": ln["open_value"],
                    "available_qty": ln["available_qty"],
                    "promise_ship": _iso(ln["promise_ship"]),
                    "promise_del": _iso(ln["promise_del"]),
                }
                for ln in lines
            ],
        })
        all_holds.extend(holds)

    state_counts = {s: 0 for s in ORDER_STATES}
    for order in orders:
        state_counts[order["state"]] += 1

    return {
        "evaluated_at": stamp.isoformat(),
        "today": today.isoformat(),
        "orders": orders,
        "holds": all_holds,
        "summary": {
            "orders": len(orders),
            "blocked": state_counts[STATE_BLOCKED],
            "attention": state_counts[STATE_ATTENTION],
            "ready": state_counts[STATE_READY],
            "holds": len(all_holds),
            "firearms_orders": sum(1 for o in orders if o["firearms"]),
            "by_reason": by_reason,
            "by_owner": {team: len(ids) for team, ids in by_owner_orders.items()},
        },
    }


def hold_key(hold: Mapping[str, Any]) -> tuple[str, str, str]:
    return (
        _text(_get(hold, "order_id") or _get(hold, "cust_order_id")).upper(),
        _text(_get(hold, "line_no")),
        _text(_get(hold, "reason_code")),
    )
