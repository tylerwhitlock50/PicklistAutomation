"""Frozen warehouse close evidence, independent review, and retained exceptions.

A clean balance is necessary but insufficient: identities, movement quantities,
source completeness and current shipment documents must agree in both directions.
No function posts to VISUAL or changes inventory to make a close balance.
"""
from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select

from fulfillment import inventory as inv, inventory_sync as sync, model as m
from fulfillment.domain import Conflict, address, identifier, key, reconcile, require


def packet(conn, close_id):
    return inv.get(conn, m.daily_closes, close_id)


def snapshot(conn):
    impacts = m.get_setting(conn, "daily_close_impacts", {})
    records = sorted(inv.rows(conn, m.daily_closes), key=lambda r: r["prepared_at"], reverse=True)
    fields = ("id", "version", "business_date", "timezone", "cutoff", "evidence_status", "approval_status",
              "prepared_by", "prepared_at", "signed_by", "signed_at", "digest", "supersedes")
    return dict(closes=[{**{k: r.get(k) for k in fields}, "exception_count": len(r["exceptions"]),
                         "impacts": impacts.get(r["id"], [])} for r in records], source=sync.status(conn),
                correspondences=list(m.get_setting(conn, "inventory_movement_correspondences", {}).values()),
                requirements="Pause warehouse movement, refresh the connector, prepare, assign exceptions, then have a different supervisor sign.")


def mark_source_impact(conn, record, old, observed):
    """Add an external annotation; never mutate a signed packet or its digest."""
    impacts = m.get_setting(conn, "daily_close_impacts", {})
    changed = False
    movement = record.get("movement") or {}
    for close in inv.rows(conn, m.daily_closes):
        if close["approval_status"] not in ("signed", "superseded"):
            continue
        prior = close["packet"]["source_index"].get(record["source_id"])
        edited = prior is not None and prior != dict(hash=record["hash"], deleted=record["deleted"])
        backdated = prior is None and movement.get("effective_date") and movement["effective_date"] <= close["business_date"]
        if not (edited or backdated):
            continue
        item = dict(source_id=record["source_id"], observed_at=observed,
                    kind="source_revision" if edited else "late_source_movement",
                    effective_date=movement.get("effective_date"), hash=record["hash"], deleted=record["deleted"])
        entries = impacts.setdefault(close["id"], [])
        if not any(e.get("source_id") == item["source_id"] and e.get("hash") == item["hash"] and e.get("deleted") == item["deleted"] for e in entries):
            entries.append(item)
            changed = True
    if changed:
        m.put_setting(conn, "daily_close_impacts", impacts)



def _mark_document_impacts(conn, documents):
    impacts = m.get_setting(conn, "daily_close_impacts", {})
    changed = False
    for close in inv.rows(conn, m.daily_closes):
        if close["approval_status"] not in ("signed", "superseded"):
            continue
        recorded = {r["work_id"]: r for r in close["packet"].get("shipment_evidence", [])}
        for current in documents:
            before = recorded.get(current["work_id"])
            if before is None or m.digest(before) == m.digest(current):
                continue
            item = dict(kind="shipment_evidence_changed", work_id=current["work_id"], packlist=current["packlist"],
                        observed_at=m.now(), hash=m.digest(current), message="Released document or label evidence changed after this close.")
            entries = impacts.setdefault(close["id"], [])
            if not any(e.get("work_id") == item["work_id"] and e.get("hash") == item["hash"] for e in entries):
                entries.append(item)
                changed = True
    if changed:
        m.put_setting(conn, "daily_close_impacts", impacts)


def _issue(kind, reference, message, *, incomplete=False, evidence=None):
    return dict(id=key(kind, reference), kind=kind, reference=str(reference), message=message,
                incomplete=incomplete, evidence=evidence or {}, owner=None, due_at=None,
                disposition="open", resolution_reference=None)


def _stock_map(rows):
    return {(r["part"], r["uom"], r.get("serial", "")): Decimal(r["quantity"]) for r in rows}


def _encoded_map(values):
    return [dict(part=k[0], uom=k[1], serial=k[2], quantity=str(v)) for k, v in sorted(values.items())]


