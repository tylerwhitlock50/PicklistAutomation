"""Read-only VISUAL -> Dispatch connector with a durable SQLite outbox.

The complete source identity map catches late commits, edits and deletions even
when transaction IDs arrive out of order. No maximum-ID polling cursor is used.
Run bootstrap once during the coordinated opening count, then poll normally.
"""
import argparse
from contextlib import contextmanager
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from fulfillment.model import digest, now
from fulfillment.domain import Conflict, require


class DeliveryError(RuntimeError):
    def __init__(self, code, message, *, rejected=False):
        self.code, self.rejected = code, rejected
        super().__init__(message)


class Receiver:
    def __init__(self, origin, token, *, allow_http_demo=False, session=None):
        url = urlsplit(origin)
        require(url.scheme == "https" or (allow_http_demo and url.scheme == "http" and url.hostname in ("127.0.0.1", "localhost", "::1")), "Connector requires HTTPS (loopback HTTP only with --allow-http-demo).")
        require(not url.username and not url.password and not url.query and not url.fragment and url.path in ("", "/"), "Use an API origin without credentials, path, query or fragment.")
        require(len(token) >= 32, "Set FULFILLMENT_INVENTORY_TOKEN to a dedicated token of at least 32 characters.")
        self.origin, self.token, self.http = origin.rstrip("/"), token, session or requests.Session()

    def request(self, method, path, payload=None):
        headers = {"Authorization": "Bearer " + self.token}
        if payload:
            headers["Idempotency-Key"] = payload["event_id"]
        try:
            response = self.http.request(method, self.origin + "/api/v1/integrations/inventory/" + path,
                                         headers=headers, json=payload, timeout=(5, 60), allow_redirects=False)
        except requests.RequestException:
            raise DeliveryError("transport", "Receiver unavailable; the durable pending batch will be retried unchanged.") from None
        try:
            data = response.json()
        except ValueError:
            raise DeliveryError("response", "Receiver returned an invalid response; pending batch retained.") from None
        if not isinstance(data, dict):
            raise DeliveryError("response", "Receiver returned an invalid response; pending batch retained.")
        if response.status_code != 200:
            raise DeliveryError(data.get("error", "http_" + str(response.status_code)), data.get("message", "Receiver rejected the batch."),
                                rejected=400 <= response.status_code < 500)
        return data

    def state(self):
        return self.request("GET", "state")

    def deliver(self, payload):
        return self.request("POST", "batches", payload)

    def confirm_delivery(self, state):
        return self.report_failure(state, "delivery_confirmed")

    def report_failure(self, state, code):
        if state.get("stream_id"):
            return self.request("POST", "health", dict(event_id="HEALTH:" + str(uuid4()).upper(), source="visual",
                stream_id=state["stream_id"], sequence=state["sequence"], code=code))


class Outbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    @contextmanager
    def transaction(self):
        # One process/host may run the connector at a time. SQLite's lock also
        # serializes overlapping scheduled invocations and crash recovery.
        with sqlite3.connect(self.path, timeout=5) as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn

    @staticmethod
    def get(conn, key, default=None):
        row = conn.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def put(conn, key, value):
        conn.execute("INSERT INTO state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, json.dumps(value, sort_keys=True)))

    def inspect(self):
        with self.transaction() as conn:
            checkpoint = self.get(conn, "checkpoint", {})
            pending = self.get(conn, "pending")
            return dict(sequence=checkpoint.get("sequence", 0), stream_id=checkpoint.get("stream_id"),
                        pending=bool(pending), last_success=checkpoint.get("last_success"), last_error=self.get(conn, "last_error"))


def build_batch(capture, receiver, checkpoint):
    scope = capture["scope"]
    require(all(scope[k] == receiver["config"][k] for k in ("site", "warehouse", "location")), "Connector source scope must exactly match Dispatch's configured warehouse.")
    opening = not checkpoint
    sequence = checkpoint.get("sequence", 0) + 1
    stream = checkpoint.get("stream_id") or str(uuid4()).upper()
    old_movements = checkpoint.get("movements", {})
    # Keep every previously seen identity, including deleted source rows. A
    # restored historical receipt was already included in opening stock or a
    # prior batch and must never be treated as a new arrival.
    movements, present_movements, receipts, issues = dict(old_movements), set(), [], []
    evidence_changes = []
    upgrade = checkpoint.get("evidence_version") != 2
    for movement in capture["movements"]:
        identity = str(movement["id"])
        require(identity not in present_movements, "Source contains duplicate transaction identities.")
        present_movements.add(identity)
        current_hash = digest(movement)
        movements[identity] = current_hash
        old_hash = old_movements.get(identity)
        core = {k: movement[k] for k in ("id", "part", "uom", "quantity", "serials", "kind", "work_order")}
        enriched = old_hash is not None and old_hash == digest(core)
        if opening or upgrade or old_hash != current_hash or identity not in checkpoint.get("present_movements", old_movements):
            evidence_changes.append(dict(id=identity, movement=movement, deleted=False,
                                         enrichment=bool(enriched), prior_hash=old_hash))
        if opening:
            continue
        if identity in old_movements:
            if old_movements[identity] != current_hash and not enriched:
                issues.append(dict(id="EDIT:" + identity, kind="source_edit", part=movement["part"],
                                   message="Previously observed VISUAL transaction " + identity + " changed. Review against the ledger; no receipt was replayed."))
            continue
        if movement["kind"] == "receipt":
            receipts.append(dict(event_id="IT:" + identity, part=movement["part"], uom=movement["uom"],
                                 quantity=movement["quantity"], serials=[s["serial"] for s in movement["serials"]],
                                 work_order=movement.get("work_order") or "", reason="VISUAL transaction " + identity))
        elif movement["kind"] != "shipment":
            issues.append(dict(id="MOVE:" + identity, kind="source_movement", part=movement["part"],
                               message="VISUAL transaction " + identity + " requires review (transfer out, correction, return or reversal). No automatic stock adjustment."))
    for identity in set(checkpoint.get("present_movements", old_movements)) - present_movements:
        evidence_changes.append(dict(id=identity, deleted=True, prior_hash=old_movements.get(identity)))
        issues.append(dict(id="DELETE:" + identity, kind="source_deletion", message="Previously observed VISUAL transaction " + identity + " is missing. Review source retention/deletion."))
    current_supplies = {}
    for supply in capture["work_orders"]:
        require(supply["work_order"] not in current_supplies, "Duplicate work-order source identity.")
        current_supplies[supply["work_order"]] = supply
    old_supplies = checkpoint.get("work_orders", {})
    linked = {r["work_order"] for r in receipts if r["work_order"]}
    work_orders = [s for identity, s in current_supplies.items()
                   if opening or digest(s) != digest(old_supplies.get(identity)) or identity in linked]
    for identity in set(old_supplies) - set(current_supplies):
        work_orders.append({**old_supplies[identity], "remaining": "0", "status": "cancelled"})
        current_supplies[identity] = work_orders[-1]
        issues.append(dict(id="WO:" + digest(identity), kind="source_supply_missing", message="Work order " + identity + " disappeared from the complete source. Its promises need review."))
    require(linked.issubset(current_supplies), "A receipt references a work order absent from the source snapshot. Correct source scope before continuing.")
    payload = {**scope, "source": "visual", "kind": "opening" if opening else "delta", "stream_id": stream,
               "event_id": "BATCH:" + str(uuid4()).upper(), "sequence": sequence,
               "expected_stock_version": receiver["stock_version"], "observed_at": capture["observed_at"],
               "source_manifest": digest([movements, sorted(present_movements), current_supplies]), "receipts": receipts, "work_orders": work_orders,
               "balances": capture["balances"], "issues": issues,
               "movement_changes": evidence_changes,
               "movement_evidence": dict(version=2, complete=capture.get("complete") is True and capture.get("evidence_version") == 2,
                   count=len(present_movements), manifest=digest(sorted((i, movements[i]) for i in present_movements)),
                   base_sequence=checkpoint.get("sequence", 0), upgrade=bool(upgrade and not opening),
                   pending_before_capture=False)}
    require(len(json.dumps(payload).encode("utf-8")) <= 15 * 1024 * 1024,
            "Complete inventory evidence exceeds the 15 MiB transport limit. Do not truncate; use a reviewed larger or chunked connector before close.", "evidence_size", 413)
    next_checkpoint = dict(sequence=sequence, stream_id=stream, movements=movements, present_movements=sorted(present_movements),
                           work_orders=current_supplies, scope=scope, evidence_version=2)
    return {"payload": payload, "checkpoint": next_checkpoint}


class Connector:
    def __init__(self, source, receiver, outbox):
        self.source, self.receiver, self.outbox = source, receiver, outbox

    def report_failure(self, state, code):
        try:
            self.receiver.report_failure(state, code)
        except Exception:
            # Do not obscure the original failure. Receiver freshness expires
            # independently, including when this best-effort signal cannot land.
            pass

    def recapture_rejected(self):
        """Explicitly abandon only a known rejected, provably uncommitted batch."""
        with self.outbox.transaction() as conn:
            pending = self.outbox.get(conn, "pending")
            error = self.outbox.get(conn, "last_error", {}) or {}
            require(pending and error.get("rejected"),
                    "Only a recorded HTTP 4xx rejection can be recaptured. Retry uncertain deliveries unchanged.")
            remote = self.receiver.state()
            checkpoint = self.outbox.get(conn, "checkpoint", {})
            require(remote["sequence"] == checkpoint.get("sequence", 0) and
                    (remote.get("stream_id") == checkpoint.get("stream_id")),
                    "Receiver advanced. Retry the pending batch or restore the original outbox; it cannot be discarded.")
            # Retain a bounded audit record, but keep the accepted identity map.
            self.outbox.put(conn, "last_discarded", {"event_id": pending["payload"]["event_id"], "at": now(), "error": error})
            self.outbox.put(conn, "pending", None)
            self.outbox.put(conn, "last_error", None)
            return {"recapture_ready": True, "sequence": checkpoint.get("sequence", 0)}

    def once(self, *, bootstrap=False):
        # Persist the envelope before network delivery. A crash after remote
        # commit but before local acknowledgement always retries the same body.
        with self.outbox.transaction() as conn:
            pending = self.outbox.get(conn, "pending")
            checkpoint = self.outbox.get(conn, "checkpoint", {})
            if not pending:
                remote = self.receiver.state()
                require(bool(checkpoint) or bootstrap, "Run --bootstrap once during a coordinated opening count.")
                require(remote["sequence"] == checkpoint.get("sequence", 0) and
                        (not checkpoint or remote.get("stream_id") == checkpoint["stream_id"]),
                        "Receiver and connector checkpoint differ. Restore the original outbox; do not create another opening import.")
                try:
                    pending = build_batch(self.source.capture(), remote, checkpoint)
                except Exception as exc:
                    self.report_failure(remote, "capture_failed")
                    # Persist a safe diagnostic, not driver text that may reveal
                    # connection parameters. No source checkpoint was advanced.
                    self.outbox.put(conn, "last_error", {"at": now(), "code": "capture_failed", "message": exc.message if isinstance(exc, Conflict) else type(exc).__name__})
                    return {"accepted": False, "error": "capture_failed", "message": "Source capture failed; inspect source validity and connector logs."}
                self.outbox.put(conn, "pending", pending)
        # Re-lock for delivery so another scheduled invocation cannot overtake
        # this batch. It may already have delivered it; reread under the lock.
        with self.outbox.transaction() as conn:
            pending = self.outbox.get(conn, "pending")
            if not pending:
                return {"accepted": True, "already_delivered": True}
            try:
                result = self.receiver.deliver(pending["payload"])
                require(result.get("accepted") and result.get("sequence") == pending["payload"]["sequence"] and
                        result.get("stream_id") == pending["payload"]["stream_id"], "Unexpected batch acknowledgement; preserve the pending envelope.")
            except DeliveryError as exc:
                self.outbox.put(conn, "last_error", {"at": now(), "code": exc.code, "message": str(exc), "rejected": exc.rejected})
                self.report_failure(self.outbox.get(conn, "checkpoint", {}), "delivery_rejected" if exc.rejected else "delivery_pending")
                if exc.code == "capture_stale":
                    # This explicit rejection occurs before any business writes.
                    self.outbox.put(conn, "pending", None)
                result = {"accepted": False, "error": exc.code, "message": str(exc)}
            else:
                self.outbox.put(conn, "checkpoint", {**pending["checkpoint"], "last_success": now()})
                self.outbox.put(conn, "pending", None)
                self.outbox.put(conn, "last_error", None)
        if result.get("accepted"):
            try:
                # This acknowledgement follows the durable local checkpoint commit.
                confirm = getattr(self.receiver, "confirm_delivery", None)
                if confirm:
                    confirm(pending["checkpoint"])
            except Exception:
                # Safe to retry the acknowledgement on the next successful poll;
                # daily close stays incomplete until it is confirmed.
                pass
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--origin", default=os.getenv("FULFILLMENT_INVENTORY_API_ORIGIN"))
    parser.add_argument("--state", default=os.getenv("FULFILLMENT_INVENTORY_OUTBOX", "data/inventory-connector.db"))
    parser.add_argument("--bootstrap", action="store_true", help="Import complete opening stock; requires disabled empty inventory")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--recapture-rejected", action="store_true", help="After correcting source data, discard only a verified uncommitted HTTP 4xx rejection")
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--max-source-rows", type=int, default=100000, help="Reviewed maximum rows per complete source query; exceeding it rejects the capture, never truncates it")
    parser.add_argument("--allow-http-demo", action="store_true")
    args = parser.parse_args()
    outbox = Outbox(args.state)
    if args.status:
        print(json.dumps(outbox.inspect(), indent=2))
        return
    require(15 <= args.interval <= 120, "Polling interval must be 15–120 seconds (receiver freshness expires after five minutes).")
    from fulfillment.visual_inventory_source import VisualInventorySource
    scope = {k: os.getenv("FULFILLMENT_" + ("SITE" if k == "site" else "INVENTORY_" + k.upper()), "").upper()
             for k in ("site", "warehouse", "location")}
    require(all(scope.values()), "Set site, inventory warehouse and inventory location before source capture.")
    source = VisualInventorySource(os.getenv("FULFILLMENT_MSSQL_CONNECTION_STRING", ""), scope, max_rows=args.max_source_rows)
    connector = Connector(source, Receiver(args.origin or "", os.getenv("FULFILLMENT_INVENTORY_TOKEN", ""), allow_http_demo=args.allow_http_demo), outbox)
    if args.recapture_rejected:
        print(json.dumps(connector.recapture_rejected()))
        return
    while True:
        try:
            result = connector.once(bootstrap=args.bootstrap)
            print(json.dumps(result), flush=True)
            if args.once or args.bootstrap:
                if not result.get("accepted"):
                    raise SystemExit(1)
                return
        except Exception as exc:
            # SQLAlchemy/driver exceptions may contain connection parameters.
            logging.error("Inventory synchronization failed (%s); source/receiver state retained.", type(exc).__name__)
            if args.once or args.bootstrap:
                raise SystemExit(1) from None
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
