import React, { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { Session, Work, Destination, Operator } from "./types";
import { effectivePolicy } from "./admin-policy";

export type Policy = {
  account_type?: "standard" | "major";
  accumulate?: boolean;
  target_guns?: number;
  min_guns?: number;
  sweep_weekday?: number | null;
  mix_orders?: boolean;
  release_cadence?: "completion" | "threshold" | "daily";
  daily_release_time?: string;
  max_hold_days?: number;
  ship_to_cooldown_days?: number;
};
type Rules = {
  policies: Record<string, Policy>;
  due_days: number;
  cooldown: number;
  lookahead_days: number;
  timezone: string;
  document_checks_required: boolean;
  component_codes: string[];
  excluded_customers: string[];
};
type Control = {
  id: string;
  kind: string;
  order: string;
  reason: string;
  expires_at: string | null;
  active: boolean;
  revoked_at: string | null;
  consumed_by?: string;
  decision?: string;
  reference?: string;
};
type Decision = {
  order_id: string;
  customer_name?: string;
  decision: string;
  label: string;
};
type Preview = {
  preview: string;
  version: number;
  newly_eligible: string[];
  no_longer_eligible: string[];
  changed_prescriptions: string[];
  eligible_count: number;
  active_work_unchanged: number;
  decisions: Decision[];
};
type State = {
  version: number;
  configured: boolean;
  rules: Rules;
  controls: Control[];
  users: (Operator & { enabled: boolean })[];
  allocation_worker: {
    at: string;
    reservations: number;
    eligible_orders: number;
  } | null;
  reservations: {
    serial_no: string;
    part_id: string;
    cust_order_id: string;
    line_no: string | number;
  }[];
  work: Work[];
  audit: {
    id: string;
    at: string;
    actor: string;
    action: string;
    detail: unknown;
  }[];
};
type Inspection = {
  order: string;
  customer: string;
  destination: Destination;
  document_fingerprint: string;
  document_context: Record<string, unknown>;
};
type Pending = { action: string; body: Record<string, unknown>; key: string };
const names: Record<string, string> = {
  rules: "Release rules",
  orders: "Orders & assignments",
  people: "People",
  history: "Change history",
};

export function Admin({
  session,
  online,
  onOpenWork,
  onAuthLost,
  onLock,
}: {
  session: Session;
  online: boolean;
  onOpenWork: (work: Work) => void;
  onAuthLost: () => void;
  onLock: (locked: boolean) => void;
}) {
  const [section, setSection] = useState("rules");
  const [state, setState] = useState<State | null>(null),
    [rules, setRules] = useState<Rules | null>(null);
  const [decisions, setDecisions] = useState<Decision[]>([]),
    [sourceError, setSourceError] = useState("");
  const [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null),
    [preview, setPreview] = useState<Preview | null>(null);
  const [reason, setReason] = useState(""),
    [customer, setCustomer] = useState("DEFAULT"),
    [newCustomer, setNewCustomer] = useState("");
  const [order, setOrder] = useState(""),
    [inspection, setInspection] = useState<Inspection | null>(null);
  const [expires, setExpires] = useState(""),
    [reference, setReference] = useState("");
  const [person, setPerson] = useState({
    operator: "",
    name: "",
    role: "operator",
    enabled: true,
    pin: "",
  });
  const locked = busy || !!pending || !online;
  useEffect(() => {
    onLock(busy || !!pending);
  }, [busy, pending, onLock]);
  useEffect(() => () => onLock(false), [onLock]);

  async function refresh(resetRules = true) {
    const data = await api<State>("/admin/state");
    setState(data);
    if (resetRules) {
      setRules(data.rules);
      setPreview(null);
    }
    try {
      const result = await api<{ decisions: Decision[] }>("/admin/orders");
      setDecisions(result.decisions || []);
      setSourceError("");
    } catch (e) {
      setSourceError((e as Error).message);
    }
  }
  useEffect(() => {
    refresh().catch((e) => {
      setError(e.message);
      if (e instanceof ApiError && e.status === 401) onAuthLost();
    });
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
      const result = await api<Preview>(
        "/admin/" + action,
        body,
        session.csrf,
        key,
      );
      setPending(null);
      if (action === "preview") {
        setPreview(result);
        setNotice("Preview ready. Review the impact before publishing.");
      } else {
        setNotice("Confirmed and saved in Dispatch.");
        setPreview(null);
        setPerson((p) => ({ ...p, pin: "" }));
        try {
          await refresh();
        } catch (e) {
          if (e instanceof ApiError && e.status === 401) onAuthLost();
          else
            setNotice("Saved. Reload administration to see the current state.");
        }
      }
    } catch (e) {
      setError((e as Error).message);
      if (!(e instanceof ApiError) || e.status >= 500)
        setPending({ action, body, key });
      else {
        setPending(null);
        if (e.status === 401) onAuthLost();
        if (e.status === 409) setPreview(null);
      }
    } finally {
      setBusy(false);
    }
  }
  function command(action: string, values: Record<string, unknown>) {
    if (state) void run(action, { version: state.version, reason, ...values });
  }
  function edit<K extends keyof Rules>(key: K, value: Rules[K]) {
    if (rules) setRules({ ...rules, [key]: value });
    setPreview(null);
  }
  function editPolicy(value: Policy) {
    if (rules) edit("policies", { ...rules.policies, [customer]: value });
  }
  function expiration() {
    return expires ? new Date(expires).toISOString() : null;
  }
  async function inspect() {
    setBusy(true);
    setError("");
    setInspection(null);
    try {
      setInspection(
        await api<Inspection>(
          "/admin/order/" + encodeURIComponent(order.trim()),
        ),
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const rawPolicy = rules?.policies[customer];
  const policy =
    rawPolicy && effectivePolicy(rules?.policies.DEFAULT, rawPolicy);

  return (
    <section className="administration">
      <div className="section-title">
        <div>
          <p className="eyebrow">ADMINISTRATION</p>
          <h2>Your warehouse, your rules.</h2>
        </div>
        <button
          className="secondary"
          disabled={locked}
          onClick={() => refresh().catch((e) => setError(e.message))}
        >
          Reload saved settings
        </button>
      </div>
      <p className="hint">
        Rules, exceptions, people and assignments are stored in Dispatch. Every
        change has a reason and a history.
      </p>
      <div
        className="admin-tabs"
        role="tablist"
        aria-label="Administration sections"
      >
        {Object.entries(names).map(([id, name]) => (
          <button
            role="tab"
            aria-selected={section === id}
            className={section === id ? "selected" : "secondary"}
            key={id}
            disabled={locked}
            onClick={() => setSection(id)}
          >
            {name}
          </button>
        ))}
      </div>
      {error && (
        <div className="alert" role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="notice" role="status">
          {notice}
        </div>
      )}
      {pending && (
        <div className="alert">
          Confirmation is uncertain. Retry this same change before making
          another.
          <button
            disabled={busy || !online}
            onClick={() => run(pending.action, pending.body, pending.key)}
          >
            Retry admin confirmation
          </button>
        </div>
      )}
      {!state || !rules ? (
        <p>Loading administration…</p>
      ) : (
        <>
          <p className="hint">
            Revision {state.version} ·{" "}
            {state.configured
              ? "Settings published"
              : "Setup required: preview and publish your settings"}
          </p>
          {section !== "history" && (
            <label className="admin-reason">
              Reason for change
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                disabled={locked}
                placeholder="Explain why this change is needed"
                maxLength={1000}
              />
            </label>
          )}
          {section === "rules" && (
            <>
              <fieldset disabled={locked} className="panel admin-fields">
                <legend>General release settings</legend>
                <label>
                  Look ahead (days)
                  <input
                    type="number"
                    min={0}
                    max={365}
                    value={rules.lookahead_days}
                    onChange={(e) =>
                      edit("lookahead_days", Number(e.target.value))
                    }
                  />
                </label>
                <label>
                  Promise-date urgency window (days)
                  <input
                    type="number"
                    min={0}
                    max={365}
                    value={rules.due_days}
                    onChange={(e) => edit("due_days", Number(e.target.value))}
                  />
                </label>
                <label>
                  Minimum destination cooldown (days)
                  <input
                    type="number"
                    min={0}
                    max={30}
                    value={rules.cooldown}
                    onChange={(e) => edit("cooldown", Number(e.target.value))}
                  />
                </label>
                <label>
                  Warehouse timezone
                  <input
                    value={rules.timezone}
                    onChange={(e) => edit("timezone", e.target.value)}
                  />
                </label>
                <label>
                  Component product codes (comma separated)
                  <input
                    value={rules.component_codes.join(", ")}
                    onChange={(e) =>
                      edit(
                        "component_codes",
                        e.target.value
                          ? e.target.value.split(",").map((s) => s.trim())
                          : [],
                      )
                    }
                  />
                </label>
                <label>
                  Excluded customer terms (comma separated)
                  <input
                    value={rules.excluded_customers.join(", ")}
                    onChange={(e) =>
                      edit(
                        "excluded_customers",
                        e.target.value
                          ? e.target.value.split(",").map((s) => s.trim())
                          : [],
                      )
                    }
                  />
                </label>
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={rules.document_checks_required}
                    onChange={(e) =>
                      edit("document_checks_required", e.target.checked)
                    }
                  />
                  Require a current document review for each order
                </label>
                <p className="hint">
                  Document approvals are tied to the current destination and ERP
                  document facts. Changed or expired reviews block release. ERP
                  order, credit and inventory checks always apply.
                </p>
              </fieldset>
              <fieldset disabled={locked} className="panel">
                <legend>Customer policies</legend>
                <div className="admin-inline">
                  <label>
                    Customer policy
                    <select
                      value={customer}
                      onChange={(e) => setCustomer(e.target.value)}
                    >
                      <option value="">Choose a policy</option>
                      {Object.keys(rules.policies).map((k) => (
                        <option key={k}>{k}</option>
                      ))}
                    </select>
                  </label>
                  <label>
                    Add customer ID
                    <input
                      value={newCustomer}
                      onChange={(e) =>
                        setNewCustomer(e.target.value.toUpperCase())
                      }
                      placeholder="Customer ID or DEFAULT"
                    />
                  </label>
                  <button
                    type="button"
                    disabled={
                      !newCustomer.trim() ||
                      !!rules.policies[newCustomer.trim()]
                    }
                    onClick={() => {
                      const id = newCustomer.trim();
                      edit("policies", {
                        ...rules.policies,
                        [id]: {},
                      });
                      setCustomer(id);
                      setNewCustomer("");
                    }}
                  >
                    Add policy
                  </button>
                </div>
                <p className="hint">
                  DEFAULT supplies common settings. A customer-specific policy
                  overrides those fields.
                </p>
                {policy && (
                  <div className="admin-fields">
                    <label>
                      Account type
                      <select
                        value={policy.account_type || "standard"}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            account_type: e.target
                              .value as Policy["account_type"],
                          })
                        }
                      >
                        <option value="standard">Standard</option>
                        <option value="major">Major</option>
                      </select>
                    </label>
                    <label>
                      Release cadence
                      <select
                        value={policy.release_cadence || "completion"}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            release_cadence: e.target
                              .value as Policy["release_cadence"],
                          })
                        }
                      >
                        <option value="completion">Order completion</option>
                        <option value="threshold">
                          Quantity threshold / sweep
                        </option>
                        <option value="daily">Daily release time</option>
                      </select>
                    </label>
                    <label>
                      Serialized-unit target
                      <input
                        type="number"
                        min={0}
                        max={100000}
                        value={policy.target_guns ?? policy.min_guns ?? 0}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            target_guns: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                    <label>
                      Maximum hold (days)
                      <input
                        type="number"
                        min={0}
                        max={90}
                        value={policy.max_hold_days ?? 7}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            max_hold_days: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                    <label>
                      Customer destination cooldown (days)
                      <input
                        type="number"
                        min={0}
                        max={30}
                        value={policy.ship_to_cooldown_days ?? 0}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            ship_to_cooldown_days: Number(e.target.value),
                          })
                        }
                      />
                    </label>
                    <label>
                      Daily release time
                      <input
                        type="time"
                        value={policy.daily_release_time || "14:00"}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            daily_release_time: e.target.value,
                          })
                        }
                      />
                    </label>
                    <label>
                      Sweep day
                      <select
                        value={policy.sweep_weekday ?? ""}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            sweep_weekday:
                              e.target.value === ""
                                ? null
                                : Number(e.target.value),
                          })
                        }
                      >
                        <option value="">No scheduled sweep</option>
                        {[
                          "Monday",
                          "Tuesday",
                          "Wednesday",
                          "Thursday",
                          "Friday",
                          "Saturday",
                          "Sunday",
                        ].map((day, i) => (
                          <option value={i} key={day}>
                            {day}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="check-label">
                      <input
                        type="checkbox"
                        checked={policy.accumulate ?? false}
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            accumulate: e.target.checked,
                          })
                        }
                      />
                      Reserve stock while waiting
                    </label>
                    <label className="check-label">
                      <input
                        type="checkbox"
                        checked={
                          policy.mix_orders ?? policy.account_type !== "major"
                        }
                        onChange={(e) =>
                          editPolicy({
                            ...rawPolicy,
                            mix_orders: e.target.checked,
                          })
                        }
                      />
                      Combine orders toward the release target
                    </label>
                    <button
                      className="secondary"
                      onClick={() => {
                        const updated = { ...rules.policies };
                        delete updated[customer];
                        edit("policies", updated);
                        setCustomer("");
                      }}
                    >
                      Remove this policy
                    </button>
                  </div>
                )}
              </fieldset>
              <div className="admin-inline">
                <button
                  disabled={locked}
                  onClick={() => command("preview", { rules })}
                >
                  Preview rule impact
                </button>
                <button
                  disabled={locked || !preview || reason.trim().length < 3}
                  onClick={() =>
                    command("rules", { rules, preview: preview?.preview })
                  }
                >
                  Publish settings
                </button>
              </div>
              {preview && (
                <div className="panel admin-preview">
                  <h3>What would change</h3>
                  <p>
                    {preview.eligible_count} eligible orders ·{" "}
                    {preview.active_work_unchanged} active picks keep their
                    assignments.
                  </p>
                  <p>
                    <strong>Newly eligible:</strong>{" "}
                    {preview.newly_eligible.join(", ") || "None"}
                  </p>
                  <p>
                    <strong>No longer eligible:</strong>{" "}
                    {preview.no_longer_eligible.join(", ") || "None"}
                  </p>
                  <p>
                    <strong>Changed queued prescriptions:</strong>{" "}
                    {preview.changed_prescriptions.join(", ") || "None"}
                  </p>
                  <p className="hint">
                    Preview uses current inventory. Publishing rechecks queued
                    allocations. Existing picks keep their serials; current
                    holds and document requirements still apply.
                  </p>
                </div>
              )}
            </>
          )}
          {section === "orders" && (
            <>
              {sourceError && (
                <div className="alert">
                  Order evaluation unavailable: {sourceError}
                </div>
              )}
              <div className="panel">
                <h3>Order controls</h3>
                <div className="admin-inline">
                  <label>
                    Sales order
                    <input
                      value={order}
                      onChange={(e) => {
                        setOrder(e.target.value.toUpperCase());
                        setInspection(null);
                      }}
                      disabled={locked}
                      placeholder="SO-DEMO-100"
                    />
                  </label>
                  <button disabled={locked || !order.trim()} onClick={inspect}>
                    Load order
                  </button>
                </div>
                {inspection && (
                  <>
                    <h4>
                      {inspection.order} · {inspection.customer}
                    </h4>
                    <p>
                      {inspection.destination.name} ·{" "}
                      {inspection.destination.address1} ·{" "}
                      {inspection.destination.city}{" "}
                      {inspection.destination.postal}
                    </p>
                    <label>
                      Expires at (this device's local time)
                      <input
                        type="datetime-local"
                        value={expires}
                        disabled={locked}
                        onChange={(e) => setExpires(e.target.value)}
                      />
                    </label>
                    <p className="hint">
                      Early release applies to the next checkout only and
                      requires an expiry. Holds can remain until revoked.
                    </p>
                    <div className="admin-inline">
                      <button
                        disabled={
                          locked || !expires || reason.trim().length < 3
                        }
                        onClick={() =>
                          command("control", {
                            order: inspection.order,
                            kind: "early_release",
                            expires_at: expiration(),
                          })
                        }
                      >
                        Allow early release
                      </button>
                      <button
                        className="secondary"
                        disabled={locked || reason.trim().length < 3}
                        onClick={() =>
                          command("control", {
                            order: inspection.order,
                            kind: "hold",
                            expires_at: expiration(),
                          })
                        }
                      >
                        Hold order
                      </button>
                      <button
                        className="secondary"
                        disabled={locked || reason.trim().length < 3}
                        onClick={() =>
                          command("unassign", { order: inspection.order })
                        }
                      >
                        Clear queued assignments & hold
                      </button>
                    </div>
                    <p className="hint">
                      Clearing assignments is allowed only when there is no
                      active work. The hold prevents the allocator from
                      immediately assigning the same stock again.
                    </p>
                    <details>
                      <summary>Review supporting documents</summary>
                      <p>
                        Check the actual documents and destination before
                        recording your conclusion. This records your review; it
                        does not perform OCR.
                      </p>
                      <dl className="document-facts">
                        {Object.entries(inspection.document_context).map(
                          ([k, v]) => (
                            <React.Fragment key={k}>
                              <dt>{k.replaceAll("_", " ")}</dt>
                              <dd>{String(v ?? "—")}</dd>
                            </React.Fragment>
                          ),
                        )}
                      </dl>
                      <label>
                        Document / review reference
                        <input
                          value={reference}
                          disabled={locked}
                          onChange={(e) => setReference(e.target.value)}
                        />
                      </label>
                      <div className="admin-inline">
                        {["pass", "blocked"].map((decision) => (
                          <button
                            className="secondary"
                            key={decision}
                            disabled={
                              locked ||
                              !expires ||
                              !reference.trim() ||
                              reason.trim().length < 3
                            }
                            onClick={() =>
                              command("control", {
                                order: inspection.order,
                                kind: "document_review",
                                decision,
                                reference,
                                fingerprint: inspection.document_fingerprint,
                                expires_at: expiration(),
                              })
                            }
                          >
                            {decision === "pass"
                              ? "Record reviewed match"
                              : "Record document problem"}
                          </button>
                        ))}
                      </div>
                    </details>
                  </>
                )}
              </div>
              <div className="panel">
                <h3>Release decisions</h3>
                <div className="admin-table">
                  <table>
                    <thead>
                      <tr>
                        <th>Order</th>
                        <th>Decision</th>
                        <th>Why</th>
                      </tr>
                    </thead>
                    <tbody>
                      {decisions.map((d) => (
                        <tr key={d.order_id}>
                          <td>
                            <button
                              className="text-button"
                              disabled={locked}
                              onClick={() => {
                                setOrder(d.order_id);
                                setInspection(null);
                              }}
                            >
                              {d.order_id}
                            </button>
                          </td>
                          <td>{d.decision}</td>
                          <td>{d.label}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {!decisions.length && (
                  <p className="hint">No queued order decisions.</p>
                )}
              </div>
              <div className="panel">
                <h3>Controls and exceptions</h3>
                {state.controls.length ? (
                  state.controls
                    .slice()
                    .reverse()
                    .map((c) => (
                      <div className="admin-control" key={c.id}>
                        <div>
                          <strong>
                            {c.order} · {c.kind.replaceAll("_", " ")}
                          </strong>
                          <p>{c.reason}</p>
                          <small>
                            {c.active
                              ? "Active"
                              : c.consumed_by
                                ? "Used at checkout"
                                : c.revoked_at
                                  ? "Revoked"
                                  : "Expired"}{" "}
                            ·{" "}
                            {c.expires_at
                              ? new Date(c.expires_at).toLocaleString()
                              : "Until revoked"}
                          </small>
                        </div>
                        {!c.revoked_at && !c.consumed_by && (
                          <button
                            className="secondary"
                            disabled={locked || reason.trim().length < 3}
                            onClick={() => command("revoke", { id: c.id })}
                          >
                            Revoke
                          </button>
                        )}
                      </div>
                    ))
                ) : (
                  <p>No order controls recorded.</p>
                )}
              </div>
              <div className="panel">
                <h3>Queued serial reservations</h3>
                <p className="hint">
                  Last reconciliation:{" "}
                  {state.allocation_worker
                    ? new Date(state.allocation_worker.at).toLocaleString()
                    : "Not run yet"}
                  . The Dispatch worker maintains these independently.
                </p>
                <button
                  className="secondary"
                  disabled={
                    locked ||
                    session.mode === "shadow" ||
                    reason.trim().length < 3
                  }
                  onClick={() => command("reconcile", {})}
                >
                  Recalculate queued assignments
                </button>
                <div className="admin-table">
                  <table>
                    <thead>
                      <tr>
                        <th>Order / line</th>
                        <th>Part</th>
                        <th>Serial</th>
                      </tr>
                    </thead>
                    <tbody>
                      {state.reservations.map((r, i) => (
                        <tr key={i}>
                          <td>
                            {r.cust_order_id} / {r.line_no}
                          </td>
                          <td>{r.part_id}</td>
                          <td>{r.serial_no}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                {!state.reservations.length && (
                  <p className="hint">
                    No queued reservations. Checked-out serials remain attached
                    to active work below.
                  </p>
                )}
              </div>
              <div className="panel">
                <h3>Active work</h3>
                {state.work
                  .filter((w) => !["released", "cancelled"].includes(w.state))
                  .map((w) => (
                    <div className="admin-control" key={w.id}>
                      <div>
                        <strong>{w.order}</strong>
                        <p>
                          {w.cart} · {w.operator} ·{" "}
                          {w.state.replaceAll("_", " ")}
                        </p>
                      </div>
                      <button disabled={locked} onClick={() => onOpenWork(w)}>
                        Manage work
                      </button>
                    </div>
                  ))}
                <p className="hint">
                  Open work to transfer custody, substitute an unpicked serial,
                  or cancel and complete putaway.
                </p>
              </div>
            </>
          )}
          {section === "people" && (
            <>
              <div className="panel">
                <h3>Operator accounts</h3>
                {state.users.map((u) => (
                  <div className="admin-control" key={u.id}>
                    <div>
                      <strong>
                        {u.name} · {u.id}
                      </strong>
                      <p>
                        {u.role} · {u.enabled ? "Enabled" : "Disabled"}
                      </p>
                    </div>
                    <button
                      className="secondary"
                      disabled={locked}
                      onClick={() =>
                        setPerson({
                          operator: u.id,
                          name: u.name,
                          role: u.role,
                          enabled: u.enabled,
                          pin: "",
                        })
                      }
                    >
                      Edit account
                    </button>
                  </div>
                ))}
              </div>
              <fieldset disabled={locked} className="panel admin-fields">
                <legend>Create or update an account</legend>
                <label>
                  Operator ID
                  <input
                    value={person.operator}
                    onChange={(e) =>
                      setPerson({
                        ...person,
                        operator: e.target.value.toUpperCase(),
                      })
                    }
                  />
                </label>
                <label>
                  Display name
                  <input
                    value={person.name}
                    onChange={(e) =>
                      setPerson({ ...person, name: e.target.value })
                    }
                  />
                </label>
                <label>
                  Role
                  <select
                    value={person.role}
                    onChange={(e) =>
                      setPerson({ ...person, role: e.target.value })
                    }
                  >
                    <option value="operator">Operator</option>
                    <option value="supervisor">Supervisor</option>
                    <option value="admin">Administrator</option>
                  </select>
                </label>
                <label>
                  New PIN (blank keeps existing PIN)
                  <input
                    type="password"
                    inputMode="numeric"
                    autoComplete="new-password"
                    value={person.pin}
                    onChange={(e) =>
                      setPerson({ ...person, pin: e.target.value })
                    }
                  />
                </label>
                <label className="check-label">
                  <input
                    type="checkbox"
                    checked={person.enabled}
                    onChange={(e) =>
                      setPerson({ ...person, enabled: e.target.checked })
                    }
                  />
                  Account enabled
                </label>
                <button
                  disabled={
                    reason.trim().length < 3 || !person.operator || !person.name
                  }
                  onClick={() =>
                    command("account", {
                      ...person,
                      ...(person.pin ? {} : { pin: undefined }),
                    })
                  }
                >
                  Save account
                </button>
                <p className="hint">
                  PIN and permission changes revoke existing sessions. Transfer
                  active work before disabling an operator. At least one
                  administrator must remain enabled.
                </p>
              </fieldset>
            </>
          )}
          {section === "history" && (
            <div className="panel">
              <h3>Recent administrative changes</h3>
              {state.audit.length ? (
                state.audit.map((e) => (
                  <details className="admin-audit" key={e.id}>
                    <summary>
                      {new Date(e.at).toLocaleString()} · {e.actor} ·{" "}
                      {e.action.replace("admin:", "").replaceAll("-", " ")}
                    </summary>
                    <pre>{JSON.stringify(e.detail, null, 2)}</pre>
                  </details>
                ))
              ) : (
                <p>No administrative changes yet.</p>
              )}
            </div>
          )}
        </>
      )}
    </section>
  );
}