def _vector(rows, *, source=False):
    result = defaultdict(Decimal)
    for row in rows:
        if source and row.get("serials"):
            for item in row["serials"]:
                result[(row["part"], row["uom"], item["serial"])] += Decimal(item["quantity"])
        else:
            result[(row["part"], row["uom"], row.get("serial", ""))] += Decimal(row["quantity"])
    return dict(result)


def _equal(left, right):
    return {k: v for k, v in left.items() if v} == {k: v for k, v in right.items() if v}


def _shipment_evidence(service, conn, works, issues):
    documents, seen = [], {}
    for work in works:
        if work["state"] != "released":
            continue
        packlist = work.get("packlist")
        if not packlist:
            issues.append(_issue("shipment_document_missing", work["id"], "Released work has no packlist.", incomplete=True))
            continue
        if packlist in seen:
            issues.append(_issue("duplicate_packlist", packlist, "One packlist is attributed to multiple fulfillments.", evidence=dict(work_ids=[seen[packlist], work["id"]])))
        seen[packlist] = work["id"]
        proof = dict(work_id=work["id"], packlist=packlist, order=work["order"], labels=[])
        try:
            document = service.erp.packlist(packlist, work)
            result = reconcile(work, document)
            proof.update(document=document, result=result, observed_status=document.get("status"))
            if result["result"] != "pass" or result["fingerprint"] != (work.get("verification") or {}).get("fingerprint"):
                issues.append(_issue("shipment_changed", work["id"], "Current VISUAL packlist differs from the released evidence.", evidence=proof))
        except Conflict as exc:
            proof["error"] = dict(code=exc.code, message=exc.message)
            issues.append(_issue("shipment_document_unavailable", work["id"], exc.message, incomplete=True, evidence=proof))
        except Exception:
            proof["error"] = dict(code="source_unavailable")
            issues.append(_issue("shipment_document_unavailable", work["id"], "Could not read current VISUAL shipment evidence.", incomplete=True))
        if not work.get("packages"):
            issues.append(_issue("label_missing", work["id"], "Released work has no retained package/label evidence.", incomplete=True))
        for package in work.get("packages", []):
            tracking = package.get("tracking")
            label = conn.execute(select(m.labels.c.document).where(m.labels.c.tracking == tracking)).scalar() if tracking else None
            label_proof = dict(package=package["id"], tracking=tracking, document=label)
            if label is None:
                issues.append(_issue("label_missing", work["id"] + ":" + package["id"], "Released package has no retained label evidence.", incomplete=True))
            else:
                valid = not label.get("void") and all(label.get(f) == v for f, v in (
                    ("site", work["site"]), ("order", work["order"]), ("packlist", packlist), ("package", package["id"])))
                valid = valid and address(label.get("destination", {})) == address(work["destination"])
                if not valid:
                    issues.append(_issue("label_changed", tracking, "Label is voided or no longer matches the released package.", evidence=label_proof))
                try:
                    if service.live:
                        service.erp.check_label(label)
                    label_proof["live_validated"] = service.live
                except Conflict as exc:
                    label_proof["error"] = dict(code=exc.code, message=exc.message)
                    issues.append(_issue("label_changed", tracking, exc.message, evidence=label_proof))
                except Exception:
                    label_proof["error"] = dict(code="source_unavailable")
                    issues.append(_issue("label_unavailable", tracking, "Current label status could not be read.", incomplete=True))
            proof["labels"].append(label_proof)
        documents.append(proof)
    return documents


