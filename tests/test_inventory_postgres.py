"""Persistence boundaries shared by SQLite and explicitly disposable PostgreSQL."""
from sqlalchemy import select

from fulfillment import model as m
from test_inventory import system, setup, ok


def test_inventory_hash_identity_uses_resource_detail_not_fulfillment_work_id(system):
    db, _, service = system
    setup(service)
    supply = ok(service, "supply", dict(work_order="W:AUDIT:1:0:0", part="ACCESSORY-B", uom="EA",
        serialized=False, quantity="5", due_date="", status="open", reason="Production forecast"))
    assert len(supply["id"]) == 64
    with db.engine.connect() as conn:
        audit = conn.execute(select(m.events).where(m.events.c.action == "inventory:supply")).one()
        assert audit.work_id is None
        assert audit.detail["inventory_resource_id"] == supply["id"]
        assert audit.detail["work_order"] == "W:AUDIT:1:0:0"
        assert audit.detail["reason"] == "Production forecast"
