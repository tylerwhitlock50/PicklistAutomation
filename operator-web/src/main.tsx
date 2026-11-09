import React, { useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import JsBarcode from "jsbarcode";
import { api, ApiError, packedQuantity } from "./api";
import type { Order, Work, Session, Line } from "./types";
import "./style.css";
import { Admin } from "./Admin";
import { Inventory } from "./Inventory";
import { Operations } from "./Operations";

function Barcode({ value }: { value: string }) {
  const ref = useRef<SVGSVGElement>(null);
  useEffect(() => {
    if (ref.current)
      JsBarcode(ref.current, value, {
        format: "CODE128",
        height: 58,
        width: 2,
        margin: 12,
        fontSize: 16,
      });
  }, [value]);
  return <svg ref={ref} role="img" aria-label={"Barcode " + value} />;
}
const stateName = (s: string) =>
  ({
    picking: "Picking",
    ready_for_pack: "Ready for packing",
    packing: "Packing",
    putaway: "Return to stock",
    released: "Released for shipping",
    cancelled: "Cancelled",
  })[s] || s;

function App() {
  const [adminLocked, setAdminLocked] = useState(false);
  const [session, setSession] = useState<Session | null>(null),
    [loading, setLoading] = useState(true);
  const [orders, setOrders] = useState<Order[]>([]),
    [works, setWorks] = useState<Work[]>([]),
    [work, setWork] = useState<Work | null>(null);
  const [tab, setTab] = useState("orders"),
    [error, setError] = useState(""),
    [notice, setNotice] = useState(""),
    [busy, setBusy] = useState(false);
  const [online, setOnline] = useState(navigator.onLine),
    [lineId, setLineId] = useState(""),
    [location, setLocation] = useState("");
  const [barcode, setBarcode] = useState(""),
    [amount, setAmount] = useState("1"),
    [packageId, setPackageId] = useState(""),
    [cart, setCart] = useState("");
  const [station, setStation] = useState("PACK-01"),
    [packlist, setPacklist] = useState(""),
    [tracking, setTracking] = useState(""),
    [reason, setReason] = useState("");
  const [operator, setOperator] = useState(""),
    [pin, setPin] = useState(""),
    [oldSerial, setOldSerial] = useState(""),
    [replacement, setReplacement] = useState("");
  const [training, setTraining] = useState<{
    packlist: string;
    labels: { package: string; tracking: string }[];
  } | null>(null);
  const [printId, setPrintId] = useState("");
  const [pending, setPending] = useState<{
    path: string;
    body: unknown;
    id: string;
  } | null>(null);
  const scanRef = useRef<HTMLInputElement>(null);
  const line = work?.lines.find((l) => l.id === lineId);
  const locked = busy || !online || session?.mode === "shadow" || !!pending;

  async function refresh() {
    const [o, w] = await Promise.all([
      api<{ orders: Order[] }>("/orders"),
      api<{ fulfillments: Work[] }>("/fulfillments"),
    ]);
    setOrders(o.orders);
    setWorks(w.fulfillments);
  }
  useEffect(() => {
    api<Session>("/auth/me")
      .then((s) => {
        setSession(s);
        if (s.operator.role === "sales") setTab("operations");
        return refresh();
      })
      .catch((e) => {
        if (!(e instanceof ApiError && e.status === 401))
          setError(String(e.message));
      })
      .finally(() => setLoading(false));
    const on = () => setOnline(true),
      off = () => setOnline(false);
    window.addEventListener("online", on);
    window.addEventListener("offline", off);
    return () => {
      window.removeEventListener("online", on);
      window.removeEventListener("offline", off);
    };
  }, []);
  async function select(w: Work) {
    setWork(await api<Work>("/fulfillments/" + w.id));
    setLineId(w.lines[0]?.id || "");
    setLocation("");
    setBarcode("");
    setPackageId("");
    setCart("");
    setPacklist(w.packlist || "");
    setTraining(null);
    setError("");
    setNotice("");
  }
  async function send(
    path: string,
    body: unknown,
    id: string = crypto.randomUUID(),
  ) {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await api<Work>(path, body, session?.csrf, id);
      setPending(null);
      if (result.id) {
        setWork(result);
        if (path === "/checkouts") setLineId(result.lines[0]?.id || "");
        if (result.state === "ready_for_pack") setCart("");
      }
      setBarcode("");
      setNotice("Confirmed and saved.");
      // A refresh failure cannot undo or disguise a successful confirmation.
      try {
        await refresh();
      } catch {
        setNotice(
          "Confirmed and saved. Queue refresh is temporarily unavailable.",
        );
      }
      requestAnimationFrame(() => scanRef.current?.focus());
    } catch (e) {
      const ex = e as Error;
      setError(ex.message);
      if (!(e instanceof ApiError) || e.status >= 500)
        setPending({ path, body, id });
      else {
        setPending(null);
        if (e.status === 401) setSession(null);
        if (work && e.status === 409)
          try {
            setWork(await api<Work>("/fulfillments/" + work.id));
          } catch {
            /* preserve visible work */
          }
      }
    } finally {
      setBusy(false);
    }
  }
  async function act(action: string, body: Record<string, unknown> = {}) {
    if (work)
      await send(`/fulfillments/${work.id}/${action}`, {
        ...body,
        version: work.version,
      });
  }
  async function signIn(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api("/auth/login", { operator, pin });
      setPin("");
      const signedIn = await api<Session>("/auth/me");
      setSession(signedIn);
      setTab(signedIn.operator.role === "sales" ? "operations" : "orders");
      await refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function scan(e: React.FormEvent) {
    e.preventDefault();
    if (!line) return;
    const action =
      work?.state === "putaway"
        ? "putaway"
        : work?.state === "packing"
          ? "pack"
          : "pick";
    void act(action, {
      line_id: line.id,
      location,
      package: packageId,
      ...(line.serialized
        ? { serial: barcode }
        : { item: barcode, quantity: amount }),
    });
  }
  function chooseLine(l: Line) {
    setLineId(l.id);
    setLocation("");
    setBarcode("");
    setAmount("1");
    requestAnimationFrame(() => scanRef.current?.focus());
  }
  async function demo() {
    if (!work) return;
    setBusy(true);
    try {
      const d = await api<{
        packlist: string;
        labels: { package: string; tracking: string }[];
      }>(`/demo/${work.id}/documents`, {}, session?.csrf);
      setTraining(d);
      setPacklist(d.packlist);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function print(value: string) {
    setPrintId(value);
    setTimeout(() => window.print(), 100);
  }

  if (loading)
    return (
      <main className="signin">
        <h1>Dispatch</h1>
        <p>Connecting to your workspace…</p>
      </main>
    );
  if (!session)
    return (
      <main className="signin">
        <div className="brand-mark">D</div>
        <p className="eyebrow">WAREHOUSE OPERATIONS</p>
        <h1>
          A clear path
          <br />
          from pick to ship.
        </h1>
        <p>Sign in to collect an order or receive a cart.</p>
        <form onSubmit={signIn}>
          <label>
            Operator ID
            <input
              autoFocus
              value={operator}
              onChange={(e) => setOperator(e.target.value)}
              autoComplete="username"
              required
            />
          </label>
          <label>
            PIN
            <input
              type="password"
              inputMode="numeric"
              value={pin}
              onChange={(e) => setPin(e.target.value)}
              autoComplete="current-password"
              required
            />
          </label>
          <button disabled={busy || !online}>Sign in →</button>
        </form>
        {error && (
          <p className="alert" role="alert">
            {error}
          </p>
        )}
      </main>
    );
  const active = works.filter(
    (w) => !["released", "cancelled"].includes(w.state),
  );
  const displayed =
    tab === "cart"
      ? works.filter((w) => w.operator === session.operator.id)
      : tab === "packing"
        ? active.filter((w) => ["ready_for_pack", "packing"].includes(w.state))
        : active.filter((w) => w.exception || w.state === "putaway");
  return (
    <>
      <div className="shell">
        <aside className="sidebar">
          <a
            className="brand"
            href="/"
            aria-disabled={busy || !!pending || adminLocked}
            onClick={(event) => {
              if (busy || pending || adminLocked) event.preventDefault();
            }}
          >
            ▧ Dispatch
          </a>
          <p className="eyebrow">PICK · PACK · SHIP</p>
          <nav>
            {[
              ...(session.operator.role !== "sales" ? [
                ["orders", "01", "Available orders"],
                ["cart", "02", "My work"],
                ["packing", "03", "Packing station"],
                ["exceptions", "04", "Exceptions"],
              ] : []),
              ["operations", "08", "Orders, approvals & close"],
              ["inventory", "05", "Inventory & allocations"],
              ...(session.operator.role === "admin"
                ? [["admin", "06", "Administration"]]
                : []),
            ].map(([id, no, name]) => (
              <button
                className={tab === id ? "nav active" : "nav"}
                key={id}
                disabled={busy || !!pending || adminLocked}
                onClick={() => {
                  setTab(id);
                  setWork(null);
                  setError("");
                }}
              >
                <span>{no}</span>
                {name}
              </button>
            ))}
            <button
              className="nav"
              hidden={session.operator.role === "sales"}
              disabled={busy || !!pending || adminLocked}
              onClick={() => {
                window.location.href = "/audit";
              }}
            >
              <span>07</span>Inventory audit
            </button>
          </nav>
          <div className="sidebar-bottom">
            <small>Signed in as</small>
            <strong>{session.operator.name}</strong>
            <span>{session.operator.role}</span>
            <button
              className="ghost"
              onClick={async () => {
                try {
                  await api("/auth/logout", {}, session.csrf);
                  setSession(null);
                  setWork(null);
                } catch (e) {
                  setError((e as Error).message);
                }
              }}
              disabled={busy || !!pending || adminLocked}
            >
              Sign out
            </button>
          </div>
        </aside>
        <main>
          <header>
            <div>
              <p className="eyebrow">FULFILLMENT WORKSPACE</p>
              <h1>
                {work
                  ? work.order
                  : tab === "orders"
                    ? "Ready to move."
                    : tab === "cart"
                      ? "Your work."
                      : tab === "packing"
                        ? "Every box, verified."
                        : tab === "inventory"
                          ? "Every unit, accounted for."
                          : tab === "operations"
                            ? "Every decision, explained."
                          : tab === "admin"
                            ? "Warehouse administration."
                            : "Resolve and continue."}
              </h1>
              <p>
                {work
                  ? `${work.customer} · Cart ${work.cart}`
                  : tab === "inventory"
                    ? "Shipping stock, future supply, and customer commitments."
                    : tab === "operations"
                      ? "Sales intake, protected promises, approvals and daily reconciliation."
                    : "One order. One cart. Every item accounted for."}
              </p>
            </div>
            <div className="header-actions">
              <span className={"connection " + (!online ? "offline" : "")}>
                {online ? "● Online" : "● Disconnected"}
              </span>
              <span className="mode">
                {session.mode === "demo"
                  ? "TRAINING"
                  : session.mode.toUpperCase()}
              </span>
              <button
                className="secondary"
                disabled={busy || !online || !!pending || adminLocked}
                hidden={tab === "inventory" || tab === "admin" || tab === "operations"}
                onClick={() => refresh().catch((e) => setError(e.message))}
              >
                Refresh
              </button>
            </div>
          </header>
          {session.mode !== "live" && (
            <div className="banner">
              {session.mode === "demo"
                ? "Training workspace · Synthetic orders and labels. Nothing here changes VISUAL."
                : "Shadow workspace · Live order information. Picking is disabled until cutover; supervisors can prepare inventory."}
            </div>
          )}
          {!online && (
            <div className="alert" role="alert">
              Connection lost. Confirmations are paused. Your saved work remains
              in the API.
            </div>
          )}
          {error && (
            <div className="alert" role="alert">
              {error}
            </div>
          )}
          {notice && (
            <div className="success" role="status">
              ✓ {notice}
            </div>
          )}
          {pending && (
            <div className="alert">
              The result is uncertain. Retry the same request to recover its
              saved result.
              <button
                disabled={busy || !online}
                onClick={() => send(pending.path, pending.body, pending.id)}
              >
                Retry confirmation
              </button>
            </div>
          )}
          {tab === "operations" ? (
            <Operations session={session} online={online} onLock={setAdminLocked} onAuthLost={() => setSession(null)} />
          ) : tab === "inventory" ? (
            <Inventory
              session={session}
              online={online}
              onLock={setAdminLocked}
              onAuthLost={() => setSession(null)}
            />
          ) : tab === "admin" && session.operator.role === "admin" ? (
            <Admin
              session={session}
              online={online}
              onLock={setAdminLocked}
              onAuthLost={() => setSession(null)}
              onOpenWork={(selected) => {
                setTab("cart");
                void select(selected).catch((e) => setError(e.message));
              }}
            />
          ) : !work ? (
            <>
              <div className="section-title">
                <h2>
                  {tab === "orders"
                    ? "Available to check out"
                    : tab === "cart"
                      ? "Assigned to you"
                      : tab === "packing"
                        ? "Carts and packages"
                        : "Needs attention"}
                </h2>
                <span>
                  {tab === "orders" ? orders.length : displayed.length}{" "}
                  {tab === "orders" ? "orders" : "fulfillments"}
                </span>
              </div>
              {tab === "orders" ? (
                <>
                  <label className="cart-input">
                    Scan your cart
                    <input
                      value={cart}
                      placeholder="Cart barcode"
                      onChange={(e) => setCart(e.target.value.toUpperCase())}
                    />
                  </label>
                  <div className="order-list">
                    {orders.map((o) => (
                      <article className="order-row" key={o.order}>
                        <div>
                          <span className="pill">READY</span>
                          <h3>{o.order}</h3>
                          <p>{o.customer}</p>
                        </div>
                        <div>
                          <small>DESTINATION</small>
                          <strong>
                            {o.destination.city}, {o.destination.state}
                          </strong>
                          <p>{o.destination.name}</p>
                        </div>
                        <div>
                          <small>YOUR PICK</small>
                          <strong>
                            {o.lines.reduce(
                              (s, l) => s + Number(l.quantity),
                              0,
                            )}{" "}
                            units ·{" "}
                            {new Set(o.lines.map((l) => l.order_line)).size}{" "}
                            lines
                          </strong>
                          <p>
                            {new Set(o.lines.map((l) => l.location)).size}{" "}
                            locations
                          </p>
                        </div>
                        <button
                          disabled={locked || !cart.trim()}
                          onClick={() =>
                            send("/checkouts", { order: o.order, cart })
                          }
                        >
                          Check out →
                        </button>
                      </article>
                    ))}
                  </div>
                  {!orders.length && (
                    <div className="empty">
                      <h2>No orders ready right now.</h2>
                      <p>
                        Refresh to check current release policies and
                        availability.
                      </p>
                    </div>
                  )}
                </>
              ) : (
                <div className="order-list">
                  {displayed.map((w) => (
                    <article className="order-row" key={w.id}>
                      <div>
                        <span className="pill">{stateName(w.state)}</span>
                        <h3>{w.order}</h3>
                        <p>{w.customer}</p>
                      </div>
                      <div>
                        <small>CART / OPERATOR</small>
                        <strong>
                          {w.cart} · {w.operator}
                        </strong>
                        <p>
                          {w.exception?.reason ||
                            `${w.packages.length} packages`}
                        </p>
                      </div>
                      <button
                        className="secondary"
                        onClick={() =>
                          select(w).catch((e) => setError(e.message))
                        }
                      >
                        Open work →
                      </button>
                    </article>
                  ))}
                  {!displayed.length && (
                    <div className="empty">
                      <h2>All clear.</h2>
                      <p>Work for this queue will appear here.</p>
                    </div>
                  )}
                </div>
              )}
            </>
          ) : (
            <>
              <div className="progress">
                {["Pick", "Handoff", "Pack", "VISUAL", "Labels", "Release"].map(
                  (s, i) => {
                    const stage =
                      work.state === "released"
                        ? 6
                        : work.state === "picking"
                          ? 0
                          : work.state === "ready_for_pack"
                            ? 1
                            : !work.verification
                              ? 2
                              : work.packages.every((p) => p.tracking)
                                ? 5
                                : 4;
                    return (
                      <div
                        className={
                          i < stage ? "done" : i === stage ? "current" : ""
                        }
                        key={s}
                      >
                        <span>
                          {i < stage ? "✓" : String(i + 1).padStart(2, "0")}
                        </span>
                        {s}
                      </div>
                    );
                  },
                )}
              </div>
              <div className="work-heading">
                <span className="pill">{stateName(work.state)}</span>
                <span>Revision {work.version}</span>
                <button
                  className="text-button"
                  onClick={() => print(work.cart)}
                >
                  Print cart barcode
                </button>
              </div>
              {work.exception && (
                <div className="alert">Exception: {work.exception.reason}</div>
              )}
              {work.state === "released" ? (
                <div className="release-summary">
                  <h2>✓ Released for shipping</h2>
                  <p>
                    {work.packages.length} packages · {work.packlist}
                  </p>
                  {work.packages.map((p) => (
                    <p key={p.id}>
                      <strong>{p.id}</strong> — {p.tracking}
                    </p>
                  ))}
                  <p>Carrier pickup and delivery are tracked separately.</p>
                </div>
              ) : work.state === "ready_for_pack" ? (
                <section className="panel">
                  <p className="eyebrow">RECEIVE THIS CART</p>
                  <h2>Ready for the packing station.</h2>
                  <p>Scan {work.cart} to accept custody of this order.</p>
                  <form
                    onSubmit={(e) => {
                      e.preventDefault();
                      void act("handoff", { cart, station });
                    }}
                    className="inline-form"
                  >
                    <label>
                      Cart barcode
                      <input
                        value={cart}
                        onChange={(e) => setCart(e.target.value.toUpperCase())}
                      />
                    </label>
                    <label>
                      Station
                      <input
                        value={station}
                        onChange={(e) =>
                          setStation(e.target.value.toUpperCase())
                        }
                      />
                    </label>
                    <button disabled={locked}>Accept cart →</button>
                  </form>
                </section>
              ) : (
                ["picking", "packing", "putaway"].includes(work.state) && (
                  <div className="work-grid">
                    <section className="panel">
                      <div className="section-title">
                        <h2>
                          {work.state === "packing"
                            ? "Pack each line"
                            : "Prescribed pick"}
                        </h2>
                        <span>{work.lines.length} stops</span>
                      </div>
                      <div className="lines">
                        {work.lines.map((l) => (
                          <button
                            key={l.id}
                            className={
                              "line " + (l.id === lineId ? "selected" : "")
                            }
                            onClick={() => chooseLine(l)}
                          >
                            <div>
                              <small>
                                SO LINE {l.order_line} · {l.location}
                              </small>
                              <strong>{l.part}</strong>
                              <span>{l.warehouse}</span>
                            </div>
                            <div className="line-qty">
                              <strong>
                                {work.state === "packing"
                                  ? packedQuantity(work, l.id)
                                  : l.picked}
                                <small> / {l.quantity}</small>
                              </strong>
                              <span>{l.uom}</span>
                            </div>
                          </button>
                        ))}
                      </div>
                      {work.state === "picking" && (
                        <button
                          className="full"
                          disabled={
                            locked ||
                            work.lines.some(
                              (l) =>
                                l.picked !== l.quantity &&
                                Number(l.picked) !== Number(l.quantity),
                            )
                          }
                          onClick={() => act("finish-pick")}
                        >
                          Finish picking →
                        </button>
                      )}
                      {work.state === "packing" && (
                        <div className="packages">
                          <h3>Packages</h3>
                          {work.packages.map((p) => (
                            <div
                              className={
                                "package " +
                                (packageId === p.id ? "selected" : "")
                              }
                              key={p.id}
                            >
                              <button
                                className="text-button"
                                onClick={() => setPackageId(p.id)}
                              >
                                {p.id} ·{" "}
                                {p.contents.reduce(
                                  (s, i) => s + Number(i.quantity),
                                  0,
                                )}{" "}
                                units
                              </button>
                              <span>
                                {p.tracking
                                  ? "✓ " + p.tracking
                                  : "Label pending"}
                              </span>
                              <div>
                                <button
                                  className="text-button"
                                  onClick={() => print(p.id)}
                                >
                                  Print barcode
                                </button>
                                <button
                                  className="text-button"
                                  disabled={locked || !reason}
                                  onClick={() =>
                                    act("reset-package", {
                                      package: p.id,
                                      reason,
                                    })
                                  }
                                >
                                  Empty & rescan
                                </button>
                                {!p.contents.length && (
                                  <button
                                    className="text-button"
                                    disabled={locked}
                                    onClick={() =>
                                      act("remove-package", { package: p.id })
                                    }
                                  >
                                    Remove
                                  </button>
                                )}
                              </div>
                            </div>
                          ))}
                          <button
                            className="secondary full"
                            disabled={locked}
                            onClick={async () => {
                              const value =
                                "PKG-" +
                                crypto.randomUUID().slice(0, 12).toUpperCase();
                              await act("package", { barcode: value });
                              setPackageId("");
                            }}
                          >
                            + Add package
                          </button>
                          <p className="hint">
                            Print and attach its barcode before scanning
                            contents.
                          </p>
                        </div>
                      )}
                    </section>
                    <section className="panel scan-panel">
                      <p className="eyebrow">
                        {work.state === "packing"
                          ? "CONFIRM PHYSICAL CONTENTS"
                          : work.state === "putaway"
                            ? "RETURN TO THE ORIGINAL BIN"
                            : "NEXT PICK"}
                      </p>
                      <h2>{line?.part || "Choose a line"}</h2>
                      {line && (
                        <>
                          <div className="location">
                            <span>{line.location}</span>
                            <div>
                              <small>REQUIRED</small>
                              <strong>
                                {line.quantity} {line.uom}
                              </strong>
                            </div>
                          </div>
                          {line.serialized && (
                            <div className="serials">
                              {line.serials.map((s) => (
                                <span
                                  className={
                                    line.picked_serials.includes(s)
                                      ? "scanned"
                                      : ""
                                  }
                                  key={s}
                                >
                                  {s}
                                </span>
                              ))}
                            </div>
                          )}
                          <form onSubmit={scan}>
                            {work.state === "packing" ? (
                              <label>
                                Scan package barcode
                                <input
                                  value={packageId}
                                  onChange={(e) =>
                                    setPackageId(e.target.value.toUpperCase())
                                  }
                                  required
                                />
                              </label>
                            ) : (
                              <label>
                                Scan location
                                <input
                                  value={location}
                                  onChange={(e) =>
                                    setLocation(e.target.value.toUpperCase())
                                  }
                                  onKeyDown={(e) => {
                                    if (e.key === "Enter") {
                                      e.preventDefault();
                                      scanRef.current?.focus();
                                    }
                                  }}
                                  placeholder={line.location}
                                  required
                                />
                              </label>
                            )}
                            <label>
                              {line.serialized
                                ? "Scan prescribed serial"
                                : "Scan part or UPC"}
                              <input
                                ref={scanRef}
                                className="barcode-input"
                                value={barcode}
                                autoComplete="off"
                                onChange={(e) =>
                                  setBarcode(e.target.value.toUpperCase())
                                }
                                required
                                placeholder="Ready to scan…"
                              />
                            </label>
                            {!line.serialized && (
                              <label>
                                Quantity ({line.uom})
                                <input
                                  inputMode="decimal"
                                  value={amount}
                                  onChange={(e) => setAmount(e.target.value)}
                                  required
                                />
                              </label>
                            )}
                            <button
                              className="full"
                              disabled={locked || !!work.exception}
                            >
                              {busy
                                ? "Saving…"
                                : work.state === "putaway"
                                  ? "Confirm putaway"
                                  : work.state === "packing"
                                    ? "Confirm in package"
                                    : "Confirm pick"}
                            </button>
                          </form>
                          <p className="hint">
                            A scan counts only after the API confirms it.
                          </p>
                        </>
                      )}
                    </section>
                  </div>
                )
              )}
              {work.state === "packing" && (
                <section className="panel verification">
                  <div>
                    <p className="eyebrow">FINAL CHECKS</p>
                    <h2>Match the record. Match the label.</h2>
                    <p>
                      {work.destination.name} · {work.destination.address1} ·{" "}
                      {work.destination.city} {work.destination.postal}
                    </p>
                  </div>
                  <details>
                    <summary>VISUAL entry manifest</summary>
                    <table>
                      <thead>
                        <tr>
                          <th>SO line</th>
                          <th>Part</th>
                          <th>Qty / unit</th>
                          <th>Warehouse / bin</th>
                          <th>Serials</th>
                        </tr>
                      </thead>
                      <tbody>
                        {work.lines.map((l) => (
                          <tr key={l.id}>
                            <td>{l.order_line}</td>
                            <td>{l.part}</td>
                            <td>
                              {l.quantity} {l.uom}
                            </td>
                            <td>
                              {l.warehouse} / {l.location}
                            </td>
                            <td>{l.serials.join(", ") || "Quantity item"}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </details>
                  {session.mode === "demo" && (
                    <div className="training">
                      <button
                        className="secondary"
                        disabled={busy}
                        onClick={demo}
                      >
                        Prepare sample ERP record & labels
                      </button>
                      {training && (
                        <p>
                          {training.packlist}
                          <br />
                          {training.labels.map((l) => (
                            <span key={l.package}>
                              {l.package}: {l.tracking}
                              <br />
                            </span>
                          ))}
                        </p>
                      )}
                    </div>
                  )}
                  <form
                    className="inline-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void act("verify", { packlist });
                    }}
                  >
                    <label>
                      Saved VISUAL packlist
                      <input
                        value={packlist}
                        onChange={(e) =>
                          setPacklist(e.target.value.toUpperCase())
                        }
                        placeholder="Scan packlist"
                        required
                      />
                    </label>
                    <button disabled={locked}>Verify VISUAL</button>
                    {work.verification && (
                      <span className="verified">✓ Contents matched</span>
                    )}
                  </form>
                  <form
                    className="inline-form"
                    onSubmit={(e) => {
                      e.preventDefault();
                      void act("label", { package: packageId, tracking });
                    }}
                  >
                    <label>
                      Package barcode
                      <input
                        value={packageId}
                        onChange={(e) =>
                          setPackageId(e.target.value.toUpperCase())
                        }
                        required
                      />
                    </label>
                    <label>
                      UPS tracking barcode
                      <input
                        value={tracking}
                        onChange={(e) =>
                          setTracking(e.target.value.toUpperCase())
                        }
                        required
                      />
                    </label>
                    <button disabled={locked || !work.verification}>
                      Verify label
                    </button>
                  </form>
                  <div className="release-footer">
                    <p>
                      Release requires a fresh ERP check and a verified label on
                      every package.
                    </p>
                    <button
                      disabled={
                        locked ||
                        !work.verification ||
                        work.packages.some((p) => !p.tracking) ||
                        !!work.exception
                      }
                      onClick={() => act("release")}
                    >
                      Release for shipping →
                    </button>
                  </div>
                </section>
              )}
              {!["released", "cancelled"].includes(work.state) && (
                <details className="panel exceptions">
                  <summary>Report a problem or correct this work</summary>
                  <label>
                    Reason / resolution
                    <input
                      value={reason}
                      onChange={(e) => setReason(e.target.value)}
                      placeholder="Describe what happened"
                    />
                  </label>
                  <div className="button-row">
                    <button
                      className="secondary"
                      disabled={locked || !reason}
                      onClick={() => act("exception", { reason })}
                    >
                      Report exception
                    </button>
                    {["supervisor", "admin"].includes(
                      session.operator.role,
                    ) && (
                      <>
                        <button
                          className="secondary"
                          disabled={locked || !reason || !work.exception}
                          onClick={() => act("resolve", { reason })}
                        >
                          Resolve exception
                        </button>
                        <button
                          className="danger"
                          disabled={locked || !reason}
                          onClick={() => act("cancel", { reason })}
                        >
                          Cancel & return stock
                        </button>
                      </>
                    )}
                  </div>
                  {["supervisor", "admin"].includes(session.operator.role) && (
                    <>
                      <form
                        className="inline-form"
                        onSubmit={(e) => {
                          e.preventDefault();
                          void act("transfer", { operator, reason });
                        }}
                      >
                        <label>
                          Transfer to operator ID
                          <input
                            value={operator}
                            onChange={(e) =>
                              setOperator(e.target.value.toUpperCase())
                            }
                          />
                        </label>
                        <button
                          className="secondary"
                          disabled={locked || !reason}
                        >
                          Transfer custody
                        </button>
                      </form>
                      {work.state === "picking" && (
                        <form
                          className="inline-form"
                          onSubmit={(e) => {
                            e.preventDefault();
                            void act("substitute", {
                              line_id: lineId,
                              old_serial: oldSerial,
                              serial: replacement,
                              reason,
                            });
                          }}
                        >
                          <label>
                            Unpicked prescribed serial
                            <input
                              value={oldSerial}
                              onChange={(e) =>
                                setOldSerial(e.target.value.toUpperCase())
                              }
                            />
                          </label>
                          <label>
                            Replacement at same location
                            <input
                              value={replacement}
                              onChange={(e) =>
                                setReplacement(e.target.value.toUpperCase())
                              }
                            />
                          </label>
                          <button
                            className="secondary"
                            disabled={locked || !reason}
                          >
                            Replace serial
                          </button>
                        </form>
                      )}
                    </>
                  )}
                </details>
              )}
              {work.events && (
                <details className="panel">
                  <summary>Activity history</summary>
                  {work.events.map((e, i) => (
                    <p key={i}>
                      <small>{new Date(e.at).toLocaleString()}</small> ·{" "}
                      {e.actor} · {e.action}
                    </p>
                  ))}
                </details>
              )}
            </>
          )}
          <footer>
            Dispatch /{" "}
            {session.mode === "live" ? "Production" : "Pre-production"}{" "}
            <span>Every confirmation stays with its order.</span>
          </footer>
        </main>
      </div>
      <div className="print-label">
        <h2>{work?.order}</h2>
        <p>{work?.customer}</p>
        {printId && <Barcode value={printId} />}
      </div>
    </>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
if (
  "serviceWorker" in navigator &&
  !["localhost", "127.0.0.1"].includes(location.hostname)
)
  navigator.serviceWorker.register("/sw.js").catch(() => {});