def _movement_coverage(ledger, source_rows, previous_index, works, issues, previously_covered, correspondences):
    changes = [r for r in source_rows if r["source_id"] not in previously_covered or previous_index.get(r["source_id"]) != dict(hash=r["hash"], deleted=r["deleted"])]
    for row in changes:
        movement = row.get("movement") or {}
        required = ("direction", "category", "effective_date", "source_created_local", "customer_order", "customer_order_line", "document", "document_line", "purchase_order", "purchase_order_line", "transfer_id", "transfer_peer")
        if any(field not in movement for field in required) or not movement.get("effective_date") or not movement.get("source_created_local"):
            issues.append(_issue("source_lineage_missing", row["source_id"], "Movement lineage/effective date is incomplete.", incomplete=True, evidence=movement))
    covered, links = set(), []
    mapped_sources, applied = set(), []
    selected = {r["source_id"]: r for r in changes}
    ledger_by_id = {r["id"]: r for r in ledger}
    for mapping in correspondences:
        if mapping.get("status") == "cancelled" or not set(mapping["source_ids"]).intersection(selected):
            continue
        valid = all(identity in selected and dict(hash=selected[identity]["hash"], deleted=selected[identity]["deleted"]) == mapping["source_current"][identity]
                    and previous_index.get(identity) == mapping["source_basis"].get(identity) for identity in mapping["source_ids"])
        valid = valid and all(identity in ledger_by_id and m.digest(ledger_by_id[identity]) == mapping["ledger_index"][identity] for identity in mapping["ledger_ids"])
        if not valid:
            issues.append(_issue("correspondence_stale", mapping["id"], "Movement correspondence no longer matches its reviewed source/ledger versions.", evidence=mapping))
            continue
        require(not mapped_sources.intersection(mapping["source_ids"]) and not covered.intersection(mapping["ledger_ids"]), "Movement correspondence overlaps another mapping.", "correspondence")
        mapped_sources.update(mapping["source_ids"])
        covered.update(mapping["ledger_ids"])
        applied.append(mapping)
        links.append(dict(source_ids=mapping["source_ids"], ledger_ids=mapping["ledger_ids"], kind="reviewed_correspondence", correspondence_id=mapping["id"]))
    by_receipt, by_ship = defaultdict(list), defaultdict(list)
    for entry in ledger:
        if entry["kind"] == "receipt":
            by_receipt[entry["reference"]].append(entry)
        elif entry["kind"] == "shipment":
            by_ship[entry["reference"]].append(entry)
        elif entry["id"] not in covered:
            issues.append(_issue("app_adjustment_posting_gap", entry["id"], "Adjustment requires corresponding VISUAL movement evidence and a reviewed correction reference.", evidence=entry))
    ship_sources = defaultdict(list)
    for row in changes:
        movement, identity = row.get("movement"), row["source_id"]
        if identity in mapped_sources:
            continue
        if row["deleted"] or (identity in previous_index and previous_index[identity] != dict(hash=row["hash"], deleted=row["deleted"])):
            issues.append(_issue("source_revision", identity, "A previously closed/baselined source movement changed or disappeared; review the prior-period impact.", evidence=row))
            continue
        if not movement:
            issues.append(_issue("source_lineage_missing", identity, "Source movement details are unavailable.", incomplete=True, evidence=row))
            continue
        if movement["kind"] == "receipt":
            local = by_receipt.get("IT:" + identity, [])
            if local and _equal(_vector(local), _vector([movement], source=True)):
                covered.update(e["id"] for e in local)
                links.append(dict(source_ids=[identity], ledger_ids=[e["id"] for e in local], kind="receipt"))
            else:
                issues.append(_issue("source_receipt_unmatched", identity, "VISUAL receipt has no exact app receipt with matching quantity and serial identities.", evidence=dict(source=movement, app=local)))
        elif movement["kind"] == "shipment":
            if not all(movement.get(k) for k in ("customer_order", "customer_order_line", "document", "document_line")):
                issues.append(_issue("shipment_lineage_missing", identity, "Shipment is missing its exact order, line or packlist linkage.", incomplete=True, evidence=movement))
            ship_sources[movement.get("document", "")].append(movement)
        else:
            issues.append(_issue("source_movement_unmatched", identity, "VISUAL transfer, correction, return or other movement needs an explicit reviewed correspondence.", evidence=movement))
    for document, movements in ship_sources.items():
        local = by_ship.get(document, [])
        candidates = [w for w in works if w.get("packlist") == document and w["state"] == "released"]
        valid = len(candidates) == 1 and bool(local) and _equal(_vector(local), _vector(movements, source=True))
        if valid:
            expected, actual = defaultdict(Decimal), defaultdict(Decimal)
            work = candidates[0]
            units = {(str(line["order_line"]), line["part"]): line["uom"] for line in work["lines"]}
            for line in (work.get("verification") or {}).get("bindings", []):
                for serial in line.get("serials") or [""]:
                    expected[(work["order"], str(line["order_line"]), line["part"], units.get((str(line["order_line"]), line["part"])), str(line["packlist_line"]), serial)] -= Decimal(1) if serial else Decimal(line["quantity"])
            for movement in movements:
                for serial, amount in [(s["serial"], Decimal(s["quantity"])) for s in movement["serials"]] or [("", Decimal(movement["quantity"]))]:
                    actual[(movement.get("customer_order"), movement.get("customer_order_line"), movement["part"], movement["uom"], movement.get("document_line"), serial)] += amount
            valid = _equal(expected, actual)
        if valid:
            covered.update(e["id"] for e in local)
            links.append(dict(source_ids=[r["id"] for r in movements], ledger_ids=[e["id"] for e in local], kind="shipment", work_id=candidates[0]["id"]))
        else:
            issues.append(_issue("source_shipment_unmatched", document or m.digest(movements), "VISUAL shipment lacks an exact released Dispatch shipment/line match. It may be posted while work remains picked.", evidence=dict(source=movements, app=local)))
    for entry in ledger:
        if entry["id"] not in covered and entry["kind"] in ("receipt", "shipment"):
            issues.append(_issue("app_movement_posting_gap", entry["id"], "App movement has no exact corresponding VISUAL movement in this close interval.", evidence=entry))
    return changes, links, applied



