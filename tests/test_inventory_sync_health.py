"""Operational recovery must never turn uncertain delivery into another receipt."""
from datetime import datetime, timedelta, timezone

import pytest

from fulfillment import inventory_sync as sync, model as m
from fulfillment.domain import Conflict
from fulfillment.inventory_connector import DeliveryError
from test_inventory import system, state, call, allocate
from test_inventory_sync import connector, MACHINE


def test_stale_source_blocks_stock_and_recovers_only_on_fresh_capture(system, tmp_path):
    db, _, service = system
    _, _, _, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    with db.transaction() as conn:
        current = m.get_setting(conn, "inventory_sync")
        current["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        m.put_setting(conn, "inventory_sync", current)
    view = state(db)
    assert view["sync"]["status"] == "stale" and not view["sync"]["fresh"]
    assert view["balances"][0]["available"] == "0"
    assert call(service, "allocate", allocate())[1] == 409
    with db.engine.connect() as conn, pytest.raises(Conflict, match="fresh valid"):
        sync.require_healthy(conn)
    assert producer.once()["accepted"]
    assert state(db)["sync"]["fresh"] and state(db)["balances"][0]["available"] == "10"


def test_invalid_source_reports_health_without_advancing_checkpoint(system, tmp_path):
    db, _, service = system
    source, receiver, outbox, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    def report(remote, code):
        payload = dict(source="visual", stream_id=remote["stream_id"], sequence=remote["sequence"], code=code)
        result, status = service.command(MACHINE, "inventory:sync-health", payload, "health-failure")
        assert status == 200, result
    receiver.report_failure = report
    capture = source.capture
    def invalid():
        raise Conflict("Source has held stock.", "inventory_source", 503)
    source.capture = invalid
    assert producer.once()["error"] == "capture_failed"
    assert outbox.inspect()["sequence"] == 1 and not outbox.inspect()["pending"]
    assert outbox.inspect()["last_error"]["message"] == "Source has held stock."
    view = state(db)
    assert view["sync"]["status"] == "source_error" and view["balances"][0]["available"] == "0"
    source.capture = capture
    assert producer.once()["accepted"]
    assert state(db)["sync"]["status"] == "matched"
    # A delayed failure report must not overwrite a newer accepted capture.
    result, status = service.command(MACHINE, "inventory:sync-health", dict(source="visual", stream_id=view["sync"]["stream_id"], sequence=1, code="capture_failed"), "late-health")
    assert status == 409 and result["error"] == "source_sequence"


def test_known_rejection_can_be_recaptured_but_uncertain_delivery_cannot(system, tmp_path):
    _, _, _ = system
    _, receiver, outbox, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    original = receiver.deliver
    def reject(payload):
        raise DeliveryError("invalid", "Source event needs correction", rejected=True)
    receiver.deliver = reject
    assert producer.once()["error"] == "invalid" and outbox.inspect()["pending"]
    assert producer.recapture_rejected()["recapture_ready"]
    assert not outbox.inspect()["pending"] and outbox.inspect()["sequence"] == 1
    receiver.deliver = original
    assert producer.once()["accepted"]
    receiver.lose_once = True
    assert producer.once()["error"] == "transport"
    with pytest.raises(Conflict, match="HTTP 4xx"):
        producer.recapture_rejected()
    assert outbox.inspect()["pending"]
    assert producer.once()["accepted"]


def test_recovery_refuses_to_drop_an_already_accepted_envelope(system, tmp_path):
    _, receiver, outbox, producer = connector(system, tmp_path)
    producer.once(bootstrap=True)
    receiver.lose_once = True
    producer.once()
    with outbox.transaction() as conn:
        outbox.put(conn, "last_error", dict(code="invalid", rejected=True))
    with pytest.raises(Conflict, match="Receiver advanced"):
        producer.recapture_rejected()
    assert producer.once()["accepted"]
