from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import csv
import os

import pytest

from fulfillment.domain import Conflict
from fulfillment.erp import VisualERP
from scripts.worldship_uploader import upload, REQUIRED


class SourceConnection:
    def __enter__(self): return self
    def __exit__(self, *args): pass


class SourceEngine:
    def begin(self): return SourceConnection()


def source_fixture():
    adapter = VisualERP.__new__(VisualERP)
    adapter.site = "TEST"
    from zoneinfo import ZoneInfo
    adapter.timezone = ZoneInfo("America/Denver")
    adapter.engine = SourceEngine()
    context = dict(policies={"DEFAULT": {"account_type": "standard", "accumulate": False}}, due_days=1, cooldown=0,
                   exceptions=[], holds=[], doc_holds=[], ocr_enabled=False, readiness_at=None)
    adapter.context = lambda: deepcopy(context)
    candidate = dict(CUST_ORDER_ID="SO-1", LINE_NO="1", PART_ID="PART-1", CUSTOMER_ID="CUSTOMER", CUSTOMER_NAME="Customer", SHIP_TO_ID="MAIN",
                     ITEM_TYPE="guns", OPEN_QTY=2, AVAILABLE_QTY=2, ORDER_RELEASED=1, LINE_AVAILABLE=1, CREDIT_APPROVED=1, SHIP_TO_PRESENT=1,
                     ORDER_DATE="2026-01-01", DESIRED_SHIP_DATE="2026-01-01", IS_RMA=0, IS_INTERNATIONAL=0, IS_EMPLOYEE=0, EXCLUDED_CUSTOMER=0)
    detail = {**candidate, "ORDER_STATUS": "R", "ORDER_QTY": Decimal(2), "SHIPPED_QTY": Decimal(0), "STOCK_UM": "EA", "SHIPTO_NAME": "Customer",
              "SHIPTO_ADDR_1": "100 Example", "SHIPTO_CITY": "Denver", "SHIPTO_STATE": "CO", "SHIPTO_ZIP": "80202", "SHIPTO_COUNTRY": "US"}
    stocks = [dict(PART_ID="PART-1", WAREHOUSE_ID="SHIPPING", LOCATION_ID="R09S02", QTY=Decimal(2), STOCK_UM="EA", UPC="100")]
    serials = [dict(PART_ID="PART-1", SERIAL_NO=s, WAREHOUSE_ID="SHIPPING", LOCATION_ID="R09S02", NET_QTY=1, LAST_TRANSACTION_AT="2026-01-01") for s in ("SN-1", "SN-2")]
    adapter.detail = lambda conn, order: [deepcopy(detail)]
    adapter.stocks = lambda conn: deepcopy(stocks)
    adapter.blocking = lambda rows, context: []
    def read(conn, query, **kwargs):
        if "RecentShippedOrders" in query: return []
        if "TraceLocation" in query: return deepcopy(serials)
        return [deepcopy(candidate)]
    adapter.read = read
    return adapter, candidate, detail, stocks, serials


def test_live_adapter_prescribes_composite_serials_and_exact_lines():
    adapter, *_ = source_fixture()
    result = adapter.available([], [])
    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["lines"][0]["order_line"] == "1"
    assert order["lines"][0]["serials"] == ["SN-1", "SN-2"]
    assert result["reservations"] == []  # ordinary orders have no sticky batch reservation


def test_live_adapter_excludes_active_work_and_supply():
    adapter, *_ = source_fixture()
    work = adapter.available([], [])["orders"][0]
    work.update(state="picking", created_at=datetime.now(timezone.utc).isoformat())
    assert adapter.available([work], [])["orders"] == []


def test_configured_batch_keeps_sticky_serials_on_their_order_line():
    adapter, *_ = source_fixture()
    context = adapter.context()
    context["policies"] = {"CUSTOMER": {"accumulate": True, "min_guns": 2}}
    adapter.context = lambda: deepcopy(context)
    first = adapter.available([], [])
    assert first["orders"]
    assert {r["serial_no"] for r in first["reservations"]} == {"SN-1", "SN-2"}
    second = adapter.available([], first["reservations"])
    assert second["orders"][0]["lines"] == first["orders"][0]["lines"]