def _correspondence(service, conn, actor, payload, *, cancel=False):
    mappings = m.get_setting(conn, "inventory_movement_correspondences", {})
    reason = inv.reason(payload)
    if cancel:
        record = mappings.get(payload.get("id"))
        require(record is not None and record.get("status") != "cancelled", "Active movement correspondence not found.", "not_found", 404)
        signed = [c for c in inv.rows(conn, m.daily_closes) if c["approval_status"] in ("signed", "superseded")]
        require(not any(record["id"] in [a["id"] for a in c["packet"].get("applied_correspondences", [])] for c in signed),
                "A signed correspondence is retained evidence; correct it through a new source/ledger revision.", "close_signed")
        record.update(status="cancelled", cancelled_by=actor["id"], cancelled_at=m.now(), cancellation_reason=reason)
        m.put_setting(conn, "inventory_movement_correspondences", mappings)
        return record
    source_ids, ledger_ids = payload.get("source_ids"), payload.get("ledger_ids", [])
    require(isinstance(source_ids, list) and 0 < len(source_ids) <= 100 and isinstance(ledger_ids, list) and len(ledger_ids) <= 100,
            "Provide 1–100 source movement IDs and up to 100 ledger entry IDs.", "invalid", 400)
    source_ids, ledger_ids = [identifier(i) for i in source_ids], [str(i) for i in ledger_ids]
    require(len(set(source_ids)) == len(source_ids) and len(set(ledger_ids)) == len(ledger_ids), "Duplicate correspondence identity.", "duplicate")
    reference = str(payload.get("reference") or "").strip()
    require(0 < len(reference) <= 1000, "Record the supporting correction/evidence reference.", "invalid", 400)
    metadata_only = payload.get("metadata_only") is True
    require(bool(ledger_ids) != metadata_only, "A quantity correspondence needs ledger entries; metadata-only review must not invent a stock entry.", "correspondence")
    active = [r for r in mappings.values() if r.get("status") != "cancelled"]
    require(not any(set(ledger_ids).intersection(r["ledger_ids"]) for r in active), "A ledger entry already belongs to another correspondence.", "duplicate")
    source_rows = {r["source_id"]: r for r in inv.rows(conn, m.inventory_source_movements)}
    require(set(source_ids).issubset(source_rows), "Source movement is not retained in the complete connector history.", "source_evidence")
    ledger = {r["id"]: r for r in inv.rows(conn, m.inventory_ledger)}
    require(set(ledger_ids).issubset(ledger), "Ledger entry not found.", "not_found", 404)
    require(all(ledger[i]["kind"] == "adjustment" for i in ledger_ids), "Only authorized adjustment entries can be explicitly matched. Receipts and shipments use exact automatic identity links.", "correspondence")
    signed = [r for r in inv.rows(conn, m.daily_closes) if r["approval_status"] == "signed"]
    prior = max(signed, key=lambda r: r["cutoff"]) if signed else None
    basis = prior["packet"]["source_index"] if prior else m.get_setting(conn, "inventory_opening", {}).get("movement_index", {})
    covered_source = set(prior["packet"].get("covered_source_ids", []) if prior else basis)
    covered_ledger = set(prior["packet"].get("covered_ledger_ids", []) if prior else m.get_setting(conn, "inventory_opening", {}).get("ledger_ids", []))
    require(not covered_ledger.intersection(ledger_ids), "A ledger entry already belongs to a signed closing baseline.", "duplicate")
    source_delta, source_current, source_basis = defaultdict(Decimal), {}, {}
    directional_source = defaultdict(Decimal)
    for identity in source_ids:
        row = source_rows[identity]
        current = dict(hash=row["hash"], deleted=row["deleted"])
        require(not any(identity in r["source_ids"] and r["source_current"][identity] == current for r in active), "This source revision already has a correspondence.", "duplicate")
        previous = basis.get(identity) if identity in covered_source else None
        source_current[identity], source_basis[identity] = current, basis.get(identity)
        require(previous != current, "Unchanged baseline movements cannot be matched to new adjustments.", "correspondence")
        before = next((v["movement"] for v in row["versions"] if previous and v["hash"] == previous["hash"] and v["deleted"] == previous["deleted"]), None)
        require(not previous or previous["deleted"] or before is not None, "The earlier source revision is unavailable; cannot prove its quantity change.", "source_evidence")
        after = None if row["deleted"] else row["movement"]
        old_values = [before] if before and previous and not previous.get("deleted") else []
        individual_delta = defaultdict(Decimal)
        for values, sign in (([after] if after else [], 1), (old_values, -1)):
            for stock_identity, amount in _vector(values, source=True).items():
                individual_delta[stock_identity] += sign * amount
        require(not metadata_only or not any(individual_delta.values()), "Metadata-only review cannot net physical inbound and outbound movements to zero.", "correspondence")
        for stock_identity, amount in individual_delta.items():
            source_delta[stock_identity] += amount
            if amount:
                directional_source[(*stock_identity, 1 if amount > 0 else -1)] += abs(amount)
        if any(r and r.get("kind") == "shipment" for r in (before, after)):
            require(metadata_only and after and after.get("kind") == "shipment", "Shipment posting gaps must resolve through the fulfillment document/release flow, never a balancing adjustment.", "correspondence")
            works = [w for w in service.list_work(conn) if w["state"] == "released" and w.get("packlist") == after.get("document") and w["order"] == after.get("customer_order")]
            require(len(works) == 1 and any(str(b["packlist_line"]) == after.get("document_line") and str(b["order_line"]) == after.get("customer_order_line")
                and b["part"] == after["part"] and Decimal(b["quantity"]) == -Decimal(after["quantity"])
                and sorted(b["serials"]) == sorted(t["serial"] for t in after["serials"])
                for b in (works[0].get("verification") or {}).get("bindings", [])),
                "Corrected shipment lineage must match its retained released packlist line exactly.", "correspondence")
    directional_ledger = defaultdict(Decimal)
    for identity in ledger_ids:
        entry = ledger[identity]
        amount = Decimal(entry["quantity"])
        directional_ledger[(entry["part"], entry["uom"], entry["serial"], 1 if amount > 0 else -1)] += abs(amount)
    require(_equal(dict(directional_source), dict(directional_ledger)), "Source changes and ledger adjustments differ in quantity, direction, units or serial identities.", "correspondence")
    record = dict(id=str(uuid4()), source_ids=source_ids, ledger_ids=ledger_ids, source_current=source_current,
                  source_basis=source_basis, ledger_index={i: m.digest(ledger[i]) for i in ledger_ids},
                  quantity_evidence=_encoded_map(source_delta), metadata_only=metadata_only, reference=reference,
                  reason=reason, created_by=actor["id"], created_at=m.now(), status="proposed")
    mappings[record["id"]] = record
    m.put_setting(conn, "inventory_movement_correspondences", mappings)
    return record


