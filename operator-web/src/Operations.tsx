import React, { useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { Session } from "./types";
import type { InventoryState } from "./inventory-types";

// Persisted workflow documents also carry versioned audit evidence. Only the
// operational fields are rendered here; the complete packet is downloadable.
type Doc = Record<string, any>;
type Pending = { path: string; body: Doc; key: string };
type Props = { session: Session; online: boolean; onAuthLost: () => void; onLock: (locked: boolean) => void };
const names: Record<string, string> = { board: "Order decisions", sales: "Sales intake", approvals: "Approval inbox", runs: "Planning history", close: "Daily close" };
const kinds: Record<string, string> = { allocation: "Reserve stock or future supply", transfer: "Reassign a commitment", allocation_change: "Change or release a hold", adjustment: "Correct an inventory count", early_release: "Request early shipment" };
const stamp = (v: string | undefined) => v ? new Date(v).toLocaleString() : "—";
const words = (v: unknown) => String(v ?? "—").replaceAll("_", " ");
const val = (d: FormData, k: string) => String(d.get(k) || "").trim();
const utc = (v: string) => v ? new Date(v).toISOString() : null;
const local = (v?: string) => v ? new Date(new Date(v).getTime() - new Date(v).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : "";

function Field({ label, name, value, type = "text", required = true, children }: { label: string; name: string; value?: string | number; type?: string; required?: boolean; children?: React.ReactNode }) {
  return <label>{label}{children || <input name={name} defaultValue={value ?? ""} type={type} required={required} maxLength={type === "text" ? 1000 : undefined} step={type === "number" ? "any" : undefined} />}</label>;
}
function Reason({ value }: { value?: string }) { return <Field label="Reason / supporting reference" name="reason" value={value} />; }
function Badge({ value }: { value: string }) { return <span className={"ops-badge " + value}>{words(value)}</span>; }
function History({ record }: { record: Doc }) {
  return <details><summary>Request history ({record.history?.length || 0})</summary><ol className="ops-timeline">{(record.history || []).map((h: Doc, i: number) => <li key={i}><strong>{words(h.action)}</strong> · {h.actor} · {stamp(h.at)}<p>{h.reason}</p></li>)}</ol></details>;
}
function RevisionActions({ record, prefix, canReview, user, locked, send }: { record: Doc; prefix: "sales" | "request"; canReview: boolean; user: string; locked: boolean; send: (path: string, body: Doc) => Promise<void> }) {
  const mine = record.requester === user;
  return <form className="ops-action-form" onSubmit={e => {
    e.preventDefault(); const form = new FormData(e.currentTarget); const action = (e.nativeEvent as SubmitEvent).submitter?.getAttribute("value");
    if (action) void send("/workflow/" + action, { id: record.id, version: record.version, reason: val(form, "reason") });
  }}><fieldset disabled={locked}><Reason /><div className="ops-actions">
    {record.status === "draft" && mine && <button value={prefix + "-submit"}>Submit for {prefix === "sales" ? "VISUAL entry" : "review"}</button>}
    {mine && ["draft", "submitted", "awaiting_visual_order"].includes(record.status) && <button className="secondary" value={prefix + "-cancel"}>Cancel request</button>}
    {prefix === "request" && record.status === "submitted" && canReview && !mine && <><button value="request-approve">Approve exact terms</button><button className="danger" value="request-reject">Reject</button></>}
    {prefix === "request" && record.status === "submitted" && mine && <p>A different authorized person must review this request.</p>}
  </div></fieldset></form>;
}

function SalesForm({ current, site, locked, send, done }: { current?: Doc; site: string; locked: boolean; send: (p: string, b: Doc) => Promise<void>; done: () => void }) {
  const [lines, setLines] = useState<Doc[]>(current?.lines || [{ id: "1", part: "", quantity: "1", uom: "EA" }]);
  return <form onSubmit={e => {
    e.preventDefault(); const d = new FormData(e.currentTarget); const destination = Object.fromEntries(["name", "address1", "address2", "address3", "city", "state", "postal", "country"].map(k => [k, val(d, "destination_" + k)]));
    void send("/workflow/" + (current ? "sales-update" : "sales-create"), { ...(current ? { id: current.id, version: current.version } : {}), customer: val(d, "customer"), site: val(d, "site"), customer_reference: val(d, "reference"), need_by: val(d, "need_by"), destination, lines, reason: val(d, "reason") });
  }}><fieldset disabled={locked}><div className="inventory-form-grid">
    <Field label="Customer ID" name="customer" value={current?.customer} /><Field label="Shipping site" name="site" value={current?.site || site} />
    <Field label="Customer PO / reference" name="reference" value={current?.customer_reference} /><Field label="Needed by" name="need_by" type="date" value={current?.need_by} required={false} />
    {[["name", "Ship-to name"], ["address1", "Address"], ["address2", "Address line 2"], ["address3", "Address line 3"], ["city", "City"], ["state", "State / region"], ["postal", "Postal code"], ["country", "Country"]].map(([k, label]) => <Field key={k} label={label} name={"destination_" + k} value={current?.destination?.[k] || (k === "country" ? "US" : "")} required={!["address2", "address3", "state"].includes(k)} />)}
  </div><h4>Requested lines</h4>{lines.map((line, i) => <div className="ops-line" key={line.id}>
    <span>Line {line.id}</span>{["part", "quantity", "uom"].map(k => <label key={k}>{k === "part" ? "Part" : k === "uom" ? "Stocking unit" : "Quantity"}<input aria-label={`Line ${line.id} ${k}`} required value={line[k]} onChange={e => setLines(lines.map((l, n) => n === i ? { ...l, [k]: e.target.value } : l))} /></label>)}
    <button type="button" className="ghost" disabled={lines.length === 1} onClick={() => setLines(lines.filter((_, n) => n !== i))}>Remove</button>
  </div>)}<button type="button" className="secondary" onClick={() => setLines([...lines, { id: String(Math.max(...lines.map(l => Number(l.id))) + 1), part: "", quantity: "1", uom: "EA" }])}>Add line</button>
    <Reason value={current?.reason} /><div className="ops-actions"><button>{current ? "Save revised draft" : "Save sales draft"}</button><button type="button" className="ghost" onClick={done}>Close editor</button></div>
    <p className="muted">This records a sales request. An existing VISUAL order must be linked before it can become shipping demand.</p>
  </fieldset></form>;
}

function RequestForm({ current, inventory, sales, locked, send, done }: { current?: Doc; inventory: InventoryState | null; sales: Doc[]; locked: boolean; send: (p: string, b: Doc) => Promise<void>; done: () => void }) {
  const [kind, setKind] = useState(current?.kind || "allocation");
  const [source, setSource] = useState(current?.terms?.source || "stock");
  const [allocationId, setAllocationId] = useState(current?.terms?.id || "");
  const terms = current?.terms || {};
  const allocation = inventory?.allocations.find(a => a.id === allocationId);
  return <form onSubmit={e => {
    e.preventDefault(); const d = new FormData(e.currentTarget); const reason = val(d, "reason"); let t: Doc = { reason };
    if (["allocation", "transfer"].includes(kind)) t = { ...t, customer: val(d, "customer"), quantity: val(d, "quantity"), order: val(d, "order"), order_line: val(d, "order_line"), hold: d.get("hold") === "on", priority: Number(val(d, "priority") || 50), need_by: val(d, "need_by"), review_at: utc(val(d, "review_at")), request_reference: val(d, "request_reference") };
    if (["allocation", "adjustment"].includes(kind)) t = { ...t, part: val(d, "part"), uom: val(d, "uom") };
    if (kind === "allocation") t = { ...t, source, ...(source === "work_order" ? { source_id: val(d, "source_id") } : {}) };
    if (["transfer", "allocation_change"].includes(kind)) t = { ...t, id: allocationId, version: allocation?.version };
    if (kind === "allocation_change") t = { ...t, ...(val(d, "change") === "release" ? { status: "released" } : { hold: val(d, "change") === "hold" }) };
    if (kind === "adjustment") t = { ...t, quantity: val(d, "quantity"), serials: val(d, "serials").split(/[\s,]+/).filter(Boolean) };
    if (kind === "early_release") t = { ...t, order: val(d, "order"), expires_at: utc(val(d, "expires_at")) };
    void send("/workflow/" + (current ? "request-update" : "request-create"), { ...(current ? { id: current.id, version: current.version } : {}), kind, terms: t, sales_request_id: kind === "allocation" ? val(d, "sales_request_id") : "", reason });
  }}><fieldset disabled={locked}><div className="inventory-form-grid">
    <Field label="Request type" name="kind"><select value={kind} onChange={e => setKind(e.target.value)}>{Object.entries(kinds).map(([k, n]) => <option value={k} key={k}>{n}</option>)}</select></Field>
    {kind === "allocation" && <><Field label="Supply source" name="source"><select value={source} onChange={e => setSource(e.target.value)}><option value="stock">On-hand stock</option><option value="work_order">Future work-order supply</option></select></Field>
      {source === "work_order" && <Field label="Work order" name="source_id"><select name="source_id" defaultValue={terms.source_id || ""} required><option value="">Choose work order</option>{inventory?.supplies.filter(s => s.status === "open").map(s => <option key={s.id} value={s.id}>{s.work_order} · {s.part} · {s.quantity} {s.uom}</option>)}</select></Field>}
      <Field label="Linked sales request (optional)" name="sales_request_id"><select name="sales_request_id" defaultValue={current?.sales_request_id || ""}><option value="">No sales draft link</option>{sales.filter(s => ["bound", "awaiting_visual_order"].includes(s.status)).map(s => <option value={s.id} key={s.id}>{s.customer} · {s.customer_reference}</option>)}</select></Field></>}
    {["transfer", "allocation_change"].includes(kind) && <Field label="Existing commitment" name="allocation_id"><select required value={allocationId} onChange={e => setAllocationId(e.target.value)}><option value="">Choose commitment</option>{inventory?.allocations.filter(a => a.status === "active").map(a => <option key={a.id} value={a.id}>{a.customer} · {a.part} · {a.quantity} {a.uom} · v{a.version}</option>)}</select></Field>}
    {["allocation", "adjustment"].includes(kind) && <><Field label="Part" name="part" value={terms.part} /><Field label="Stocking unit" name="uom" value={terms.uom || "EA"} /></>}
    {["allocation", "transfer", "adjustment"].includes(kind) && <Field label={kind === "adjustment" ? "Signed count correction (+ / −)" : "Quantity"} name="quantity" value={terms.quantity || "1"} />}
    {["allocation", "transfer"].includes(kind) && <><Field label="Customer ID" name="customer" value={terms.customer} /><Field label="VISUAL order (optional)" name="order" value={terms.order} required={false} /><Field label="Order line (optional)" name="order_line" value={terms.order_line} required={false} /><Field label="Priority (0 highest, 100 lowest)" name="priority" type="number" value={terms.priority ?? 50} /><Field label="Needed by" name="need_by" type="date" value={terms.need_by} required={false} /><Field label="Hold review time" name="review_at" type="datetime-local" value={local(terms.review_at)} required={false} /><Field label="Request reference" name="request_reference" value={terms.request_reference} required={false} /><label className="ops-check"><input type="checkbox" name="hold" defaultChecked={terms.hold ?? true} />Keep committed stock on hold</label></>}
    {kind === "allocation_change" && <Field label="Requested change" name="change"><select name="change" defaultValue={terms.status === "released" ? "release" : terms.hold === false ? "unhold" : "hold"}><option value="hold">Hold for current owner</option><option value="unhold">Remove hold; keep ownership</option><option value="release">Free unconsumed commitment</option></select></Field>}
    {kind === "adjustment" && <label>Serials, if applicable<textarea name="serials" defaultValue={(terms.serials || []).join("\n")} rows={3} /></label>}
    {kind === "early_release" && <><Field label="VISUAL order" name="order" value={terms.order} /><Field label="Authorization expires" name="expires_at" type="datetime-local" value={local(terms.expires_at)} /><p>An administrator must review early release. Hard shipping checks still apply.</p></>}
  </div><Reason value={terms.reason} /><div className="ops-actions"><button>{current ? "Save revised request" : "Save request draft"}</button><button type="button" className="ghost" onClick={done}>Close editor</button></div><p className="muted">Drafts and pending requests do not reserve stock. Approval rechecks the exact terms and current supply.</p></fieldset></form>;
}

export function Operations({ session, online, onAuthLost, onLock }: Props) {
  const [section, setSection] = useState("board"), [query, setQuery] = useState(""), [filter, setFilter] = useState("all");
  const [board, setBoard] = useState<Doc | null>(null), [workflow, setWorkflow] = useState<Doc | null>(null), [runs, setRuns] = useState<Doc | null>(null), [closes, setCloses] = useState<Doc | null>(null), [inventory, setInventory] = useState<InventoryState | null>(null);
  const [error, setError] = useState(""), [notice, setNotice] = useState(""), [busy, setBusy] = useState(false), [pending, setPending] = useState<Pending | null>(null), [stale, setStale] = useState(false);
  const [editor, setEditor] = useState<Doc | null>(null), [packet, setPacket] = useState<Doc | null>(null), [run, setRun] = useState<Doc | null>(null);
  const reviewer = ["supervisor", "admin"].includes(session.operator.role);
  const locked = busy || !!pending || !online || stale;
  const sales: Doc[] = workflow?.sales_requests || [], requests: Doc[] = workflow?.approval_requests || [];
  useEffect(() => { onLock(busy || !!pending); }, [busy, pending, onLock]);
  useEffect(() => () => onLock(false), [onLock]);
  useEffect(() => { if (!pending) return; const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; }; window.addEventListener("beforeunload", warn); return () => window.removeEventListener("beforeunload", warn); }, [pending]);
  async function refresh() {
    const paths = ["/planning/board", "/workflow", "/planning/runs", "/daily-close", "/inventory"];
    const setters = [setBoard, setWorkflow, setRuns, setCloses, setInventory] as ((v: any) => void)[];
    const results = await Promise.allSettled(paths.map(p => api(p)));
    let failed = false;
    results.forEach((r, i) => { if (r.status === "fulfilled") setters[i](r.value); else { failed = true; setError(r.reason.message); if (r.reason instanceof ApiError && r.reason.status === 401) onAuthLost(); } });
    setStale(failed);
    return !failed;
  }
  useEffect(() => { void refresh(); }, []);
  async function send(path: string, body: Doc, key: string = crypto.randomUUID()) {
    setBusy(true); setError(""); setNotice("");
    try {
      const result = await api<Doc>(path, body, session.csrf, key);
      setPending(null); setEditor(null);
      if (path.startsWith("/daily-close") && result.packet) setPacket(result);
      await refresh(); setNotice("Saved. The recorded result and current versions are shown below.");
    } catch (e) {
      const err = e as Error; setError(err.message);
      if (!(e instanceof ApiError) || e.status >= 500) setPending({ path, body, key });
      else { setPending(null); await refresh(); }
      if (e instanceof ApiError && e.status === 401) onAuthLost();
    } finally { setBusy(false); }
  }
  async function inspect(path: string, setter: (d: Doc) => void) { setBusy(true); setError(""); try { setter(await api<Doc>(path)); } catch (e) { setError((e as Error).message); } finally { setBusy(false); } }
  const matches = (r: Doc) => JSON.stringify([r.order, r.customer, r.customer_id, r.customer_reference, r.status, r.label, r.requester, r.terms]).toLowerCase().includes(query.toLowerCase());
  return <section className="operations">
    <div className="inventory-tabs" role="tablist" aria-label="Operating workflows">{Object.entries(names).map(([k, n]) => <button key={k} role="tab" aria-selected={section === k} className={section === k ? "active" : "secondary"} disabled={busy || !!pending} onClick={() => { setSection(k); setFilter("all"); setEditor(null); }}>{n}</button>)}</div>
    <div className="ops-toolbar"><label>Search<input aria-label="Search operations" value={query} onChange={e => setQuery(e.target.value)} placeholder="Order, customer, request or reason" /></label><button className="secondary" disabled={busy || !!pending || !online} onClick={() => { setError(""); void refresh(); }}>Refresh workspace</button></div>
    {error && <div className="alert" role="alert">{error}</div>}{notice && <div className="notice" role="status">{notice}</div>}
    {stale && <div className="alert">Some workspace data could not be refreshed. Actions are paused until refresh succeeds.</div>}
    {pending && <div className="alert">The result is uncertain. Keep this workspace open and recover the same request before making another change.<button disabled={busy || !online} onClick={() => void send(pending.path, pending.body, pending.key)}>Recover confirmation</button></div>}
    {section === "board" && <>
      <div className="section-title"><div><h2>Every order, with its next action</h2><p className="muted">Ready suggestions are tentative until checkout. Existing commitments remain protected.</p></div><Badge value={board?.source?.available ? "source_current" : "source_unavailable"} /></div>
      <p>Last planning decision: {stamp(board?.latest_run?.finished_at)} · {board?.pending_triggers?.length || 0} pending planning triggers</p>
      {reviewer && <form className="ops-action-form" onSubmit={e => { e.preventDefault(); void send("/planning/run", { reason: val(new FormData(e.currentTarget), "reason") }); }}><fieldset disabled={locked || session.mode === "shadow"}><Reason /><button>Refresh orders and run planning</button></fieldset></form>}
      <label>Decision filter<select value={filter} onChange={e => setFilter(e.target.value)}><option value="all">All decisions</option>{Array.from(new Set<string>((board?.orders || []).map((o: Doc) => o.status))).map(s => <option key={s} value={s}>{words(s)}</option>)}</select></label>
      {!(board?.orders || []).length && <div className="empty"><h3>No mirrored orders yet</h3><p>The allocation worker imports VISUAL orders. A supervisor can also refresh orders and run planning.</p></div>}
      <div className="ops-cards">{(board?.orders || []).filter((o: Doc) => matches(o) && (filter === "all" || o.status === filter)).map((o: Doc) => <article className="ops-card" key={o.id}><div className="section-title"><h3>{o.order} · {o.customer}</h3><Badge value={o.status} /></div><p><strong>{o.label}</strong></p><p>{o.next_action}</p><p className="muted">Next action owner: {o.owner} · Source observed {stamp(o.observed_at)}</p><details><summary>Demand, commitments and decision evidence</summary><div className="ops-table-wrap"><table><thead><tr><th>Line</th><th>Part</th><th>Open demand</th></tr></thead><tbody>{o.lines.map((l: Doc, i: number) => <tr key={i}><td>{l.order_line}</td><td>{l.part}</td><td>{l.quantity} {l.uom}</td></tr>)}</tbody></table></div><p>{o.commitments.length} commitments · {o.pending_approvals.length} pending requests</p>{o.commitments.map((a: Doc) => <p key={a.id}>{a.quantity} {a.uom} · {a.part} · {words(a.source)} · {a.hold ? "Held for owner" : "Owner may use"}</p>)}{o.decision && <p>Rule: {words(o.decision.reason_code)} · {o.decision.label} · Decision at {stamp(o.decision_at)}</p>}{o.conflicts.map((c: unknown, i: number) => <p key={i} className="alert">{typeof c === "string" ? c : JSON.stringify(c)}</p>)}</details></article>)}</div>
    </>}
    {section === "sales" && <>
      <div className="section-title"><div><h2>Sales intake</h2><p>Record the request, then link the commercial order created in VISUAL.</p></div><button disabled={locked} onClick={() => setEditor({})}>New sales request</button></div>
      {editor && <article className="ops-card"><h3>{editor.id ? "Revise sales draft" : "New sales draft"}</h3><SalesForm key={editor.id + ":" + editor.version} current={editor.id ? editor : undefined} site={inventory?.config.site || ""} locked={locked} send={send} done={() => setEditor(null)} /></article>}
      {!sales.length && <div className="empty">No sales requests recorded.</div>}
      <div className="ops-cards">{sales.filter(matches).map(s => <article className="ops-card" key={s.id}><div className="section-title"><h3>{s.customer} · {s.customer_reference}</h3><Badge value={s.status} /></div><p>Requested by {s.requester_name} · Need by {s.need_by || "not specified"} · {s.order ? "VISUAL " + s.order : "No VISUAL order bound"}</p><p>{s.destination.name}, {s.destination.address1}, {s.destination.city} {s.destination.postal}</p>{s.lines.map((l: Doc) => <p key={l.id}>Line {l.id}: <strong>{l.part}</strong> · {l.quantity} {l.uom}</p>)}{s.duplicate_references?.length > 0 && <div className="alert">Duplicate customer reference needs independent review before binding.</div>}
        {["draft", "awaiting_visual_order"].includes(s.status) && (s.requester === session.operator.id || session.operator.role === "admin") && <button className="secondary" disabled={locked} onClick={() => setEditor(s)}>Revise draft</button>}
        <RevisionActions record={s} prefix="sales" canReview={reviewer} user={session.operator.id} locked={locked} send={send} />
        {s.status === "awaiting_visual_order" && reviewer && <details><summary>Link an existing VISUAL order</summary><form onSubmit={e => { e.preventDefault(); const d = new FormData(e.currentTarget); void send("/workflow/sales-bind", { id: s.id, version: s.version, order: val(d, "order"), line_bindings: s.lines.map((l: Doc) => ({ request_line: l.id, order_line: val(d, "line_" + l.id) })), duplicate_reference_review: val(d, "duplicate_review"), reason: val(d, "reason") }); }}><fieldset disabled={locked}><Field label="VISUAL order ID" name="order" />{s.lines.map((l: Doc) => <Field key={l.id} label={`VISUAL line for request line ${l.id} (${l.part})`} name={"line_" + l.id} />)}<Field label="Duplicate reference review (if flagged)" name="duplicate_review" required={s.duplicate_references?.length > 0} /><Reason /><button>Validate and bind order</button></fieldset></form></details>}<History record={s} /></article>)}</div>
    </>}
    {section === "approvals" && <>
      <div className="section-title"><div><h2>Requests and independent approvals</h2><p>Pending requests reserve nothing. The approver rechecks current quantities and exact terms.</p></div><button disabled={locked} onClick={() => setEditor({})}>New approval request</button></div>
      {editor && <article className="ops-card"><RequestForm key={editor.id + ":" + editor.version} current={editor.id ? editor : undefined} inventory={inventory} sales={sales} locked={locked} send={send} done={() => setEditor(null)} /></article>}
      <label>Request status<select value={filter} onChange={e => setFilter(e.target.value)}>{["all", "draft", "submitted", "approved", "rejected", "cancelled"].map(s => <option key={s} value={s}>{words(s)}</option>)}</select></label>
      {!requests.length && <div className="empty">No approval requests yet.</div>}
      <div className="ops-cards">{requests.filter(r => matches(r) && (filter === "all" || r.status === filter)).map(r => <article className="ops-card" key={r.id}><div className="section-title"><h3>{kinds[r.kind]}</h3><Badge value={r.status} /></div><p>Requested by {r.requester_name} · {stamp(r.created_at)} · Revision {r.version}</p><dl className="ops-facts">{Object.entries(r.terms).filter(([k]) => k !== "reason").map(([k, v]) => <React.Fragment key={k}><dt>{words(k)}</dt><dd>{typeof v === "boolean" ? (v ? "Yes" : "No") : Array.isArray(v) ? v.join(", ") : String(v ?? "—")}</dd></React.Fragment>)}</dl><p>{r.terms.reason}</p>{r.progress && <p><strong>Commitment:</strong> {words(r.progress.status)} · {r.progress.quantity} promised · {r.progress.fulfilled} {r.progress.source === "work_order" ? "promoted to stock" : "shipped"} · {r.progress.hold ? "held" : "not held"}</p>}
        {["draft", "submitted"].includes(r.status) && (r.requester === session.operator.id || session.operator.role === "admin") && <button className="secondary" disabled={locked} onClick={() => setEditor(r)}>Revise requested terms</button>}
        <RevisionActions record={r} prefix="request" canReview={reviewer && (r.kind !== "early_release" || session.operator.role === "admin")} user={session.operator.id} locked={locked} send={send} /><History record={r} /></article>)}</div>
    </>}
    {section === "runs" && <><h2>Planning history</h2><p>Committed attempts retain inputs, decisions and allocation changes. Preview reads do not create a run.</p><div className="ops-table-wrap"><table><thead><tr><th>Started</th><th>Result</th><th>Actor</th><th>Decisions</th><th>Review</th></tr></thead><tbody>{(runs?.runs || []).map((r: Doc) => <tr key={r.id}><td>{stamp(r.started_at)}</td><td><Badge value={r.status} /></td><td>{r.actor}</td><td>{r.decision_count ?? r.decisions?.length ?? "—"}</td><td><button className="secondary" disabled={locked} onClick={() => void inspect("/planning/runs/" + r.id, setRun)}>Open run</button></td></tr>)}</tbody></table></div>
      {run && <article className="ops-card"><h3>Run at {stamp(run.started_at)}</h3><Badge value={run.status} />{run.error && <p className="alert">{run.error.message}</p>}<p>{run.allocation_changes?.length || 0} commitment changes · {run.triggers?.length || 0} trigger groups</p><a href={"/api/v1/planning/runs/" + run.id} download={"planning-" + run.id + ".json"}>Download complete run evidence</a><div className="ops-table-wrap"><table><thead><tr><th>Order</th><th>Decision</th><th>Reason at that time</th></tr></thead><tbody>{(run.decisions || []).map((d: Doc, i: number) => <tr key={i}><td>{d.order_id}</td><td>{words(d.decision)}</td><td>{d.label}</td></tr>)}</tbody></table></div>{(run.allocation_changes || []).map((a: Doc, i: number) => <p key={i}>{words(a.action || "commitment change")} · {a.id || a.allocation_id} · {a.before?.quantity || "0"} → {a.after?.quantity || "0"}</p>)}</article>}
    </>}
    {section === "close" && <>
      <h2>Daily close</h2><p>Compare the ledger, VISUAL movements and shipment documents at a coordinated boundary. A separate supervisor signs the reviewed packet.</p>
      {reviewer && <details className="ops-card"><summary>Match a reviewed VISUAL correction to the local ledger</summary><p>Use exact source transaction and adjustment entry IDs from the exception evidence. Matching records does not change stock. Prepare a new close afterward for independent review.</p><form onSubmit={e => { e.preventDefault(); const d = new FormData(e.currentTarget); void send("/daily-close/correspondence", { source_ids: val(d, "source_ids").split(/[\s,]+/).filter(Boolean), ledger_ids: val(d, "ledger_ids").split(/[\s,]+/).filter(Boolean), metadata_only: d.get("metadata_only") === "on", reference: val(d, "reference"), reason: val(d, "reason") }); }}><fieldset disabled={locked}><Field label="VISUAL transaction IDs" name="source_ids" /><Field label="Local adjustment entry IDs" name="ledger_ids" required={false} /><label className="ops-check"><input type="checkbox" name="metadata_only" />Metadata correction only, with no quantity movement</label><Field label="Supporting correction reference" name="reference" /><Reason /><button>Record exact correspondence</button></fieldset></form>{(closes?.correspondences || []).map((c: Doc) => <div className="ops-exception" key={c.id}><p>{c.reference} · {words(c.status)} · {c.created_by}</p><p>VISUAL {c.source_ids.join(", ")} ↔ {c.metadata_only ? "metadata review" : c.ledger_ids.join(", ")}</p>{c.status === "proposed" && <form onSubmit={e => { e.preventDefault(); void send("/daily-close/correspondence-cancel", { id: c.id, reason: val(new FormData(e.currentTarget), "reason") }); }}><fieldset disabled={locked}><Reason /><button className="secondary">Cancel unused correspondence</button></fieldset></form>}</div>)}</details>}
      {reviewer && <form className="ops-card" onSubmit={e => { e.preventDefault(); const d = new FormData(e.currentTarget); void send("/daily-close/prepare", { reason: val(d, "reason"), pause_confirmed: d.get("paused") === "on" }); }}><fieldset disabled={locked}><label className="ops-check"><input required type="checkbox" name="paused" />Warehouse movements are paused for the actual close capture and review</label><Reason /><button>Prepare current close</button></fieldset></form>}
      <div className="ops-table-wrap"><table><thead><tr><th>Business date / actual cutoff</th><th>Evidence</th><th>Approval</th><th>Exceptions</th><th>Review</th></tr></thead><tbody>{(closes?.closes || []).map((c: Doc) => <tr key={c.id}><td>{c.business_date}<br /><small>{stamp(c.cutoff)} · {c.timezone}</small>{c.impacts?.length > 0 && <p className="alert">{c.impacts.length} later source changes affect this close</p>}</td><td><Badge value={c.evidence_status} /></td><td><Badge value={c.approval_status} /></td><td>{c.exception_count}</td><td><button className="secondary" disabled={locked} onClick={() => void inspect("/daily-close/" + c.id + "/packet", setPacket)}>Review packet</button></td></tr>)}</tbody></table></div>
      {packet && <article className="ops-card"><div className="section-title"><h3>Close for {packet.business_date}</h3><div><Badge value={packet.evidence_status} /> <Badge value={packet.approval_status} /></div></div><p>Prepared by {packet.prepared_by} · {stamp(packet.prepared_at)} · Actual source boundary {stamp(packet.cutoff)}</p>{packet.signed_by && <p>Signed by {packet.signed_by} · {stamp(packet.signed_at)}</p>}<a href={"/api/v1/daily-close/" + packet.id + "/packet"}>Download complete evidence packet</a><p>{packet.evidence_status === "balanced" ? "The prepared evidence agrees. Sign-off rechecks material evidence and source completeness." : packet.evidence_status === "incomplete" ? "Required evidence is missing or unconfirmed. Sign-off is blocked; restore evidence and prepare again." : "Differences remain. Assign and document every exception before an explicit exception sign-off."}</p>
        <CloseBalances packet={packet.packet} />
        <div className="ops-cards">{(packet.exceptions || []).map((x: Doc) => <div className="ops-exception" key={x.id}><h4>{words(x.kind)}</h4><p>{x.message}</p><p>Reference: {x.reference} · Owner: {x.owner || "Unassigned"} · Due: {stamp(x.due_at)}</p><Badge value={x.disposition} /><details><summary>Exact exception evidence</summary><pre className="ops-evidence">{JSON.stringify(x.evidence, null, 2)}</pre></details>{reviewer && packet.approval_status === "draft" && <details><summary>Assign and review exception</summary><form onSubmit={e => { e.preventDefault(); const d = new FormData(e.currentTarget); void send("/daily-close/exception", { id: packet.id, version: packet.version, exception_id: x.id, owner: val(d, "owner"), due_at: utc(val(d, "due_at")), disposition: val(d, "disposition"), resolution_reference: val(d, "reference"), reason: val(d, "reason") }); }}><fieldset disabled={locked}><Field label="Responsible user ID" name="owner" value={x.owner} /><Field label="Follow-up due" name="due_at" type="datetime-local" value={local(x.due_at)} /><Field label="Disposition" name="disposition"><select name="disposition" defaultValue={x.disposition}><option value="open">Open for investigation</option><option value="accepted_exception">Accept as a visible exception</option></select></Field><Field label="Evidence / correction reference" name="reference" value={x.resolution_reference} /><Reason /><button>Save exception review</button></fieldset></form></details>}</div>)}</div>
        {reviewer && packet.approval_status === "draft" && <form className="ops-action-form" onSubmit={e => { e.preventDefault(); const d = new FormData(e.currentTarget); void send("/daily-close/sign", { id: packet.id, version: packet.version, allow_exceptions: d.get("accept") === "on", reason: val(d, "reason") }); }}><fieldset disabled={locked || packet.prepared_by === session.operator.id || packet.evidence_status === "incomplete" || packet.packet?.applied_correspondences?.some((c: Doc) => c.created_by === session.operator.id)}><Reason />{packet.exceptions?.length > 0 && <label className="ops-check"><input type="checkbox" name="accept" required />Sign with the documented exceptions; this is not a balanced close</label>}<button>Sign reviewed revision</button></fieldset>{packet.prepared_by === session.operator.id && <p>A different supervisor must sign the close you prepared.</p>}{packet.packet?.applied_correspondences?.some((c: Doc) => c.created_by === session.operator.id) && <p>A different supervisor must review and sign your movement correspondence.</p>}</form>}
        {reviewer && packet.approval_status === "signed" && <details><summary>Prepare a superseding close</summary><form onSubmit={e => { e.preventDefault(); const d = new FormData(e.currentTarget); void send("/daily-close/supersede", { id: packet.id, version: packet.version, pause_confirmed: d.get("paused") === "on", reason: val(d, "reason") }); }}><fieldset disabled={locked}><label className="ops-check"><input type="checkbox" name="paused" required />Warehouse movements are paused for a new capture</label><Reason /><button>Prepare retained correction</button></fieldset></form></details>}
      </article>}
    </>}
  </section>;
}

function CloseBalances({ packet }: { packet: Doc }) {
  const identity = (r: Doc) => [r.part, r.uom, r.serial || ""].join("\u0000");
  const equation = packet?.ledger_equation || {};
  const opening = new Map<string, Doc>((equation.opening || []).map((r: Doc) => [identity(r), r]));
  const delta = new Map<string, Doc>((equation.delta || []).map((r: Doc) => [identity(r), r]));
  const closing = new Map<string, Doc>((equation.closing || []).map((r: Doc) => [identity(r), r]));
  const keys = Array.from(new Set([...opening.keys(), ...delta.keys(), ...closing.keys()])).sort();
  return <details open><summary>Balance equation and transaction coverage</summary><p>{packet?.movement_links?.length || 0} movement correspondences · {packet?.shipment_evidence?.length || 0} shipment document checks · {packet?.carry_forward?.length || 0} work items carried forward</p><div className="ops-table-wrap"><table><thead><tr><th>Part / serial</th><th>Unit</th><th>Opening</th><th>Net receipts, adjustments and shipments</th><th>Closing</th></tr></thead><tbody>{keys.map(k => { const r = closing.get(k) || opening.get(k) || delta.get(k)!; return <tr key={k}><td>{r.part}{r.serial ? " · " + r.serial : ""}</td><td>{r.uom}</td><td>{opening.get(k)?.quantity || "0"}</td><td>{delta.get(k)?.quantity || "0"}</td><td>{closing.get(k)?.quantity || "0"}</td></tr>; })}</tbody></table></div></details>;
}
