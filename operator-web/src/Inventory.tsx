import React, { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { Session } from "./types";
import type {
  InventoryState,
  FutureSupply,
  InventoryAllocation,
  InventoryDemand,
} from "./inventory-types";
import { DemandFields, CommitmentFields } from "./InventoryDemand";
import { InventorySync } from "./InventorySync";
import { inventorySyncHealth } from "./inventory-health";

type Pending = { action: string; body: Record<string, unknown>; key: string };
type Section = "stock" | "supply" | "allocations" | "sync" | "history";
const sections: [Section, string][] = [
  ["stock", "Stock & receipts"],
  ["supply", "Future supply"],
  ["allocations", "Allocations & holds"],
  ["sync", "Sync & warehouse"],
  ["history", "Activity"],
];
const text = (data: FormData, key: string) =>
  String(data.get(key) || "").trim();
const dateTime = (value: string | null | undefined) =>
  value ? new Date(value).toLocaleString() : "—";
const quantity = (value: string | number) =>
  Number(value).toLocaleString(undefined, { maximumFractionDigits: 8 });
const active = (allocation: InventoryAllocation) =>
  allocation.status === "active";
function Field({
  label,
  name,
  value,
  type = "text",
  required = true,
  placeholder,
  min,
  max,
  readOnly = false,
}: {
  label: string;
  name: string;
  value?: string | number;
  type?: string;
  required?: boolean;
  placeholder?: string;
  min?: string;
  max?: string | number;
  readOnly?: boolean;
}) {
  return (
    <label>
      {label}
      <input
        name={name}
        type={type}
        defaultValue={value}
        required={required}
        placeholder={placeholder}
        min={min}
        max={max}
        readOnly={readOnly}
        step={type === "number" ? "any" : undefined}
        maxLength={type === "text" ? 200 : undefined}
      />
    </label>
  );
}
function Reason() {
  return (
    <label className="inventory-span">
      Reason / request reference
      <input
        name="reason"
        required
        maxLength={1000}
        placeholder="Inside sales request, receipt reference, or explanation"
      />
    </label>
  );
}
function Empty({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="empty inventory-empty">
      <h3>{title}</h3>
      <p>{children}</p>
    </div>
  );
}
function serials(data: FormData) {
  return text(data, "serials")
    .split(/[\s,]+/)
    .filter(Boolean);
}
function localDateTime(value?: string | null) {
  if (!value) return "";
  const date = new Date(value);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
function optionalDate(value: string) {
  return value ? new Date(value).toISOString() : null;
}

export function Inventory({
  session,
  online,
  onAuthLost,
  onLock,
}: {
  session: Session;
  online: boolean;
  onAuthLost: () => void;
  onLock: (locked: boolean) => void;
}) {
  const [clock, setClock] = useState(Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 15000);
    return () => window.clearInterval(timer);
  }, []);
  const [state, setState] = useState<InventoryState | null>(null);
  const [section, setSection] = useState<Section>("stock");
  const [demand, setDemand] = useState<InventoryDemand | null>(null);
  const [demandError, setDemandError] = useState("");
  const [demandLoading, setDemandLoading] = useState(false);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false),
    [pending, setPending] = useState<Pending | null>(null);
  const [stale, setStale] = useState(false),
    [revision, setRevision] = useState(0);
  const [query, setQuery] = useState(""),
    [receiptSupply, setReceiptSupply] = useState("");
  const [editingSupply, setEditingSupply] = useState(""),
    [editingAllocation, setEditingAllocation] = useState("");
  const [allocationSource, setAllocationSource] = useState<
    "stock" | "work_order"
  >("stock");
  const [allocationSupply, setAllocationSupply] = useState("");
  const [allocationFilter, setAllocationFilter] = useState("active");
  const [commitmentPart, setCommitmentPart] = useState<{
    part: string;
    uom: string;
  } | null>(null);
  const [stockSelection, setStockSelection] = useState<{
    part: string;
    uom: string;
  } | null>(null);
  const syncHealth = inventorySyncHealth(state?.sync, clock);
  const canManage = ["supervisor", "admin"].includes(session.operator.role);
  const locked = busy || !!pending || !online || !canManage || stale;
  const viewLocked = busy || !!pending;
  const source = state?.supplies.find((item) => item.id === receiptSupply);
  const selectedSupply = state?.supplies.find(
    (item) => item.id === editingSupply,
  );
  const allocatedSupply = state?.supplies.find(
    (item) => item.id === allocationSupply,
  );
  const selectedAllocation = state?.allocations.find(
    (item) => item.id === editingAllocation,
  );
  const transferable = selectedAllocation
    ? Number(
        selectedAllocation.transferable_quantity ??
          Number(selectedAllocation.quantity) -
            Number(selectedAllocation.fulfilled),
      )
    : 0;
  const matches = (...values: (string | null | undefined)[]) =>
    values.join(" ").toLowerCase().includes(query.toLowerCase().trim());

  useEffect(() => {
    onLock(viewLocked || stale);
  }, [viewLocked, stale, onLock]);
  useEffect(() => {
    if (!viewLocked && !stale) return;
    const protectConfirmation = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", protectConfirmation);
    return () =>
      window.removeEventListener("beforeunload", protectConfirmation);
  }, [viewLocked, stale]);
  useEffect(() => () => onLock(false), [onLock]);
  async function loadDemand() {
    setDemandLoading(true);
    setDemandError("");
    try {
      setDemand(await api<InventoryDemand>("/inventory/demand"));
    } catch (e) {
      setDemand(null);
      setDemandError(
        "Open-order lookup is unavailable. Customer pools and manual order entry remain available; order bindings are checked when saved.",
      );
      if (e instanceof ApiError && e.status === 401) onAuthLost();
    } finally {
      setDemandLoading(false);
    }
  }
  useEffect(() => {
    if (section === "allocations" && !demand && !demandError) void loadDemand();
  }, [section]);
  async function refresh() {
    const result = await api<InventoryState>("/inventory");
    setState(result);
    setReceiptSupply((id) =>
      result.supplies.some(
        (item) =>
          item.id === id &&
          item.status === "open" &&
          Number(item.quantity) > Number(item.received),
      )
        ? id
        : "",
    );
    setStale(false);
    setRevision((value) => value + 1);
  }
  async function reload() {
    setBusy(true);
    setError("");
    try {
      await refresh();
    } catch (e) {
      setError((e as Error).message);
      if (e instanceof ApiError && e.status === 401) onAuthLost();
    } finally {
      setBusy(false);
    }
  }
  useEffect(() => {
    void reload();
  }, []);
  async function run(
    action: string,
    body: Record<string, unknown>,
    key: string = crypto.randomUUID(),
  ) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await api("/inventory/" + action, body, session.csrf, key);
      setPending(null);
      setNotice("Confirmed and saved in Dispatch.");
      // A committed mutation remains successful even if the following read fails.
      setStale(true);
      try {
        await refresh();
      } catch (e) {
        if (e instanceof ApiError && e.status === 401) onAuthLost();
        else
          setNotice(
            "Saved. Reload inventory before making another change; displayed balances may be out of date.",
          );
      }
    } catch (e) {
      setError((e as Error).message);
      if (!(e instanceof ApiError) || e.status >= 500)
        setPending({ action, body, key });
      else {
        setPending(null);
        if (e.status === 401) onAuthLost();
        if (e.status === 409) {
          setStale(true);
          try {
            await refresh();
          } catch {
            /* retain visible state, require reload */
          }
        }
      }
    } finally {
      setBusy(false);
    }
  }
  function submit(
    event: React.FormEvent<HTMLFormElement>,
    action: string,
    body: (data: FormData) => Record<string, unknown>,
  ) {
    event.preventDefault();
    if (locked) return;
    try {
      void run(action, body(new FormData(event.currentTarget)));
    } catch (e) {
      setError((e as Error).message);
    }
  }
  function startAllocation(part: string, uom: string, supply?: FutureSupply) {
    setStockSelection({ part, uom });
    setAllocationSource(supply ? "work_order" : "stock");
    setAllocationSupply(supply?.id || "");
    setEditingAllocation("");
    setCommitmentPart(null);
    setQuery("");
    setSection("allocations");
  }

  const grouped = new Map<
    string,
    {
      part: string;
      uom: string;
      quantity: number;
      committed: number;
      available: number;
      serials: string[];
      syncBlocked: boolean;
    }
  >();
  for (const item of state?.balances || []) {
    const key = JSON.stringify([item.part, item.uom]);
    const group = grouped.get(key) || {
      part: item.part,
      uom: item.uom,
      quantity: 0,
      committed: 0,
      available: 0,
      serials: [],
      syncBlocked: false,
    };
    group.syncBlocked ||= !!item.sync_blocked || syncHealth.blocked;
    group.quantity += Number(item.quantity);
    group.committed += Number(item.committed);
    group.available += syncHealth.blocked ? 0 : Number(item.available);
    if (item.serial && Number(item.quantity) > 0)
      group.serials.push(item.serial);
    grouped.set(key, group);
  }
  const allocations = state?.allocations.filter(active) || [];
  const held = allocations.filter((item) => item.hold);
  const openSupplies =
    state?.supplies.filter((item) => item.status === "open") || [];
  const latestReconciliation = state?.reconciliations[0];
  const stockOptions = [...grouped.values()];
  const allocatedStock = stockSelection
    ? grouped.get(JSON.stringify([stockSelection.part, stockSelection.uom]))
    : undefined;
  const allocationAvailable = syncHealth.blocked
    ? 0
    : allocationSource === "work_order"
      ? Number(allocatedSupply?.available || 0)
      : allocatedStock?.available || 0;
  const visibleAllocations = (state?.allocations || []).filter(
    (item) =>
      matches(item.customer, item.order, item.part, item.receipt_event_id) &&
      (!commitmentPart ||
        (item.source === "stock" &&
          item.part === commitmentPart.part &&
          item.uom === commitmentPart.uom)) &&
      (allocationFilter === "all" ||
        (allocationFilter === "active" && active(item)) ||
        (allocationFilter === "held" && active(item) && item.hold) ||
        (allocationFilter === "review" && active(item) && item.review_due) ||
        (allocationFilter === "closed" && !active(item)) ||
        (allocationFilter === "risk" && active(item) && item.at_risk)),
  );

  return (
    <section className="inventory-workspace" aria-label="Shipping inventory">
      <div className="section-title">
        <div>
          <p className="eyebrow">SHIPPING INVENTORY</p>
          <h2>Stock with a destination.</h2>
        </div>
        <button
          className="secondary"
          disabled={viewLocked || !online}
          onClick={() => void reload()}
        >
          Reload inventory
        </button>
      </div>
      <p className="inventory-intro">
        Manage what is here, what is coming, and who it belongs to.
      </p>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="success" role="status">
          {notice}
        </div>
      )}
      {pending && (
        <div className="alert" role="alert">
          Confirmation is uncertain. Retry the same change to recover its saved
          result.
          <button
            disabled={busy || !online}
            onClick={() => void run(pending.action, pending.body, pending.key)}
          >
            Retry inventory confirmation
          </button>
        </div>
      )}
      {!state ? (
        <Empty
          title={busy ? "Loading inventory…" : "Inventory is unavailable."}
        >
          Use Reload inventory to try again. The shipping ledger is independent
          of the order feed.
        </Empty>
      ) : (
        <>
          <div className="inventory-status">
            <span
              className={"pill " + (!state.config.enabled ? "pill-warm" : "")}
            >
              {state.config.enabled ? "APP INVENTORY ACTIVE" : "PREPARATION"}
            </span>
            <strong>
              {[
                state.config.site,
                state.config.warehouse,
                state.config.location,
              ]
                .filter(Boolean)
                .join(" / ") || "Shipping location not configured"}
            </strong>
            <span>Updated {dateTime(state.as_of)}</span>
          </div>
          {syncHealth.blocked && (
            <div className="alert" role="alert">
              <strong>{syncHealth.label}</strong>
              <p>{syncHealth.message}</p>
              <button
                className="secondary"
                disabled={viewLocked}
                onClick={() => setSection("sync")}
              >
                Review connection
              </button>
            </div>
          )}
          {!state.config.enabled && (
            <div className="banner">
              Prepare the opening inventory, reconcile it with VISUAL, then
              activate the shipping ledger. Receipts and planning can be
              prepared here while the current shipping workflow remains in use.
            </div>
          )}
          {session.mode === "shadow" && (
            <div className="banner">
              Inventory preparation is available to supervisors and
              administrators. Picking remains disabled in shadow.
            </div>
          )}
          {!canManage && (
            <div className="banner">
              View access. A supervisor or administrator can receive stock and
              manage allocations.
            </div>
          )}
          {stale && (
            <div className="alert">
              Reload inventory to confirm the latest balances before making
              changes.
            </div>
          )}
          <div className="inventory-metrics" aria-label="Inventory summary">
            <div>
              <small>STOCKED PARTS</small>
              <strong>
                {
                  new Set(
                    [...grouped.values()]
                      .filter((item) => item.quantity > 0)
                      .map((item) => item.part),
                  ).size
                }
              </strong>
              <span>At the shipping location</span>
            </div>
            <div>
              <small>FUTURE WORK ORDERS</small>
              <strong>{openSupplies.length}</strong>
              <span>Planned supply, awaiting receipt</span>
            </div>
            <div>
              <small>ACTIVE ALLOCATIONS</small>
              <strong>{allocations.length}</strong>
              <span>Customer or order commitments</span>
            </div>
            <div>
              <small>HELD FOR CUSTOMERS</small>
              <strong>{held.length}</strong>
              <span>Protected until explicitly released</span>
            </div>
          </div>
          <div className="inventory-tabs" aria-label="Inventory sections">
            {sections.map(([id, label]) => (
              <button
                key={id}
                className={section === id ? "" : "secondary"}
                aria-pressed={section === id}
                disabled={viewLocked}
                onClick={() => {
                  setSection(id);
                  setQuery("");
                }}
              >
                {label}
              </button>
            ))}
          </div>

          {section === "stock" && (
            <>
              <div className="inventory-columns">
                <section className="panel">
                  <div className="section-title">
                    <h3>On hand at shipping</h3>
                    <span>{grouped.size} part / unit combinations</span>
                  </div>
                  <label className="inventory-search">
                    Find a part or serial
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Part number or serial"
                    />
                  </label>
                  {grouped.size ? (
                    <div className="inventory-table">
                      <table>
                        <thead>
                          <tr>
                            <th>Part / unit</th>
                            <th>On hand</th>
                            <th>Committed</th>
                            <th>Available</th>
                            <th>
                              <span className="sr-only">Actions</span>
                            </th>
                          </tr>
                        </thead>
                        <tbody>
                          {[...grouped.values()]
                            .filter((item) =>
                              matches(item.part, ...item.serials),
                            )
                            .map((item) => (
                              <tr key={JSON.stringify([item.part, item.uom])}>
                                <td>
                                  <strong>{item.part}</strong>
                                  <span className="inventory-cell-note">
                                    {item.uom}
                                  </span>
                                  {item.syncBlocked && (
                                    <span className="pill pill-warm">
                                      Sync review
                                    </span>
                                  )}
                                  {!!item.serials.length && (
                                    <details className="inventory-serials">
                                      <summary>
                                        {item.serials.length} serials
                                      </summary>
                                      <p>{item.serials.join(", ")}</p>
                                    </details>
                                  )}
                                </td>
                                <td>{quantity(item.quantity)}</td>
                                <td>{quantity(item.committed)}</td>
                                <td className="inventory-available">
                                  {quantity(item.available)}
                                </td>
                                <td>
                                  <button
                                    className="text-button"
                                    disabled={locked || item.available <= 0}
                                    onClick={() =>
                                      startAllocation(item.part, item.uom)
                                    }
                                  >
                                    Allocate →
                                  </button>
                                  {item.committed > 0 && (
                                    <button
                                      className="text-button"
                                      disabled={viewLocked}
                                      onClick={() => {
                                        setCommitmentPart({
                                          part: item.part,
                                          uom: item.uom,
                                        });
                                        setAllocationFilter("active");
                                        setQuery("");
                                        setSection("allocations");
                                      }}
                                    >
                                      View commitments
                                    </button>
                                  )}
                                </td>
                              </tr>
                            ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty title="No stock received yet.">
                      Record a receipt when inventory physically arrives in the
                      shipping warehouse.
                    </Empty>
                  )}
                  <p className="hint">
                    Available stock excludes existing commitments and stock
                    under sync review. Future work orders become on-hand
                    inventory only when received.
                  </p>
                </section>
                <section className="panel inventory-form-panel">
                  <p className="eyebrow">RECEIVING</p>
                  <h3>Add inventory to shipping</h3>
                  <p>
                    Confirm the physical arrival with a unique receipt
                    reference.
                  </p>
                  {state.sync?.connected && (
                    <p className="inventory-callout">
                      VISUAL sync records warehouse arrivals automatically.
                      Review receipt exceptions in Sync & warehouse.
                    </p>
                  )}
                  <form
                    key={"receipt-" + revision + "-" + receiptSupply}
                    onSubmit={(event) =>
                      submit(event, "receive", (data) => ({
                        event_id: text(data, "event_id"),
                        source: "manual",
                        site: state.config.site,
                        warehouse: state.config.warehouse,
                        location: state.config.location,
                        part: source?.part || text(data, "part"),
                        uom: source?.uom || text(data, "uom"),
                        quantity: text(data, "quantity"),
                        serials: serials(data),
                        ...(source ? { work_order_id: source.id } : {}),
                        reason: text(data, "reason"),
                      }))
                    }
                  >
                    <fieldset
                      disabled={
                        locked ||
                        !!state.sync?.connected ||
                        !state.config.site ||
                        !state.config.warehouse ||
                        !state.config.location
                      }
                      className="inventory-fields"
                    >
                      <label className="inventory-span">
                        Receive against work order
                        <select
                          value={receiptSupply}
                          onChange={(event) =>
                            setReceiptSupply(event.target.value)
                          }
                        >
                          <option value="">
                            Manual receipt / opening inventory
                          </option>
                          {openSupplies.map((item) => (
                            <option
                              key={item.id}
                              value={item.id}
                              disabled={
                                Number(item.received) >= Number(item.quantity)
                              }
                            >
                              {item.work_order} · {item.part}
                            </option>
                          ))}
                        </select>
                      </label>
                      {source ? (
                        <p className="inventory-span inventory-callout">
                          {source.part} ·{" "}
                          {quantity(
                            Number(source.quantity) - Number(source.received),
                          )}{" "}
                          {source.uom} awaiting receipt.
                          {source.serialized
                            ? " Enter one serial per unit."
                            : ""}
                        </p>
                      ) : (
                        <>
                          <Field name="part" label="Part number" />
                          <Field
                            name="uom"
                            label="Unit of measure"
                            value="EA"
                          />
                        </>
                      )}
                      <Field
                        name="quantity"
                        label="Quantity received"
                        max={
                          source
                            ? (
                                Number(source.quantity) -
                                Number(source.received)
                              ).toFixed(8)
                            : undefined
                        }
                        type="number"
                        min="0.00000001"
                      />
                      <Field
                        name="event_id"
                        label="Unique receipt reference"
                        placeholder="Receipt / transfer / source event ID"
                      />
                      <label className="inventory-span">
                        Serials{" "}
                        {source?.serialized ? "(required)" : "(if serialized)"}
                        <textarea
                          name="serials"
                          required={source?.serialized}
                          rows={3}
                          placeholder="One serial per line"
                        />
                      </label>
                      <Reason />
                      <button className="inventory-span" type="submit">
                        {busy ? "Saving…" : "Confirm receipt"}
                      </button>
                    </fieldset>
                  </form>
                  {(!state.config.site ||
                    !state.config.warehouse ||
                    !state.config.location) && (
                    <p className="hint">
                      Set the shipping location under Sync & warehouse before
                      receiving.
                    </p>
                  )}
                </section>
              </div>
              {canManage && (
                <details className="panel">
                  <summary>Inventory correction</summary>
                  <p>
                    Use a signed adjustment after a physical count. Existing
                    commitments must remain covered. With a connected source,
                    a new part or serial also needs a fresh VISUAL comparison
                    showing that stock.
                  </p>
                  <form
                    key={"adjust-" + revision}
                    onSubmit={(event) =>
                      submit(event, "adjust", (data) => ({
                        part: text(data, "part"),
                        uom: text(data, "uom"),
                        quantity: text(data, "quantity"),
                        serials: serials(data),
                        reason: text(data, "reason"),
                      }))
                    }
                  >
                    <fieldset
                      disabled={locked}
                      className="inventory-fields inventory-wide-fields"
                    >
                      <Field name="part" label="Part number" />
                      <Field name="uom" label="Unit of measure" value="EA" />
                      <Field
                        name="quantity"
                        label="Adjustment (+ / −)"
                        type="number"
                      />
                      <label>
                        Serials (if serialized)
                        <textarea
                          name="serials"
                          rows={2}
                          placeholder="One serial per line"
                        />
                      </label>
                      <Reason />
                      <button type="submit">Save adjustment</button>
                    </fieldset>
                  </form>
                </details>
              )}
            </>
          )}

          {section === "supply" && (
            <>
              <div className="inventory-columns">
                <section className="panel">
                  <div className="section-title">
                    <h3>Work-order supply</h3>
                    <span>{openSupplies.length} open</span>
                  </div>
                  <p>
                    Plan customer commitments before production reaches the
                    warehouse.
                  </p>
                  {state.supplies.length ? (
                    <div className="inventory-table">
                      <table>
                        <thead>
                          <tr>
                            <th>Work order / part</th>
                            <th>Expected</th>
                            <th>Received</th>
                            <th>Reserved</th>
                            <th>Due / status</th>
                            <th>Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {state.supplies.map((item) => (
                            <tr key={item.id}>
                              <td>
                                <strong>{item.work_order}</strong>
                                <span className="inventory-cell-note">
                                  {item.part} · {item.uom}
                                </span>
                              </td>
                              <td>{quantity(item.quantity)}</td>
                              <td>{quantity(item.received)}</td>
                              <td>{quantity(item.reserved)}</td>
                              <td>
                                {item.due_date || "No date"}
                                <span className="inventory-cell-note">
                                  {item.status}
                                </span>
                                {Number(item.shortfall) > 0 && (
                                  <span className="pill pill-warm">
                                    {quantity(item.shortfall)} {item.uom}{" "}
                                    shortfall
                                  </span>
                                )}
                              </td>
                              <td>
                                <div className="inventory-actions">
                                  <button
                                    className="text-button"
                                    disabled={locked || !!state.sync?.connected}
                                    onClick={() => setEditingSupply(item.id)}
                                  >
                                    Edit
                                  </button>
                                  {item.status === "open" && (
                                    <>
                                      <button
                                        className="text-button"
                                        disabled={
                                          locked ||
                                          syncHealth.blocked ||
                                          Number(item.available) <= 0
                                        }
                                        onClick={() =>
                                          startAllocation(
                                            item.part,
                                            item.uom,
                                            item,
                                          )
                                        }
                                      >
                                        Allocate
                                      </button>
                                      <button
                                        className="text-button"
                                        disabled={
                                          locked ||
                                          !!state.sync?.connected ||
                                          Number(item.received) >=
                                            Number(item.quantity)
                                        }
                                        onClick={() => {
                                          setReceiptSupply(item.id);
                                          setSection("stock");
                                        }}
                                      >
                                        Receive
                                      </button>
                                    </>
                                  )}
                                </div>
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  ) : (
                    <Empty title="No future supply planned.">
                      Add an existing VISUAL work order and reserve its expected
                      output for a customer.
                    </Empty>
                  )}
                </section>
                <section className="panel inventory-form-panel">
                  <p className="eyebrow">SUPPLY PLANNING</p>
                  <h3>
                    {selectedSupply
                      ? "Update work-order supply"
                      : "Add work-order supply"}
                  </h3>
                  <p>
                    Use an existing production work order. This plan does not
                    increase on-hand stock.
                  </p>
                  {state.sync?.connected && (
                    <p className="inventory-callout">
                      VISUAL sync owns work-order dates, quantities, and status.
                      Allocate from the imported supply records; update the
                      production plan in VISUAL.
                    </p>
                  )}
                  {selectedSupply && (
                    <button
                      className="text-button"
                      disabled={viewLocked}
                      onClick={() => setEditingSupply("")}
                    >
                      Start a new supply record
                    </button>
                  )}
                  <form
                    key={"supply-" + revision + "-" + editingSupply}
                    onSubmit={(event) =>
                      submit(event, "supply", (data) => ({
                        ...(selectedSupply
                          ? {
                              id: selectedSupply.id,
                              version: selectedSupply.version,
                            }
                          : {}),
                        work_order: text(data, "work_order"),
                        part: text(data, "part"),
                        uom: text(data, "uom"),
                        serialized:
                          selectedSupply?.serialized ?? data.has("serialized"),
                        quantity: text(data, "quantity"),
                        due_date: text(data, "due_date"),
                        status: text(data, "status"),
                        reason: text(data, "reason"),
                      }))
                    }
                  >
                    <fieldset
                      disabled={locked || !!state.sync?.connected}
                      className="inventory-fields"
                    >
                      <Field
                        name="work_order"
                        label="VISUAL work order"
                        value={selectedSupply?.work_order}
                        readOnly={!!selectedSupply}
                      />
                      <Field
                        name="part"
                        label="Part number"
                        value={selectedSupply?.part}
                        readOnly={!!selectedSupply}
                      />
                      <Field
                        name="uom"
                        label="Unit of measure"
                        value={selectedSupply?.uom || "EA"}
                        readOnly={!!selectedSupply}
                      />
                      <Field
                        name="quantity"
                        label="Total expected quantity"
                        type="number"
                        min="0"
                        value={selectedSupply?.quantity}
                      />
                      <Field
                        name="due_date"
                        label="Expected arrival"
                        type="date"
                        value={selectedSupply?.due_date || ""}
                        required={false}
                      />
                      <label>
                        Status
                        <select
                          name="status"
                          defaultValue={selectedSupply?.status || "open"}
                        >
                          <option value="open">Open</option>
                          <option value="closed">Closed</option>
                          <option value="cancelled">Cancelled</option>
                        </select>
                      </label>
                      <label className="check-label inventory-span">
                        <input
                          type="checkbox"
                          name="serialized"
                          defaultChecked={selectedSupply?.serialized}
                          disabled={!!selectedSupply}
                        />
                        Serialized output
                      </label>
                      <Reason />
                      <button type="submit" className="inventory-span">
                        {selectedSupply
                          ? "Save supply changes"
                          : "Add future supply"}
                      </button>
                    </fieldset>
                  </form>
                  <p className="hint">
                    Reductions and cancellations keep customer promises visible.
                    Any uncovered commitment appears as a supply shortfall for
                    review.
                  </p>
                </section>
              </div>
            </>
          )}

          {section === "allocations" && (
            <>
              <div className="inventory-columns">
                <section className="panel">
                  <div className="section-title">
                    <h3>Customer commitments</h3>
                    <span>{allocations.length} active</span>
                  </div>
                  <label className="inventory-search">
                    Find a customer, order, or part
                    <input
                      value={query}
                      onChange={(event) => setQuery(event.target.value)}
                      placeholder="Customer / sales order / part"
                    />
                  </label>
                  <div className="inventory-filter-row">
                    <label>
                      Show commitments
                      <select
                        value={allocationFilter}
                        onChange={(event) =>
                          setAllocationFilter(event.target.value)
                        }
                      >
                        <option value="active">Active allocations</option>
                        <option value="held">Customer holds</option>
                        <option value="review">Review overdue</option>
                        <option value="risk">Supply or date at risk</option>
                        <option value="closed">
                          Completed / released / transferred
                        </option>
                        <option value="all">All history</option>
                      </select>
                    </label>
                    {commitmentPart && (
                      <div>
                        <strong>
                          {commitmentPart.part} · {commitmentPart.uom}
                        </strong>
                        <button
                          className="text-button"
                          onClick={() => setCommitmentPart(null)}
                        >
                          Clear stock filter
                        </button>
                      </div>
                    )}
                  </div>
                  {commitmentPart && (
                    <p className="hint">
                      Stock commitments for this part are shown below. Any
                      additional committed balance is already checked out in
                      fulfillment.
                    </p>
                  )}
                  {visibleAllocations.length ? (
                    <div className="inventory-allocation-list">
                      {visibleAllocations.map((item) => {
                        const supply = state.supplies.find(
                          (entry) => entry.id === item.source_id,
                        );
                        const parent = state.allocations.find(
                          (entry) => entry.id === item.parent_id,
                        );
                        const productionSource = state.supplies.find(
                          (entry) => entry.id === parent?.source_id,
                        );
                        return (
                          <article
                            key={item.id}
                            className={
                              "inventory-allocation " +
                              (!active(item) ? "inventory-inactive" : "")
                            }
                          >
                            <div className="inventory-allocation-heading">
                              <div>
                                <strong>{item.customer}</strong>
                                <span className="inventory-cell-note">
                                  {item.order
                                    ? "Order " +
                                      item.order +
                                      (item.order_line
                                        ? " / line " + item.order_line
                                        : "")
                                    : "Customer stock pool"}
                                </span>
                              </div>
                              <span
                                className={
                                  "pill " +
                                  (item.hold && active(item) ? "pill-warm" : "")
                                }
                              >
                                {!active(item)
                                  ? item.status
                                  : item.hold
                                    ? "HELD"
                                    : "ALLOCATED"}
                              </span>
                            </div>
                            <div className="inventory-allocation-detail">
                              <span>
                                <strong>{item.part}</strong>
                                <br />
                                {quantity(
                                  Number(item.quantity) -
                                    Number(item.fulfilled),
                                )}{" "}
                                {item.uom} remaining
                              </span>
                              <span>
                                {item.source === "stock"
                                  ? "On-hand stock"
                                  : "Work order " +
                                    (supply?.work_order || item.source_id)}
                                <br />
                                <small>
                                  {quantity(item.fulfilled)} of{" "}
                                  {quantity(item.quantity)}{" "}
                                  {item.source === "work_order"
                                    ? "received into stock"
                                    : "shipped"}
                                </small>
                              </span>
                            </div>
                            {item.at_risk && (
                              <div className="inventory-risk" role="status">
                                {(item.risks || []).map((risk) => (
                                  <p key={risk.code}>{risk.message}</p>
                                ))}
                              </div>
                            )}
                            <p>{item.reason}</p>
                            <p className="inventory-cell-note">
                              Priority {item.priority ?? 50}
                              {item.need_by ? " · Needed " + item.need_by : ""}
                              {item.request_reference
                                ? " · Request " + item.request_reference
                                : ""}
                            </p>
                            {item.receipt_event_id && (
                              <p className="inventory-cell-note">
                                Received on {item.receipt_event_id}
                                {productionSource
                                  ? " · Work order " +
                                    productionSource.work_order
                                  : item.parent_id
                                    ? " · From a production commitment"
                                    : ""}
                                <button
                                  className="text-button"
                                  disabled={viewLocked}
                                  onClick={() => {
                                    setQuery(item.receipt_event_id || "");
                                    setSection("history");
                                  }}
                                >
                                  View receipt history →
                                </button>
                              </p>
                            )}
                            {item.transfer_from && (
                              <p className="inventory-cell-note">
                                Transferred from another commitment
                              </p>
                            )}
                            {item.source === "stock" &&
                              !!item.items?.length && (
                                <details className="inventory-serials">
                                  <summary>Reserved stock details</summary>
                                  <ul>
                                    {item.items
                                      .filter(
                                        (entry) => Number(entry.quantity) > 0,
                                      )
                                      .map((entry) => {
                                        const stock = state.balances.find(
                                          (row) => row.id === entry.stock_id,
                                        );
                                        return (
                                          <li key={entry.stock_id}>
                                            {stock?.serial || item.part} ·{" "}
                                            {quantity(entry.quantity)}{" "}
                                            {item.uom}
                                          </li>
                                        );
                                      })}
                                  </ul>
                                </details>
                              )}
                            <div className="inventory-allocation-footer">
                              <small
                                className={
                                  item.review_due ? "inventory-review-due" : ""
                                }
                              >
                                {item.review_due ? "Review due · " : ""}
                                {item.review_at
                                  ? dateTime(item.review_at)
                                  : "No scheduled review"}
                              </small>
                              {active(item) && (
                                <button
                                  className="text-button"
                                  disabled={locked}
                                  onClick={() => setEditingAllocation(item.id)}
                                >
                                  Manage allocation →
                                </button>
                              )}
                            </div>
                          </article>
                        );
                      })}
                    </div>
                  ) : (
                    <Empty
                      title={
                        state.allocations.length
                          ? "No commitments match these filters."
                          : "No customer commitments yet."
                      }
                    >
                      {state.allocations.length
                        ? "Change the search or status filter to inspect other commitments."
                        : "Allocate stock or expected production, then hold it when inside sales needs a shipment paused."}
                    </Empty>
                  )}
                </section>
                <div>
                  {selectedAllocation && active(selectedAllocation) && (
                    <section className="panel inventory-form-panel">
                      <p className="eyebrow">MANAGE COMMITMENT</p>
                      <h3>{selectedAllocation.customer}</h3>
                      <p>
                        {selectedAllocation.part} ·{" "}
                        {quantity(
                          Number(selectedAllocation.quantity) -
                            Number(selectedAllocation.fulfilled),
                        )}{" "}
                        {selectedAllocation.uom} remaining
                      </p>
                      <p className="inventory-callout">
                        {selectedAllocation.hold
                          ? "Held inventory is protected from shipping. Remove the hold when the customer is ready."
                          : "This inventory is assigned to the customer. Place a hold to pause shipping, or release the allocation to return it to the available pool."}
                      </p>
                      <form
                        key={
                          "allocation-edit-" +
                          revision +
                          "-" +
                          editingAllocation
                        }
                        onSubmit={(event) =>
                          submit(event, "allocation-update", (data) => ({
                            id: selectedAllocation.id,
                            version: selectedAllocation.version,
                            reason: text(data, "reason"),
                            review_at: optionalDate(text(data, "review_at")),
                            priority: Number(text(data, "priority")),
                            request_reference: text(data, "request_reference"),
                            need_by: text(data, "need_by") || null,
                            ...(text(data, "change") === "release"
                              ? { status: "released" }
                              : text(data, "change") === "review"
                                ? {}
                                : { hold: text(data, "change") === "hold" }),
                          }))
                        }
                      >
                        <fieldset
                          disabled={locked}
                          className="inventory-fields"
                        >
                          <label className="inventory-span">
                            Change
                            <select name="change" defaultValue="review">
                              <option value="review">
                                Keep current hold status
                              </option>
                              <option value="hold">
                                Hold: keep allocation, pause shipping
                              </option>
                              <option value="unhold">
                                Remove hold: keep allocation, allow shipping
                              </option>
                              <option value="release">
                                Release allocation: return remaining supply
                              </option>
                            </select>
                          </label>
                          <Field
                            name="review_at"
                            label="Next review date / time"
                            type="datetime-local"
                            required={false}
                            value={localDateTime(selectedAllocation.review_at)}
                          />
                          <CommitmentFields allocation={selectedAllocation} />
                          <Reason />
                          <button className="inventory-span" type="submit">
                            Save allocation change
                          </button>
                        </fieldset>
                      </form>
                      <details className="inventory-transfer">
                        <summary>Split or move this commitment</summary>
                        <p>
                          Move all or part of the remaining quantity to another
                          customer or sales order. The original commitment keeps
                          its remaining balance and history. Checked-out stock
                          must be returned before it can move.
                        </p>
                        {Number(selectedAllocation.locked_quantity || 0) >
                          0 && (
                          <p className="inventory-callout">
                            {quantity(selectedAllocation.locked_quantity || 0)}{" "}
                            {selectedAllocation.uom} is checked out for
                            fulfillment and stays with this commitment.{" "}
                            {quantity(transferable)} {selectedAllocation.uom}{" "}
                            can move.
                          </p>
                        )}
                        <form
                          key={"transfer-" + revision + "-" + editingAllocation}
                          onSubmit={(event) =>
                            submit(event, "allocation-transfer", (data) => ({
                              id: selectedAllocation.id,
                              version: selectedAllocation.version,
                              quantity: text(data, "quantity"),
                              customer: text(data, "customer"),
                              order: text(data, "order") || null,
                              order_line: text(data, "order_line") || null,
                              hold: data.has("hold"),
                              review_at: optionalDate(text(data, "review_at")),
                              priority: Number(text(data, "priority")),
                              request_reference: text(
                                data,
                                "request_reference",
                              ),
                              need_by: text(data, "need_by") || null,
                              reason: text(data, "reason"),
                            }))
                          }
                        >
                          <fieldset
                            disabled={
                              locked || syncHealth.blocked || transferable <= 0
                            }
                            className="inventory-fields"
                          >
                            <Field
                              name="quantity"
                              label="Quantity to move"
                              type="number"
                              min="0.00000001"
                              max={transferable.toFixed(8)}
                              value={transferable.toFixed(8)}
                            />
                            <DemandFields
                              demand={demand}
                              part={selectedAllocation.part}
                              uom={selectedAllocation.uom}
                              defaults={selectedAllocation}
                            />
                            <label className="check-label inventory-span">
                              <input
                                name="hold"
                                type="checkbox"
                                defaultChecked={selectedAllocation.hold}
                              />
                              Hold the moved commitment for inside sales
                            </label>
                            <Field
                              name="review_at"
                              label="Review date for moved commitment"
                              type="datetime-local"
                              required={false}
                              value={localDateTime(
                                selectedAllocation.review_at,
                              )}
                            />
                            <CommitmentFields allocation={selectedAllocation} />
                            <Reason />
                            <button type="submit" className="inventory-span">
                              Move commitment
                            </button>
                          </fieldset>
                        </form>
                      </details>
                      <button
                        className="text-button"
                        disabled={viewLocked}
                        onClick={() => setEditingAllocation("")}
                      >
                        Close allocation
                      </button>
                    </section>
                  )}
                  <section className="panel inventory-form-panel">
                    <p className="eyebrow">ALLOCATION PLANNING</p>
                    <h3>Reserve for a customer</h3>
                    <p>
                      Customer pools protect supply for that customer. Add an
                      order and line to make a specific commitment.
                    </p>
                    <div className="inventory-demand-status">
                      <p className="hint">
                        {demandLoading
                          ? "Loading open orders…"
                          : demandError ||
                            (demand
                              ? "Open-order lookup refreshed " +
                                dateTime(demand.as_of)
                              : "Choose a known order or enter a customer pool manually.")}
                      </p>
                      <button
                        type="button"
                        className="text-button"
                        disabled={viewLocked || demandLoading || !online}
                        onClick={() => void loadDemand()}
                      >
                        Refresh open orders
                      </button>
                    </div>
                    <form
                      key={
                        "allocation-" +
                        revision +
                        "-" +
                        allocationSource +
                        "-" +
                        allocationSupply +
                        "-" +
                        stockSelection?.part
                      }
                      onSubmit={(event) =>
                        submit(event, "allocate", (data) => ({
                          source: allocationSource,
                          ...(allocationSource === "work_order"
                            ? { source_id: allocationSupply }
                            : {}),
                          part:
                            allocationSource === "work_order"
                              ? allocatedSupply?.part
                              : stockSelection?.part,
                          uom:
                            allocationSource === "work_order"
                              ? allocatedSupply?.uom
                              : stockSelection?.uom,
                          customer: text(data, "customer"),
                          order: text(data, "order") || null,
                          order_line: text(data, "order_line") || null,
                          quantity: text(data, "quantity"),
                          hold: data.has("hold"),
                          priority: Number(text(data, "priority")),
                          request_reference: text(data, "request_reference"),
                          need_by: text(data, "need_by") || null,
                          review_at: optionalDate(text(data, "review_at")),
                          reason: text(data, "reason"),
                        }))
                      }
                    >
                      <fieldset disabled={locked} className="inventory-fields">
                        <label className="inventory-span">
                          Supply source
                          <select
                            value={allocationSource}
                            onChange={(event) =>
                              setAllocationSource(
                                event.target.value as "stock" | "work_order",
                              )
                            }
                          >
                            <option value="stock">
                              On-hand shipping stock
                            </option>
                            <option value="work_order">
                              Future work-order supply
                            </option>
                          </select>
                        </label>
                        {allocationSource === "work_order" ? (
                          <label className="inventory-span">
                            Work order
                            <select
                              required
                              value={allocationSupply}
                              onChange={(event) =>
                                setAllocationSupply(event.target.value)
                              }
                            >
                              <option value="">Choose a work order</option>
                              {openSupplies.map((item) => (
                                <option
                                  key={item.id}
                                  value={item.id}
                                  disabled={Number(item.available) <= 0}
                                >
                                  {item.work_order} · {item.part} ·{" "}
                                  {quantity(item.available)} {item.uom}{" "}
                                  unallocated
                                </option>
                              ))}
                            </select>
                          </label>
                        ) : (
                          <label className="inventory-span">
                            Shipping stock
                            <select
                              required
                              value={
                                stockSelection
                                  ? JSON.stringify([
                                      stockSelection.part,
                                      stockSelection.uom,
                                    ])
                                  : ""
                              }
                              onChange={(event) => {
                                const choice = stockOptions.find(
                                  (item) =>
                                    JSON.stringify([item.part, item.uom]) ===
                                    event.target.value,
                                );
                                setStockSelection(
                                  choice
                                    ? { part: choice.part, uom: choice.uom }
                                    : null,
                                );
                              }}
                            >
                              <option value="">Choose a stocked part</option>
                              {stockOptions.map((item) => (
                                <option
                                  key={JSON.stringify([item.part, item.uom])}
                                  value={JSON.stringify([item.part, item.uom])}
                                  disabled={item.available <= 0}
                                >
                                  {item.part} · {quantity(item.available)}{" "}
                                  {item.uom} available
                                </option>
                              ))}
                            </select>
                          </label>
                        )}
                        <p className="inventory-callout inventory-span">
                          {quantity(allocationAvailable)}{" "}
                          {allocationSource === "work_order"
                            ? allocatedSupply?.uom
                            : allocatedStock?.uom}{" "}
                          available to allocate from this source.
                        </p>
                        <DemandFields
                          demand={demand}
                          part={
                            allocationSource === "work_order"
                              ? allocatedSupply?.part
                              : stockSelection?.part
                          }
                          uom={
                            allocationSource === "work_order"
                              ? allocatedSupply?.uom
                              : stockSelection?.uom
                          }
                        />
                        <Field
                          name="quantity"
                          label="Quantity to allocate"
                          type="number"
                          min="0.00000001"
                          max={allocationAvailable.toFixed(8)}
                        />
                        <CommitmentFields />
                        <label className="check-label inventory-span">
                          <input type="checkbox" name="hold" />
                          Hold for inside sales approval
                        </label>
                        <Field
                          name="review_at"
                          label="Review date / time (optional)"
                          type="datetime-local"
                          required={false}
                        />
                        <Reason />
                        <button
                          type="submit"
                          className="inventory-span"
                          disabled={allocationAvailable <= 0}
                        >
                          Create allocation
                        </button>
                      </fieldset>
                    </form>
                    <p className="hint">
                      A review date is a reminder for review. It does not
                      automatically remove a customer hold.
                    </p>
                  </section>
                </div>
              </div>
            </>
          )}

          {section === "sync" && (
            <>
              <InventorySync
                sync={state.sync}
                now={clock}
                supplies={state.supplies}
                locked={locked}
                revision={revision}
                submit={submit}
              />
              <section className="panel">
                <div className="section-title">
                  <h3>VISUAL reconciliation</h3>
                  <span
                    className={
                      "pill " +
                      (latestReconciliation?.differences.length
                        ? "pill-warm"
                        : "")
                    }
                  >
                    {latestReconciliation
                      ? latestReconciliation.status
                      : "NO SNAPSHOT YET"}
                  </span>
                </div>
                <p>
                  Compare an independent VISUAL warehouse snapshot with the
                  shipping ledger. A comparison records discrepancies for
                  review; it does not overwrite stock.
                </p>
                {latestReconciliation ? (
                  <>
                    <p className="hint">
                      Snapshot observed{" "}
                      {dateTime(latestReconciliation.observed_at)} ·{" "}
                      {latestReconciliation.differences.length} differences
                    </p>
                    {latestReconciliation.differences.length ? (
                      <div className="inventory-table inventory-differences">
                        <table>
                          <thead>
                            <tr>
                              <th>Part / serial</th>
                              <th>Dispatch</th>
                              <th>VISUAL</th>
                              <th>Difference</th>
                            </tr>
                          </thead>
                          <tbody>
                            {latestReconciliation.differences.map(
                              (item, index) => (
                                <tr key={index}>
                                  <td>
                                    <strong>{item.part}</strong>
                                    <span className="inventory-cell-note">
                                      {item.serial || "Quantity stock"} ·{" "}
                                      {item.uom}
                                    </span>
                                  </td>
                                  <td>{quantity(item.local)}</td>
                                  <td>{quantity(item.visual)}</td>
                                  <td>{quantity(item.delta)}</td>
                                </tr>
                              ),
                            )}
                          </tbody>
                        </table>
                      </div>
                    ) : (
                      <p className="inventory-callout">
                        No discrepancies in the latest snapshot.
                      </p>
                    )}
                  </>
                ) : (
                  <Empty title="Opening reconciliation required.">
                    Import the independent warehouse count after receiving the
                    opening inventory, then activate app-owned inventory.
                  </Empty>
                )}
                {canManage && (
                  <details className="inventory-import">
                    <summary>Record a VISUAL snapshot</summary>
                    <p>
                      Paste the complete balance array from the configured
                      shipping location. Include every on-hand part and serial.
                      An empty array represents a verified empty warehouse.
                    </p>
                    <form
                      key={"reconcile-" + revision}
                      onSubmit={(event) =>
                        submit(event, "reconcile", (data) => {
                          const balances: unknown = JSON.parse(
                            text(data, "balances"),
                          );
                          if (!Array.isArray(balances))
                            throw new Error(
                              "The snapshot must be a JSON array of warehouse balances.",
                            );
                          return {
                            event_id: text(data, "event_id"),
                            site: state.config.site,
                            warehouse: state.config.warehouse,
                            location: state.config.location,
                            observed_at: optionalDate(
                              text(data, "observed_at"),
                            ),
                            balances,
                          };
                        })
                      }
                    >
                      <fieldset
                        disabled={
                          locked ||
                          !state.config.site ||
                          !state.config.warehouse ||
                          !state.config.location
                        }
                        className="inventory-fields"
                      >
                        <Field
                          name="event_id"
                          label="Unique snapshot reference"
                          placeholder="VISUAL snapshot reference"
                        />
                        <Field
                          name="observed_at"
                          label="Snapshot observed at"
                          type="datetime-local"
                        />
                        <label className="inventory-span">
                          Full warehouse balance array (JSON)
                          <textarea
                            name="balances"
                            rows={7}
                            required
                            spellCheck={false}
                            placeholder={
                              '[{"part":"PART-ID","uom":"EA","quantity":"2","serials":[]}]'
                            }
                          />
                        </label>
                        <button type="submit">Compare snapshot</button>
                      </fieldset>
                    </form>
                  </details>
                )}
                {state.reconciliations.length > 1 && (
                  <details>
                    <summary>Previous comparisons</summary>
                    <div className="inventory-table">
                      <table>
                        <thead>
                          <tr>
                            <th>Recorded</th>
                            <th>Status</th>
                            <th>Differences</th>
                          </tr>
                        </thead>
                        <tbody>
                          {state.reconciliations.slice(1).map((item) => (
                            <tr key={item.id}>
                              <td>{dateTime(item.at)}</td>
                              <td>{item.status}</td>
                              <td>{item.differences.length}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  </details>
                )}
              </section>
              <section className="panel inventory-form-panel">
                <p className="eyebrow">WAREHOUSE CONTROL</p>
                <h3>One shipping location</h3>
                <p>
                  All app-owned balances belong to this site, warehouse, and
                  location.
                </p>
                <ol className="inventory-steps">
                  <li>
                    Save the shipping location with app inventory inactive.
                  </li>
                  <li>
                    Receive opening stock and compare a complete VISUAL
                    snapshot.
                  </li>
                  <li>
                    Finish open fulfillment work and resolve queued legacy
                    reservations, then activate with a matching snapshot
                    observed within the past hour.
                  </li>
                </ol>
                <form
                  key={"config-" + revision}
                  onSubmit={(event) =>
                    submit(event, "configure", (data) => ({
                      version: state.config.version,
                      site: text(data, "site"),
                      warehouse: text(data, "warehouse"),
                      location: text(data, "location"),
                      enabled: data.has("enabled"),
                      reason: text(data, "reason"),
                    }))
                  }
                >
                  <fieldset
                    disabled={locked || session.operator.role !== "admin"}
                    className="inventory-fields inventory-wide-fields"
                  >
                    <Field
                      name="site"
                      label="VISUAL site"
                      value={state.config.site}
                    />
                    <Field
                      name="warehouse"
                      label="Shipping warehouse"
                      value={state.config.warehouse}
                    />
                    <Field
                      name="location"
                      label="Shipping location"
                      value={state.config.location}
                    />
                    <label className="check-label inventory-span">
                      <input
                        name="enabled"
                        type="checkbox"
                        defaultChecked={state.config.enabled}
                      />
                      Activate app-owned inventory for shipping
                    </label>
                    <Reason />
                    <button type="submit">Save warehouse configuration</button>
                  </fieldset>
                </form>
                <p className="hint">
                  Administrator access required. Location changes and activation
                  are checked against existing stock, commitments, open work,
                  and reconciliation.
                </p>
              </section>
            </>
          )}

          {section === "history" && (
            <section className="panel">
              <div className="section-title">
                <h3>Inventory movement history</h3>
                <span>{state.ledger.length} recent entries</span>
              </div>
              <p>
                Every receipt, correction, and shipment leaves an inventory
                record.
              </p>
              <label className="inventory-search">
                Find a movement
                <input
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="Part, serial, receipt reference, or operator"
                />
              </label>
              {state.ledger.length ? (
                <div className="inventory-table">
                  <table>
                    <thead>
                      <tr>
                        <th>Recorded</th>
                        <th>Action / reference</th>
                        <th>Part</th>
                        <th>Quantity</th>
                        <th>By</th>
                      </tr>
                    </thead>
                    <tbody>
                      {state.ledger
                        .filter((item) =>
                          matches(
                            item.part,
                            item.serial,
                            item.reference,
                            item.actor,
                            item.reason,
                          ),
                        )
                        .map((item) => (
                          <tr key={item.id}>
                            <td>{dateTime(item.at)}</td>
                            <td>
                              {item.kind.replaceAll("_", " ")}
                              <span className="inventory-cell-note">
                                {item.reference}
                              </span>
                              <span className="inventory-cell-note">
                                {item.reason}
                              </span>
                            </td>
                            <td>
                              {item.part || "—"}
                              {item.serial && (
                                <span className="inventory-cell-note">
                                  Serial {item.serial}
                                </span>
                              )}
                            </td>
                            <td>
                              {quantity(item.quantity)} {item.uom}
                              <span className="inventory-cell-note">
                                Balance {quantity(item.balance)}
                              </span>
                            </td>
                            <td>{item.actor}</td>
                          </tr>
                        ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <Empty title="No inventory movements yet.">
                  Receipts and stock changes will appear here as they are
                  confirmed.
                </Empty>
              )}
            </section>
          )}
        </>
      )}
    </section>
  );
}