def _collect(service, conn, baseline_id=None):
    state = sync.status(conn)
    opening = m.get_setting(conn, "inventory_opening", {})
    baseline = packet(conn, baseline_id) if baseline_id else None
    previous_index = baseline["packet"]["source_index"] if baseline else opening.get("movement_index", {})
    opening_stock = baseline["packet"]["closing_stock"] if baseline else opening.get("stock", [])
    previous_ledger = set(baseline["packet"]["ledger_boundary"] if baseline else opening.get("ledger_ids", []))
    previous_cutoff = baseline["cutoff"] if baseline else opening.get("observed_at")
    previous_ledger_index = baseline["packet"].get("ledger_index", {}) if baseline else opening.get("ledger_index", {})
    all_ledger = sorted(inv.rows(conn, m.inventory_ledger), key=lambda r: (r["at"], r["id"]))
    ledger = [r for r in all_ledger if r["id"] not in previous_ledger]
    stocks = sorted(inv.rows(conn, m.inventory_stock), key=lambda r: r["id"])
    source_rows = sorted(inv.rows(conn, m.inventory_source_movements), key=lambda r: r["source_id"])
    source_index = sync.movement_index(conn)
    issues = []
    if not opening or not opening.get("evidence", {}).get("complete"):
        issues.append(_issue("opening_evidence_missing", "opening", "A complete verified opening source and ledger baseline is unavailable; review the connector cutover.", incomplete=True))
    if not state.get("connected") or not state.get("fresh") or not state.get("movement_evidence", {}).get("complete"):
        issues.append(_issue("source_incomplete", "source", "A fresh, complete and healthy source capture is required.", incomplete=True))
    if state.get("delivery_confirmed_sequence") != state.get("sequence"):
        issues.append(_issue("delivery_unconfirmed", "source", "Connector durable checkpoint acknowledgement is missing; pending delivery cannot be excluded.", incomplete=True))
    comparisons = inv.rows(conn, m.inventory_reconciliations)
    comparison = next((r for r in comparisons if r["id"] == state.get("reconciliation_id")), None)
    if not comparison or comparison["fingerprint"] != inv.fingerprint(conn):
        issues.append(_issue("comparison_stale", "source", "Current local stock has not been compared with the complete source snapshot.", incomplete=True))
    elif comparison["differences"]:
        for difference in comparison["differences"]:
            issues.append(_issue("balance_difference", key(difference["part"], difference["uom"], difference["serial"]), "VISUAL and Dispatch stock differ.", evidence=difference))
    by_id = {r["id"]: r for r in all_ledger}
    if any(identity not in by_id or m.digest(by_id[identity]) != expected for identity, expected in previous_ledger_index.items()):
        issues.append(_issue("ledger_history_changed", "ledger", "Previously closed ledger evidence was edited or removed.", incomplete=True))
    if not previous_ledger.issubset(by_id):
        issues.append(_issue("ledger_history_missing", "ledger", "Previously closed ledger entries are missing.", incomplete=True))
    opening_map, closing_map = _stock_map(opening_stock), _stock_map(stocks)
    computed = defaultdict(Decimal, opening_map)
    for row in ledger:
        computed[(row["part"], row["uom"], row["serial"])] += Decimal(row["quantity"])
    if not _equal(computed, closing_map):
        issues.append(_issue("ledger_discontinuity", "ledger", "Opening plus retained ledger movements does not equal closing stock.", evidence=dict(computed=_encoded_map(computed), actual=_encoded_map(closing_map))))
    all_works = sorted(service.list_work(conn), key=lambda w: w["id"])
    relevant_works = [w for w in all_works if w["state"] != "released" or not opening.get("observed_at") or w.get("released_at", "") >= opening["observed_at"]]
    source_covered = set(baseline["packet"].get("covered_source_ids", []) if baseline else previous_index)
    ledger_covered = set(baseline["packet"].get("covered_ledger_ids", []) if baseline else previous_ledger)
    coverage_ledger = [r for r in all_ledger if r["id"] not in ledger_covered]
    correspondences = list(m.get_setting(conn, "inventory_movement_correspondences", {}).values())
    changes, links, applied = _movement_coverage(coverage_ledger, source_rows, previous_index, relevant_works, issues, source_covered, correspondences)
    source_covered.update(i for link in links for i in link["source_ids"])
    ledger_covered.update(i for link in links for i in link["ledger_ids"])
    documents = _shipment_evidence(service, conn, relevant_works, issues)
    _mark_document_impacts(conn, documents)
    carry = [deepcopy(w) for w in all_works if w["state"] not in ("released", "cancelled")]
    for work in carry:
        if work.get("exception"):
            issues.append(_issue("work_exception", work["id"], "Active fulfillment has an unresolved exception.", evidence=work))
    sync_issues = sorted(state.get("issues", []), key=lambda r: r["id"])
    for item in sync_issues:
        issues.append(_issue("sync_issue", item["id"], item["message"], evidence=item))
    # Separate preserved evidence from the material signature. Observation times,
    # sequence-only healthy polls and documentary local refresh timestamps do not
    # invalidate the same business boundary; all actual quantities and identities do.
    material = dict(config=inv.config(conn), stock=stocks, ledger=all_ledger, source_index=source_index,
                    source_balances=state.get("balances"), works=all_works, documents=documents,
                    allocations=sorted(inv.rows(conn, m.inventory_allocations), key=lambda a: a["id"]), sync_issues=sync_issues,
                    correspondences=applied,
                    previous_close=baseline_id)
    result = dict(previous_close_id=baseline_id, previous_cutoff=previous_cutoff,
                  opening_stock=deepcopy(opening_stock), ledger=ledger, ledger_boundary=[r["id"] for r in all_ledger],
                  ledger_index={r["id"]: m.digest(r) for r in all_ledger},
                  ledger_equation=dict(opening=_encoded_map(opening_map), delta=_encoded_map(_vector(ledger)), closing=_encoded_map(closing_map)),
                  closing_stock=stocks, source=state, reconciliation=comparison, source_index=source_index,
                  source_changes=changes, movement_links=links, applied_correspondences=applied, shipment_evidence=documents,
                  covered_source_ids=sorted(source_covered), covered_ledger_ids=sorted(ledger_covered),
                  carry_forward=carry, commitments=inv.active_allocations(conn), material_digest=m.digest(material),
                  limitations=["Status A/S/P verifies the saved shipment document, not invoicing, financial posting or carrier departure.",
                               "The recorded pause is an operator attestation; external VISUAL writers cannot be locked by Dispatch."])
    return result, issues


