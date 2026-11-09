"""Daily close proves movements as well as balances; never writes to VISUAL."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import select

from fulfillment import daily_close as close, inventory as inv, inventory_sync as sync, model as m
from fulfillment.domain import Conflict
from fulfillment.inventory_connector import Connector, Outbox
from test_inventory import system, setup, ok, ADMIN
from test_inventory_sync import Source, Receiver

REVIEWER = dict(id="REVIEWER", role="supervisor")


def movement(identity, amount, **extra):
    return dict(id=str(identity), part="ACCESSORY-B", uom="EA", quantity=str(amount), serials=[],
                kind="receipt", work_order="", direction="I", category="R",
                effective_date=datetime.now(timezone.utc).date().isoformat(), source_created_local="2026-10-20 09:00:00",
                customer_order="", customer_order_line="", document="", document_line="",
                purchase_order="PO-1", purchase_order_line="1", transfer_id="", transfer_peer={}) | extra


class ModernSource(Source):
    def __init__(self):
        super().__init__()
        self.data.update(evidence_version=2, complete=True, movements=[movement(100, 10)])


class ConfirmedReceiver(Receiver):
    def confirm_delivery(self, state):
        with self.service.db.transaction() as conn:
            return sync.report_failure(conn, dict(source="visual", stream_id=state["stream_id"], sequence=state["sequence"], code="delivery_confirmed"))


def environment(system, tmp_path, receiver_class=ConfirmedReceiver):
    db, _, service = system
    setup(service)
    with db.transaction() as conn:
        for actor in (ADMIN, REVIEWER):
            conn.execute(m.users.insert().values(id=actor["id"], name=actor["id"], pin_hash="unused", role=actor["role"], enabled=True))
    source, receiver = ModernSource(), receiver_class(service)
    producer = Connector(source, receiver, Outbox(tmp_path / "close-outbox.db"))
    assert producer.once(bootstrap=True)["accepted"]
    return source, producer


def command(system, action, payload, actor=ADMIN):
    db, _, service = system
    with db.transaction() as conn:
        return close.execute(service, conn, actor, action, payload)


def prepare(system, **extra):
    return command(system, "prepare", dict(reason="Daily coordinated review", pause_confirmed=True) | extra)


def sign(system, record, **extra):
    return command(system, "sign", dict(id=record["id"], version=record["version"], reason="Independent review complete") | extra, REVIEWER)


def assign_all(system, record):
    for issue in list(record["exceptions"]):
        record = command(system, "exception", dict(id=record["id"], version=record["version"], exception_id=issue["id"],
            owner="ADMIN", due_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
            disposition="accepted_exception", resolution_reference="CASE-123", reason="Assigned documented investigation"))
    return record


def test_clean_opening_close_is_frozen_and_requires_independent_reviewer(system, tmp_path):
    source, producer = environment(system, tmp_path)
    prepared = prepare(system)
    assert prepared["evidence_status"] == "balanced" and not prepared["exceptions"]
    with pytest.raises(Conflict, match="different supervisor"):
        command(system, "sign", dict(id=prepared["id"], version=1, reason="Self review"))
    signed = sign(system, prepared)
    assert signed["approval_status"] == "signed" and signed["signed_by"] == "REVIEWER"
    assert signed["packet"] == prepared["packet"] and signed["digest"] == prepared["digest"]
    with pytest.raises(Conflict, match="immutable"):
        command(system, "exception", dict(id=signed["id"], version=signed["version"], reason="Edit signed close"))


def test_unchanged_healthy_poll_does_not_invalidate_material_review(system, tmp_path):
    _, producer = environment(system, tmp_path)
    prepared = prepare(system)
    assert producer.once()["accepted"]
    signed = sign(system, prepared)
    assert signed["followup_validation"]["sequence"] > signed["packet"]["source"]["sequence"]


def test_capture_must_be_acknowledged_and_cannot_represent_historical_midnight(system, tmp_path):
    environment(system, tmp_path, Receiver)
    prepared = prepare(system)
    assert prepared["evidence_status"] == "incomplete"
    assert any(i["kind"] == "delivery_unconfirmed" for i in prepared["exceptions"])
    with pytest.raises(Conflict, match="Missing or unconfirmed"):
        sign(system, prepared, allow_exceptions=True)
    with pytest.raises(Conflict, match="actual coordinated capture"):
        prepare(system, business_date="2001-01-01")
    with pytest.raises(Conflict, match="Confirm warehouse"):
        prepare(system, pause_confirmed=False)


def test_zero_net_missing_receipt_and_shipment_do_not_make_clean_close(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["movements"].extend([movement(101, 2, kind="other"), movement(102, -2, kind="shipment", direction="O", category="I",
        customer_order="SO-1", customer_order_line="1", document="PL-1", document_line="1")])
    assert producer.once()["accepted"]
    prepared = prepare(system)
    assert prepared["packet"]["reconciliation"]["status"] == "matched"
    assert prepared["evidence_status"] == "exceptions"
    assert {i["kind"] for i in prepared["exceptions"]} >= {"source_movement_unmatched", "source_shipment_unmatched"}
    with pytest.raises(Conflict, match="explicit exception sign-off"):
        sign(system, prepared)
    with pytest.raises(Conflict, match="Every exception"):
        sign(system, prepared, allow_exceptions=True)
    prepared = assign_all(system, prepared)
    signed = sign(system, prepared, allow_exceptions=True)
    assert signed["evidence_status"] == "exceptions" and signed["approval_status"] == "signed"


def test_matched_receipt_has_exact_two_way_movement_coverage(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["movements"].append(movement(99, 2))
    source.data["balances"][0]["quantity"] = "12"
    producer.once()
    prepared = prepare(system)
    assert prepared["evidence_status"] == "balanced"
    assert prepared["packet"]["movement_links"][0]["source_ids"] == ["99"]
    assert prepared["packet"]["ledger_equation"]["delta"][0]["quantity"] == "2"
    sign(system, prepared)
    again = prepare(system)
    assert again["evidence_status"] == "balanced" and not again["packet"]["ledger"]


def test_material_change_during_review_requires_reprepare(system, tmp_path):
    source, producer = environment(system, tmp_path)
    prepared = prepare(system)
    source.data["movements"].append(movement(101, 1))
    source.data["balances"][0]["quantity"] = "11"
    producer.once()
    with pytest.raises(Conflict, match="changed"):
        sign(system, prepared)


def test_missing_lineage_and_stale_source_are_incomplete(system, tmp_path):
    source, producer = environment(system, tmp_path)
    item = movement(101, 1)
    del item["effective_date"]
    source.data["movements"].append(item)
    source.data["balances"][0]["quantity"] = "11"
    producer.once()
    assert prepare(system)["evidence_status"] == "incomplete"
    db, _, _ = system
    with db.transaction() as conn:
        state = m.get_setting(conn, "inventory_sync")
        state["observed_at"] = (datetime.now(timezone.utc) - timedelta(minutes=6)).isoformat()
        m.put_setting(conn, "inventory_sync", state)
    assert prepare(system)["evidence_status"] == "incomplete"


def test_late_source_edit_flags_signed_close_without_rewriting_packet(system, tmp_path):
    source, producer = environment(system, tmp_path)
    signed = sign(system, prepare(system))
    frozen = deepcopy(signed["packet"])
    source.data["movements"][0]["purchase_order"] = "PO-CORRECTED"
    producer.once()
    db, _, _ = system
    with db.engine.connect() as conn:
        assert close.packet(conn, signed["id"])["packet"] == frozen
        impacts = close.snapshot(conn)["closes"][0]["impacts"]
        rows = inv.rows(conn, m.inventory_source_movements)
    assert impacts[0]["kind"] == "source_revision" and len(rows[0]["versions"]) == 2
    assert prepare(system)["evidence_status"] == "exceptions"


def test_deletions_are_retained_and_never_look_like_no_activity(system, tmp_path):
    source, producer = environment(system, tmp_path)
    sign(system, prepare(system))
    source.data["movements"] = []
    producer.once()
    db, _, _ = system
    with db.engine.connect() as conn:
        assert inv.rows(conn, m.inventory_source_movements)[0]["deleted"]
    assert any(i["kind"] == "source_revision" for i in prepare(system)["exceptions"])


def test_notes_cannot_resolve_a_stock_difference(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["balances"][0]["quantity"] = "9"
    producer.once()
    prepared = prepare(system)
    with pytest.raises(Conflict, match="Corrections require"):
        command(system, "exception", dict(id=prepared["id"], version=1, exception_id=prepared["exceptions"][0]["id"],
            owner="ADMIN", due_at=m.now(), disposition="resolved", reason="Count note says fixed"))


def test_role_revocation_is_rechecked_and_two_drafts_cannot_sign_same_interval(system, tmp_path):
    environment(system, tmp_path)
    one, two = prepare(system), prepare(system)
    sign(system, one)
    with pytest.raises(Conflict, match="Another close"):
        sign(system, two)
    db, _, _ = system
    with db.transaction() as conn:
        conn.execute(m.users.update().where(m.users.c.id == "ADMIN").values(enabled=False))
    with pytest.raises(Conflict, match="active supervisor"):
        prepare(system)


def test_supersede_retains_signed_original_until_replacement_signs(system, tmp_path):
    environment(system, tmp_path)
    old = sign(system, prepare(system))
    replacement = command(system, "supersede", dict(id=old["id"], version=old["version"], pause_confirmed=True, reason="Revised coordinated review"))
    db, _, _ = system
    with db.engine.connect() as conn:
        assert close.packet(conn, old["id"])["approval_status"] == "signed"
    replacement = sign(system, replacement)
    with db.engine.connect() as conn:
        retained = close.packet(conn, old["id"])
    assert retained["approval_status"] == "superseded" and retained["packet"] == old["packet"]


def test_manifest_tampering_rejects_atomic_batch(system, tmp_path):
    source, producer = environment(system, tmp_path)
    original = producer.receiver.deliver
    def tamper(payload):
        payload = deepcopy(payload)
        payload["movement_evidence"]["count"] += 1
        return original(payload)
    producer.receiver.deliver = tamper
    assert not producer.once()["accepted"]
    db, _, _ = system
    with db.engine.connect() as conn:
        assert sync.status(conn)["sequence"] == 1


def test_lineage_enrichment_upgrade_does_not_replay_receipts_or_create_edit_alert(system, tmp_path):
    db, _, service = system
    setup(service)
    old_source, receiver = Source(), ConfirmedReceiver(service)
    producer = Connector(old_source, receiver, Outbox(tmp_path / "upgrade.db"))
    producer.once(bootstrap=True)
    old_source.data.update(evidence_version=2, complete=True, movements=[movement(100, 10)])
    assert producer.once()["accepted"]
    with db.engine.connect() as conn:
        assert len(inv.rows(conn, m.inventory_ledger)) == 1
        assert not any(i["kind"] == "source_edit" for i in sync.status(conn)["issues"])


def test_direct_ledger_tampering_breaks_continuity(system, tmp_path):
    environment(system, tmp_path)
    db, _, _ = system
    with db.transaction() as conn:
        stock = inv.rows(conn, m.inventory_stock)[0]
        stock["quantity"] = "12"
        inv.save(conn, m.inventory_stock, stock)
    prepared = prepare(system)
    assert {i["kind"] for i in prepared["exceptions"]} >= {"ledger_discontinuity", "comparison_stale"}


def shipment(system, *, released=True):
    from fulfillment.domain import reconcile
    db, erp, _ = system
    work = dict(id="WORK-1", site="DEMO", order="SO-1", state="released" if released else "packing", version=1,
                operator="ADMIN", cart="CART-1", destination=deepcopy(erp.orders[0]["destination"]), packlist="PL-1",
                lines=[dict(id="L1", order_line="1", part="ACCESSORY-B", quantity="2", uom="EA", serials=[])],
                packages=[dict(id="PKG-1", tracking="TRACK-1")], inventory_owned=True)
    document = dict(id="PL-1", site="DEMO", order="SO-1", status="A", destination=work["destination"],
                    lines=[dict(packlist_line="1", order="SO-1", order_line="1", part="ACCESSORY-B", uom="EA", quantity="2", serials=[])])
    erp.packlists["PL-1"] = document
    work["verification"] = reconcile(work, document)
    if released:
        work["released_at"] = m.now()
    with db.transaction() as conn:
        conn.execute(m.works.insert().values(id=work["id"], state=work["state"], version=1, document=work, updated_at=m.now()))
        label = dict(tracking="TRACK-1", site="DEMO", order="SO-1", packlist="PL-1", package="PKG-1", destination=work["destination"], void=False)
        conn.execute(m.labels.insert().values(tracking="TRACK-1", document=label, received_at=m.now()))
        if released:
            stock = inv.rows(conn, m.inventory_stock)[0]
            stock["quantity"] = "8"
            inv.save(conn, m.inventory_stock, stock)
            inv.ledger(conn, ADMIN, "shipment", stock, -2, "PL-1", "Released fulfillment WORK-1")
    return work


def source_ship(source):
    source.data["movements"].append(movement(101, -2, kind="shipment", direction="O", category="I",
        customer_order="SO-1", customer_order_line="1", document="PL-1", document_line="1"))
    source.data["balances"][0]["quantity"] = "8"


def test_shipments_validate_exact_packlist_line_and_avoid_cumulative_order_delta(system, tmp_path):
    source, producer = environment(system, tmp_path)
    shipment(system)
    source_ship(source)
    producer.once()
    _, erp, _ = system
    def forbidden(*args, **kwargs):
        raise AssertionError("Later valid shipments may increase cumulative order totals; close must validate saved documents instead")
    erp.check_order = forbidden
    prepared = prepare(system)
    assert prepared["evidence_status"] == "balanced"
    assert prepared["packet"]["movement_links"][0]["kind"] == "shipment"
    sign(system, prepared)


def test_wrong_order_line_cannot_net_to_matching_shipment_total(system, tmp_path):
    source, producer = environment(system, tmp_path)
    shipment(system)
    source_ship(source)
    source.data["movements"][-1]["customer_order_line"] = "99"
    producer.once()
    prepared = prepare(system)
    assert prepared["packet"]["reconciliation"]["status"] == "matched"
    assert any(i["kind"] == "source_shipment_unmatched" for i in prepared["exceptions"])


def test_changed_or_missing_released_packlist_and_void_label_are_visible(system, tmp_path):
    source, producer = environment(system, tmp_path)
    shipment(system)
    source_ship(source)
    producer.once()
    prepared = prepare(system)
    db, erp, _ = system
    erp.packlists["PL-1"]["destination"]["postal"] = "99999"
    with pytest.raises(Conflict, match="changed"):
        sign(system, prepared)
    assert any(i["kind"] == "shipment_changed" for i in prepare(system)["exceptions"])
    del erp.packlists["PL-1"]
    assert prepare(system)["evidence_status"] == "incomplete"
    with db.transaction() as conn:
        label = conn.execute(select(m.labels.c.document)).scalar_one()
        label["void"] = True
        conn.execute(m.labels.update().values(document=label))
    assert any(i["kind"] == "label_changed" for i in prepare(system)["exceptions"])


def test_posted_before_release_carries_forward_and_matches_next_period(system, tmp_path):
    source, producer = environment(system, tmp_path)
    work = shipment(system, released=False)
    source_ship(source)
    producer.once()
    prepared = prepare(system)
    assert prepared["evidence_status"] == "exceptions" and len(prepared["packet"]["carry_forward"]) == 1
    sign(system, assign_all(system, prepared), allow_exceptions=True)
    db, _, _ = system
    with db.transaction() as conn:
        work.update(state="released", released_at=m.now(), version=2)
        conn.execute(m.works.update().where(m.works.c.id == work["id"]).values(state="released", version=2, document=work, updated_at=m.now()))
        stock = inv.rows(conn, m.inventory_stock)[0]
        stock["quantity"] = "8"
        inv.save(conn, m.inventory_stock, stock)
        inv.ledger(conn, ADMIN, "shipment", stock, -2, "PL-1", "Released fulfillment WORK-1")
    producer.once()
    next_close = prepare(system)
    assert next_close["evidence_status"] == "balanced"
    assert next_close["packet"]["movement_links"][0]["source_ids"] == ["101"]


def test_old_ledger_entry_edit_is_detected_even_when_current_stock_matches(system, tmp_path):
    environment(system, tmp_path)
    sign(system, prepare(system))
    db, _, _ = system
    with db.transaction() as conn:
        entry = inv.rows(conn, m.inventory_ledger)[0]
        entry["reason"] = "Rewritten historical attribution"
        inv.save(conn, m.inventory_ledger, entry)
    assert any(i["kind"] == "ledger_history_changed" for i in prepare(system)["exceptions"])


def test_correction_correspondence_requires_exact_quantity_and_independent_signoff(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["movements"].append(movement(101, -1, kind="other", direction="O", category="A"))
    source.data["balances"][0]["quantity"] = "9"
    producer.once()
    db, _, service = system
    ok(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="-1", reason="Approved damaged stock adjustment CASE-1"))
    producer.once()
    ok(service, "sync-ack", dict(id="MOVE:101", reason="Correction CASE-1 reviewed against count"))
    with db.engine.connect() as conn:
        entry = next(r for r in inv.rows(conn, m.inventory_ledger) if r["kind"] == "adjustment")
    mapping = command(system, "correspondence", dict(source_ids=["101"], ledger_ids=[entry["id"]], reference="CASE-1", reason="Same physical writeoff"))
    prepared = prepare(system)
    assert prepared["evidence_status"] == "balanced"
    assert prepared["packet"]["applied_correspondences"][0]["id"] == mapping["id"]
    signed = sign(system, prepared)
    with pytest.raises(Conflict, match="signed correspondence"):
        command(system, "correspondence-cancel", dict(id=mapping["id"], reason="Cannot erase signed proof"))
    assert signed["evidence_status"] == "balanced"


def test_source_correction_after_signed_exception_can_be_matched_next_day(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["movements"].append(movement(101, -1, kind="other", direction="O", category="A"))
    source.data["balances"][0]["quantity"] = "9"
    producer.once()
    sign(system, assign_all(system, prepare(system)), allow_exceptions=True)
    db, _, service = system
    ok(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="-1", reason="Approved count correction"))
    producer.once()
    ok(service, "sync-ack", dict(id="MOVE:101", reason="Resolved with correction CASE-1"))
    with db.engine.connect() as conn:
        entry = next(r for r in inv.rows(conn, m.inventory_ledger) if r["kind"] == "adjustment")
    command(system, "correspondence", dict(source_ids=["101"], ledger_ids=[entry["id"]], reference="CASE-1", reason="Reviewed correction"))
    assert prepare(system)["evidence_status"] == "balanced"


def test_wrong_amount_or_stale_source_cannot_use_correspondence(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["movements"].append(movement(101, -2, kind="other", direction="O", category="A"))
    source.data["balances"][0]["quantity"] = "8"
    producer.once()
    db, _, service = system
    ok(service, "adjust", dict(part="ACCESSORY-B", uom="EA", quantity="-1", reason="Partial count correction"))
    with db.engine.connect() as conn:
        entry = next(r for r in inv.rows(conn, m.inventory_ledger) if r["kind"] == "adjustment")
    with pytest.raises(Conflict, match="differ in quantity"):
        command(system, "correspondence", dict(source_ids=["101"], ledger_ids=[entry["id"]], reference="CASE-1", reason="Mismatch"))
    with pytest.raises(Conflict, match="Unchanged baseline"):
        command(system, "correspondence", dict(source_ids=["100"], ledger_ids=[entry["id"]], reference="CASE-1", reason="Wrong period"))


def test_metadata_correction_uses_no_inventory_entry_and_cannot_self_approve(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["movements"][0]["purchase_order"] = "PO-CORRECTED"
    producer.once()
    _, _, service = system
    ok(service, "sync-ack", dict(id="EDIT:100", reason="Corrected source purchase reference CASE-2"))
    mapping = command(system, "correspondence", dict(source_ids=["100"], ledger_ids=[], metadata_only=True, reference="CASE-2", reason="No stock delta"), REVIEWER)
    prepared = prepare(system)
    assert prepared["evidence_status"] == "balanced" and not prepared["packet"]["ledger"]
    with pytest.raises(Conflict, match="movement correspondences"):
        sign(system, prepared)
    command(system, "correspondence-cancel", dict(id=mapping["id"], reason="Return for independent preparation"))
    assert prepare(system)["evidence_status"] == "exceptions"


def test_wrong_packlist_line_is_not_hidden_by_matching_order_line(system, tmp_path):
    source, producer = environment(system, tmp_path)
    shipment(system)
    source_ship(source)
    source.data["movements"][-1]["document_line"] = "99"
    producer.once()
    assert any(i["kind"] == "source_shipment_unmatched" for i in prepare(system)["exceptions"])


def test_metadata_review_cannot_hide_zero_net_physical_movements(system, tmp_path):
    source, producer = environment(system, tmp_path)
    source.data["movements"].extend([movement(101, 2, kind="other"), movement(102, -2, kind="other", direction="O")])
    producer.once()
    with pytest.raises(Conflict, match="cannot net physical"):
        command(system, "correspondence", dict(source_ids=["101", "102"], ledger_ids=[], metadata_only=True,
            reference="CASE-3", reason="Not sufficient proof"))


def test_later_document_change_annotates_original_signed_close(system, tmp_path):
    source, producer = environment(system, tmp_path)
    shipment(system)
    source_ship(source)
    producer.once()
    signed = sign(system, prepare(system))
    db, erp, _ = system
    erp.packlists["PL-1"]["status"] = "X"
    prepare(system)
    with db.engine.connect() as conn:
        assert close.packet(conn, signed["id"])["packet"] == signed["packet"]
        impacts = m.get_setting(conn, "daily_close_impacts")[signed["id"]]
    assert any(i["kind"] == "shipment_evidence_changed" for i in impacts)
    source.data["movements"][0]["purchase_order"] = "PO-EDIT"
    assert producer.once()["accepted"]  # Source and document annotations coexist.
