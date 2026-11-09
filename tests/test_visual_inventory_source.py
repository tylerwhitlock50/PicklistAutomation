"""Read-only source contract tests. These never connect to VISUAL."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json

import pytest

from fulfillment.domain import Conflict
from fulfillment import visual_inventory_source as source


SCOPE = dict(site="TEST", warehouse="SHIPPING", location="DISPATCH")


def stock(part, quantity, traced=False, **updates):
    return dict(PART_ID=part, STOCK_UM="EA", QTY=Decimal(quantity), STATUS="A",
                LOCKED="N", TRANSIT="N", HOLD_REASON_ID=None,
                TRACE_PROFILE_PART_ID=part if traced else None, **updates)


def movement(transaction, part, quantity, *, direction="I", category="R", traced=False, serial=None, trace_qty=None, **updates):
    return dict(TRANSACTION_ID=transaction, PART_ID=part, STOCK_UM="EA",
                QTY=Decimal(quantity), TYPE=direction, CLASS=category,
                TRACE_PROFILE_PART_ID=part if traced else None,
                TRANSFER_TRANS_ID=None, XFER_IN_TRANSIT=None, CUST_ORDER_ID=None, CUST_ORDER_LINE_NO=None,
                PURC_ORDER_ID=None, PURC_ORDER_LINE_NO=None,
                VERIFIED_WORK_ORDER=0, VERIFIED_PURCHASE_ORDER_LINE=0,
                WORKORDER_TYPE=None, WORKORDER_BASE_ID=None, WORKORDER_LOT_ID=None,
                WORKORDER_SPLIT_ID=None, WORKORDER_SUB_ID=None,
                TRACE_ID=serial, TRACE_QTY=Decimal(trace_qty) if trace_qty is not None else None) | updates


def work_order(base="WO-1", *, part="SERIAL-PART", traced=True, **updates):
    return dict(TYPE="W", BASE_ID=base, LOT_ID="1", SPLIT_ID="0", SUB_ID="0", PART_ID=part,
                STOCK_UM="EA", TRACE_PROFILE_PART_ID=part if traced else None,
                DESIRED_QTY=Decimal(5), RECEIVED_QTY=Decimal(2), STATUS="R",
                DUE_DATE=datetime(2026, 10, 23), HAS_DEMAND_LINK=0) | updates


def fixture_rows():
    return {
        "scope": [dict(SITE_ID="TEST", WAREHOUSE_ID="SHIPPING", LOCATION_ID="DISPATCH")],
        "balances": [stock("SERIAL-PART", "1", True), stock("COMPONENT", "5.5")],
        "traces": [dict(PART_ID="SERIAL-PART", TRACE_ID="SN-1", NET_QTY=Decimal(1))],
        "movements": [
            movement(10, "SERIAL-PART", "2", traced=True, serial="SN-1", trace_qty="1"),
            movement(10, "SERIAL-PART", "2", traced=True, serial="SN-2", trace_qty="1"),
            movement(20, "SERIAL-PART", "1", direction="O", category="I", traced=True,
                     serial="SN-2", trace_qty="-1", CUST_ORDER_ID="SO-TEST"),
            movement(1, "COMPONENT", "5.5"),
        ],
        "work-orders": [work_order(), work_order("WO-TIED", HAS_DEMAND_LINK=1),
                        work_order("WO-CLOSED", STATUS="C"), work_order("WO-CANCELLED", STATUS="X")],
        "observed": [dict(OBSERVED_AT=datetime(2026, 10, 20, 15, 30))],
    }


class FakeResult:
    def __init__(self, rows):
        self.rows, self.closed = rows, False

    def mappings(self):
        return self

    def fetchmany(self, count):
        self.fetch_limit = count
        return deepcopy(self.rows[:count])

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self, engine):
        self.engine = engine

    def __enter__(self):
        self.engine.active = True
        return self

    def __exit__(self, exc_type, *args):
        self.engine.active = False
        self.engine.completed.append(exc_type)

    def execute(self, statement, params):
        assert self.engine.active
        assert params == SCOPE
        query = str(statement)
        name = query.split("dispatch.inventory-", 1)[1].split(":", 1)[0]
        result = FakeResult(self.engine.rows[name])
        self.engine.executed.append((query, result))
        return result


class FakeEngine:
    def __init__(self, rows):
        self.rows, self.active, self.executed, self.completed = rows, False, [], []

    def begin(self):
        return FakeConnection(self)


def adapter(monkeypatch, *, rows=None, max_rows=100000):
    engine = FakeEngine(fixture_rows() if rows is None else rows)
    options = []

    def make_engine(connection, **kwargs):
        assert connection == "test-connection-placeholder"
        options.append(kwargs)
        return engine

    monkeypatch.setattr(source, "create_engine", make_engine)
    result = source.VisualInventorySource("test-connection-placeholder", SCOPE, max_rows=max_rows)
    return result, engine, options


def test_complete_capture_uses_one_serializable_transaction_and_stocking_quantities(monkeypatch):
    reader, engine, options = adapter(monkeypatch)
    assert not engine.executed  # Construction never contacts SQL Server.
    assert options == [dict(pool_pre_ping=True, isolation_level="SERIALIZABLE")]
    result = reader.capture()
    assert result["scope"] == SCOPE
    assert result["observed_at"] == "2026-10-20T15:30:00+00:00"
    assert result["balances"] == [
        dict(part="COMPONENT", uom="EA", quantity="5.5", serials=[]),
        dict(part="SERIAL-PART", uom="EA", quantity="1", serials=["SN-1"]),
    ]
    assert [row["id"] for row in result["movements"]] == ["1", "10", "20"]
    assert result["movements"][-1]["quantity"] == "-1"
    assert result["movements"][-1]["serials"] == [dict(serial="SN-2", quantity="-1")]
    assert result["movements"][-1]["kind"] == "shipment"
    assert len(engine.executed) == 6 and engine.completed == [None]
    assert all(result.closed and result.fetch_limit == 100001 for _, result in engine.executed)


def test_complete_result_limit_rejects_instead_of_returning_partial_snapshot(monkeypatch):
    reader, engine, _ = adapter(monkeypatch, max_rows=1)
    with pytest.raises(Conflict, match="exceeds its row limit"):
        reader.capture()
    assert engine.completed == [Conflict]
    assert all(result.closed for _, result in engine.executed)


@pytest.mark.parametrize("change,expected", [
    (lambda rows: rows["scope"].clear(), "configured inventory location"),
    (lambda rows: rows["balances"][0].update(QTY=Decimal(2)), "transaction totals"),
    (lambda rows: rows["balances"][0].update(QTY=Decimal(-1)), "negative stock"),
    (lambda rows: rows["balances"][0].update(LOCKED="Y"), "held, locked"),
    (lambda rows: rows["balances"][0].update(HOLD_REASON_ID="QUALITY"), "held, locked"),
    (lambda rows: rows["balances"].pop(0), "missing from PART_LOCATION"),
    (lambda rows: rows["traces"][0].update(NET_QTY=Decimal(2)), "non-unit or negative"),
    (lambda rows: rows["traces"].clear(), "trace identities"),
    (lambda rows: rows["traces"].append(deepcopy(rows["traces"][0])), "duplicate current trace"),
    (lambda rows: rows["balances"][0].update(TRACE_PROFILE_PART_ID=None), "without a matching"),
    (lambda rows: rows["movements"][0].update(TRACE_QTY=Decimal(2)), "non-unit trace movements"),
    (lambda rows: rows["movements"].pop(0), "signed trace quantities"),
    (lambda rows: rows["movements"].append(deepcopy(rows["movements"][0])), "duplicate transaction"),
])
def test_incomplete_or_inconsistent_source_evidence_fails_closed(monkeypatch, change, expected):
    rows = fixture_rows()
    change(rows)
    reader, engine, _ = adapter(monkeypatch, rows=rows)
    with pytest.raises(Conflict, match=expected):
        reader.capture()
    assert engine.completed == [Conflict]


def test_work_orders_keep_full_keys_and_remove_tied_closed_or_subassembly_future_capacity(monkeypatch):
    rows = fixture_rows()
    rows["work-orders"].extend([work_order("WO-SUB", SUB_ID="1"),
                              work_order("WO-SPLIT", SPLIT_ID="2"),
                              work_order("WO-UNRELEASED", STATUS="U"),
                              work_order("WO-OVER", RECEIVED_QTY=Decimal(6))])
    reader, _, _ = adapter(monkeypatch, rows=rows)
    result = {json.loads(row["work_order"])[1]: row for row in reader.capture()["work_orders"]}
    assert result["WO-1"]["remaining"] == "3"
    assert result["WO-1"]["serialized"] is True
    assert json.loads(result["WO-SPLIT"]["work_order"]) == ["W", "WO-SPLIT", "1", "2", "0"]
    assert result["WO-SPLIT"]["status"] == "open"
    assert result["WO-OVER"]["remaining"] == "0"
    for name in ("WO-TIED", "WO-CLOSED", "WO-SUB", "WO-UNRELEASED"):
        assert result[name]["remaining"] == "0" and result[name]["status"] == "closed"
    assert result["WO-CANCELLED"]["status"] == "cancelled"


def test_canonical_work_order_rejects_partial_or_oversized_keys_without_truncating():
    assert source.canonical_work_order([None] * 5, optional=True) == ""
    for parts in (["W", "WO", "1", None, "0"], ["W", "X" * 80, "1", "0", "0"]):
        with pytest.raises(Conflict):
            source.canonical_work_order(parts)
    assert source.canonical_work_order(["W", "A/B", "1", "0", "0"]) != source.canonical_work_order(["W", "A", "B/1", "0", "0"])


def valid_transfer(**changes):
    return movement(30, "COMPONENT", "3", category="A", TRANSFER_TRANS_ID=31,
                    PAIR_TRANSACTION_ID=31, PAIR_TRANSFER_TRANS_ID=30,
                    PAIR_TYPE="O", PAIR_CLASS="A", PAIR_PART_ID="COMPONENT", PAIR_QTY=Decimal(3),
                    PAIR_SITE_ID="TEST", PAIR_WAREHOUSE_ID="MAIN", PAIR_LOCATION_ID="BUILD",
                    PAIR_XFER_IN_TRANSIT=None) | changes


def manufacturing_receipt(**changes):
    return movement(50, "COMPONENT", "2", WORKORDER_TYPE="W", WORKORDER_BASE_ID="WO-TEST",
                    WORKORDER_LOT_ID="1", WORKORDER_SPLIT_ID="0", WORKORDER_SUB_ID="0",
                    VERIFIED_WORK_ORDER=1) | changes


def purchase_receipt(**changes):
    return movement(51, "COMPONENT", "2", PURC_ORDER_ID="PO-TEST", PURC_ORDER_LINE_NO=3,
                    VERIFIED_PURCHASE_ORDER_LINE=1) | changes


@pytest.mark.parametrize("row,expected", [
    (manufacturing_receipt(), "receipt"),
    (purchase_receipt(), "receipt"),
    (movement(52, "COMPONENT", "2"), "other"),
    (manufacturing_receipt(VERIFIED_WORK_ORDER=0), "other"),
    (manufacturing_receipt(WORKORDER_TYPE="M"), "other"),
    (purchase_receipt(VERIFIED_PURCHASE_ORDER_LINE=0), "other"),
    (purchase_receipt(PURC_ORDER_ID=None), "other"),
    (purchase_receipt(PURC_ORDER_LINE_NO=None), "other"),
    (manufacturing_receipt(CUST_ORDER_ID="SO-RETURN"), "other"),
    (purchase_receipt(CUST_ORDER_ID="SO-RETURN"), "other"),
    (purchase_receipt(CUST_ORDER_LINE_NO=4), "other"),
    (manufacturing_receipt(PURC_ORDER_ID="PO-TEST", PURC_ORDER_LINE_NO=3,
                           VERIFIED_PURCHASE_ORDER_LINE=1), "other"),
    (purchase_receipt(QTY=Decimal(-2)), "other"),
    (manufacturing_receipt(TRANSFER_TRANS_ID=99), "other"),
])
def test_receipt_requires_verified_unambiguous_production_or_purchase_origin(monkeypatch, row, expected):
    reader, _, _ = adapter(monkeypatch)
    movements, _ = reader._movements([row])
    assert movements[0]["kind"] == expected


def test_only_supported_receipts_and_matching_external_transfers_are_automatic(monkeypatch):
    reader, _, _ = adapter(monkeypatch)
    rows = [valid_transfer(), movement(40, "COMPONENT", "1", category="A"),
            movement(41, "COMPONENT", "1", category="I"),
            movement(42, "COMPONENT", "-1"),
            movement(43, "COMPONENT", "1", direction="O", category="A"),
            movement(44, "COMPONENT", "1", XFER_IN_TRANSIT="Y")]
    movements, _ = reader._movements(rows)
    assert [row["kind"] for row in movements] == ["receipt", "other", "other", "other", "other", "other"]
    assert movements[0]["work_order"] == ""  # No inferred work order by SKU.
    assert movements[3]["quantity"] == "-1"  # Negative receipt is a reversal.


@pytest.mark.parametrize("changes", [
    dict(PAIR_TRANSFER_TRANS_ID=999), dict(PAIR_QTY=Decimal(2)),
    dict(PAIR_TYPE="I"), dict(PAIR_CLASS="R"), dict(PAIR_PART_ID="OTHER"),
    dict(PAIR_XFER_IN_TRANSIT="Y"), dict(PAIR_LOCATION_ID=None),
    dict(PAIR_WAREHOUSE_ID="SHIPPING", PAIR_LOCATION_ID="DISPATCH"),
])
def test_ambiguous_or_internal_transfer_is_attention_not_receipt(monkeypatch, changes):
    reader, _, _ = adapter(monkeypatch)
    movements, _ = reader._movements([valid_transfer(**changes)])
    assert movements[0]["kind"] == "other"


def test_zero_cost_rows_and_changed_decimal_scale_do_not_invent_movements(monkeypatch):
    reader, _, _ = adapter(monkeypatch)
    rows = [movement(1, "COMPONENT", "0", category="C"),
            movement(2, "COMPONENT", "1.00000000")]
    movements, totals = reader._movements(rows)
    assert movements[0]["quantity"] == "0" and movements[0]["kind"] == "other"
    assert movements[1]["quantity"] == "1"
    assert totals["COMPONENT"][1] == 1


def test_sql_assets_have_no_id_watermark_no_dirty_read_and_all_five_wo_keys():
    movements = (source.SQL_ROOT / "inventory_sync_movements.sql").read_text()
    traces = (source.SQL_ROOT / "inventory_sync_traces.sql").read_text()
    orders = (source.SQL_ROOT / "inventory_sync_work_orders.sql").read_text()
    assert "TRANSACTION_ID >" not in movements and "ROWID >" not in movements
    assert "NOLOCK" not in movements and "NOLOCK" not in traces
    assert "SUM(tit.QTY)" in traces  # Trace signs must not be flipped by TYPE.
    assert "wo.TYPE = 'W'" in orders
    for field in ("BASE_ID", "LOT_ID", "SPLIT_ID", "SUB_ID"):
        assert "dsl.SUPPLY_" + field + " = wo." + field in orders
    assert "wo.STATUS IN" not in orders  # Closed/cancelled promises stay visible.
    assert "po.ID = it.PURC_ORDER_ID AND pol.LINE_NO = it.PURC_ORDER_LINE_NO" in movements
    assert "po.SITE_ID = it.SITE_ID AND pol.PART_ID = it.PART_ID" in movements
    assert "wo.SITE_ID = it.SITE_ID AND wo.PART_ID = it.PART_ID" in movements
    assert "it.CUST_ORDER_ID, it.CUST_ORDER_LINE_NO" in movements
    for field in ("TYPE", "BASE_ID", "LOT_ID", "SPLIT_ID", "SUB_ID"):
        assert "wo." + field + " = it.WORKORDER_" + field in movements


def test_snapshot_timestamp_uses_utc_query_semantics():
    assert source.observed_timestamp(datetime(2026, 10, 20)) == "2026-10-20T00:00:00+00:00"
    assert source.observed_timestamp(datetime(2026, 10, 20, tzinfo=timezone.utc)).endswith("+00:00")


def test_source_movement_retains_document_purchase_transfer_and_date_lineage(monkeypatch):
    reader, _, _ = adapter(monkeypatch)
    row = movement(90, "COMPONENT", "2", direction="O", category="I", CUST_ORDER_ID="SO-1", CUST_ORDER_LINE_NO=4,
                   PACKLIST_ID="PL-1", PACKLIST_LINE_NO=7, TRANSACTION_DATE=datetime(2026, 10, 19),
                   CREATE_DATE=datetime(2026, 10, 20, 9, 30), PURC_ORDER_ID="PO-OLD", PURC_ORDER_LINE_NO=3)
    rows, _ = reader._movements([row])
    result = rows[0]
    assert result["customer_order"] == "SO-1" and result["customer_order_line"] == "4"
    assert result["document"] == "PL-1" and result["document_line"] == "7"
    assert result["effective_date"] == "2026-10-19" and result["source_created_local"] == "2026-10-20 09:30:00"
    assert result["purchase_order"] == "PO-OLD" and result["purchase_order_line"] == "3"
    assert result["direction"] == "O" and result["category"] == "I"
    sql = (source.SQL_ROOT / "inventory_sync_movements.sql").read_text()
    assert "sl.TRANSACTION_ID = it.TRANSACTION_ID" in sql
    assert "it.TRANSACTION_DATE, it.CREATE_DATE" in sql