def _status(issues):
    return "incomplete" if any(i["incomplete"] for i in issues) else "exceptions" if issues else "balanced"


def _timezone(conn):
    name = m.get_setting(conn, "administration", {}).get("rules", {}).get("timezone", "America/Denver")
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError:
        raise Conflict("Configure a valid warehouse timezone before closing.", "configuration")


def _prepare(service, conn, actor, payload, supersedes=None):
    require(payload.get("pause_confirmed") is True, "Confirm warehouse movements are paused for the coordinated capture and review.", "pause_required")
    reason = inv.reason(payload)
    signed = [r for r in inv.rows(conn, m.daily_closes) if r["approval_status"] == "signed" and r["id"] != supersedes]
    prior = max(signed, key=lambda r: r["cutoff"]) if signed else None
    if supersedes:
        replaced = packet(conn, supersedes)
        prior_id = replaced["packet"]["previous_close_id"]
    else:
        prior_id = prior["id"] if prior else None
    evidence, issues = _collect(service, conn, prior_id)
    tz_name, tz = _timezone(conn)
    cutoff = evidence["source"].get("observed_at") or m.now()
    business_date = datetime.fromisoformat(cutoff).astimezone(tz).date().isoformat()
    require(not payload.get("business_date") or payload["business_date"] == business_date,
            "Close date must match the actual coordinated capture in the warehouse timezone; arbitrary historical snapshots cannot be reconstructed.", "cutoff")
    if prior:
        require(cutoff >= prior["cutoff"], "The source capture precedes the previous signed close.", "cutoff")
    result = dict(id=str(uuid4()), version=1, business_date=business_date, timezone=tz_name, cutoff=cutoff,
                  prepared_at=m.now(), prepared_by=actor["id"], reason=reason, pause_confirmed=True,
                  evidence_status=_status(issues), approval_status="draft", packet=evidence,
                  exceptions=issues, supersedes=supersedes, review_history=[])
    result["digest"] = m.digest(dict(packet=evidence, exceptions=issues, cutoff=cutoff, business_date=business_date, timezone=tz_name))
    inv.save(conn, m.daily_closes, result)
    return result