def test_order_edits_and_external_shipments_block_existing_work():
    adapter, _, detail, *_ = source_fixture()
    work = adapter.available([], [])["orders"][0]
    adapter.check_order(work)
    detail["SHIPPED_QTY"] = Decimal(1)
    with pytest.raises(Conflict, match="shipped quantities"):
        adapter.check_order(work)
    detail["SHIPPED_QTY"] = Decimal(2)
    adapter.check_order(work, final=True)
    detail["ORDER_QTY"] = Decimal(3)
    with pytest.raises(Conflict, match="lines changed"):
        adapter.check_order(work, final=True)


def test_rendered_detail_preserves_stocking_uom_and_site():
    adapter = VisualERP.__new__(VisualERP); adapter.site = "TEST"
    def read(conn, query, **params):
        assert "p.STOCK_UM" in query and "co.SITE_ID = :site" in query
        assert "WITH (NOLOCK)" not in query
        assert params == {"so": "SO-1", "site": "TEST"}
        return [{"SITE_ID": "TEST"}]
    adapter.read = read
    adapter.detail(None, "SO-1")


def test_non_unit_serial_supply_blocks_prescription():
    adapter, _, _, _, serials = source_fixture()
    serials[0]["NET_QTY"] = 2
    with pytest.raises(Conflict, match="non-unit"):
        adapter.available([], [])


def write_snapshot(path, **changes):
    row = {k: "" for k in REQUIRED}
    row.update(tracking="TRACK", site="TEST", order="SO-1", packlist="PL-1", package="BOX-1", void="N",
               exported_at=datetime.now(timezone.utc).isoformat(), name="Customer", address1="100 Example", city="Denver", state="CO", postal="80202", country="US")
    row.update(changes)
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer=csv.DictWriter(stream,fieldnames=sorted(REQUIRED));writer.writeheader();writer.writerow(row)


class FakeHTTP:
    def __init__(self): self.calls=[]
    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs));return self
    def raise_for_status(self): pass


def test_worldship_snapshot_retry_uses_same_key_and_preserves_full_address(tmp_path):
    path=tmp_path / "labels.csv";write_snapshot(path,address2="Suite 4")
    http=FakeHTTP()
    assert upload(path,"https://dispatch.test","x"*40,session=http)==1
    upload(path,"https://dispatch.test","x"*40,session=http)
    a,b=http.calls
    assert a[1]["headers"]["Idempotency-Key"] == b[1]["headers"]["Idempotency-Key"]
    assert a[1]["json"]["destination"]["address2"] == "Suite 4"


def test_worldship_stale_file_or_unknown_void_never_becomes_active(tmp_path):
    path=tmp_path / "labels.csv";write_snapshot(path,void="UNKNOWN")
    with pytest.raises(ValueError,match="void"):
        upload(path,"https://dispatch.test","x"*40,session=FakeHTTP())
    os.utime(path,(1,1))
    with pytest.raises(ValueError,match="stale"):
        upload(path,"https://dispatch.test","x"*40,session=FakeHTTP())


def test_legacy_cutover_blocks_generation_and_reservation_writer(monkeypatch):
    monkeypatch.setenv("FULFILLMENT_OPERATOR_CUTOVER", "true")
    from picklist.services.run_service import fetch_picklist_from_mssql
    from picklist.stores.shipping_store import sync_serial_reservations
    with pytest.raises(RuntimeError, match="moved to Dispatch"):
        fetch_picklist_from_mssql("guns")
    with pytest.raises(RuntimeError, match="moved to Dispatch"):
        sync_serial_reservations(desired=[], live_serials=[], evaluated_at="now", policy_version=1)