def _revision(conn, payload):
    record = packet(conn, payload.get("id"))
    require(record["version"] == payload.get("version"), "The close revision changed. Reload and review it again.", "revision")
    require(record["approval_status"] == "draft", "A signed close is immutable; prepare a superseding version.", "close_signed")
    return record


def execute(service, conn, actor, action, payload):
    user = conn.execute(select(m.users).where(m.users.c.id == actor.get("id"))).mappings().first()
    require(user and user["enabled"] and user["role"] in ("admin", "supervisor"),
            "An active supervisor must prepare or sign a close.", "role", 403)
    if action in ("correspondence", "correspondence-cancel"):
        return _correspondence(service, conn, actor, payload, cancel=action == "correspondence-cancel")
    if action == "prepare":
        return _prepare(service, conn, actor, payload)
    if action == "supersede":
        previous = packet(conn, payload.get("id"))
        require(previous["version"] == payload.get("version") and previous["approval_status"] == "signed",
                "Only the current signed revision can be superseded.", "revision")
        later = [r for r in inv.rows(conn, m.daily_closes) if r["approval_status"] == "signed" and r["cutoff"] > previous["cutoff"]]
        require(not later, "A later close depends on this baseline. Record a current-period correction and retain the earlier impact.", "close_dependency")
        return _prepare(service, conn, actor, payload, supersedes=previous["id"])
    record = _revision(conn, payload)
    why = inv.reason(payload)
    if action == "exception":
        item = next((i for i in record["exceptions"] if i["id"] == payload.get("exception_id")), None)
        require(item is not None, "Close exception not found.", "not_found", 404)
        owner = identifier(payload.get("owner"), "Responsible owner")
        owner_row = conn.execute(select(m.users).where(m.users.c.id == owner)).mappings().first()
        require(owner_row and owner_row["enabled"], "Assign an active named user to the exception.", "owner")
        due = inv.timestamp(payload.get("due_at"), "Exception due time")
        disposition = payload.get("disposition", "open")
        require(disposition in ("open", "accepted_exception"), "Corrections require new verified evidence; a note cannot mark a difference resolved.", "evidence")
        reference = str(payload.get("resolution_reference") or "").strip()
        require(len(reference) <= 1000, "Correction reference is too long.", "invalid", 400)
        item.update(owner=owner, due_at=due, disposition=disposition, resolution_reference=reference or None)
        record["review_history"].append(dict(at=m.now(), actor=actor["id"], reason=why, exception_id=item["id"], changes=deepcopy(item)))
    elif action == "sign":
        require(all(mapping["created_by"] != actor["id"] for mapping in record["packet"].get("applied_correspondences", [])),
                "A different supervisor must review movement correspondences created by the signer.", "self_approval", 403)
        require(actor["id"] != record["prepared_by"], "A different supervisor must independently review and sign this close.", "self_approval", 403)
        evidence, current_issues = _collect(service, conn, record["packet"]["previous_close_id"])
        require(evidence["material_digest"] == record["packet"]["material_digest"],
                "Stock, source movement, work, commitments or shipment evidence changed. Prepare and review a new close.", "close_stale")
        require(_status(current_issues) != "incomplete" and record["evidence_status"] != "incomplete",
                "Missing or unconfirmed evidence prevents sign-off. Refresh evidence and prepare again.", "close_incomplete")
        require(not record["exceptions"] or payload.get("allow_exceptions") is True,
                "This close has exceptions; explicit exception sign-off is required.", "close_exceptions")
        require(all(i.get("owner") and i.get("due_at") and i.get("resolution_reference") and i.get("disposition") == "accepted_exception" for i in record["exceptions"]),
                "Every exception needs an owner, due time, evidence/correction reference and explicit acceptance.", "close_exceptions")
        # Concurrent drafts cannot both become the next baseline.
        newer = [r for r in inv.rows(conn, m.daily_closes) if r["approval_status"] == "signed"
                 and r["id"] not in (record["packet"]["previous_close_id"], record.get("supersedes"))
                 and r["cutoff"] >= (record["packet"]["previous_cutoff"] or "")]
        require(not newer, "Another close has been signed for this boundary. Prepare against the current baseline.", "close_stale")
        record.update(approval_status="signed", signed_by=actor["id"], signed_at=m.now(), sign_reason=why,
                      followup_validation=dict(sequence=evidence["source"]["sequence"], observed_at=evidence["source"]["observed_at"],
                                               material_digest=evidence["material_digest"]))
        if record.get("supersedes"):
            old = packet(conn, record["supersedes"])
            old["approval_status"] = "superseded"
            old["superseded_by"] = record["id"]
            old["version"] += 1
            inv.save(conn, m.daily_closes, old)
    else:
        raise Conflict("Unknown close action.", "not_found", 404)
    record["version"] += 1
    record["digest"] = m.digest(dict(packet=record["packet"], exceptions=record["exceptions"], cutoff=record["cutoff"], business_date=record["business_date"], timezone=record["timezone"]))
    inv.save(conn, m.daily_closes, record)
    return record
