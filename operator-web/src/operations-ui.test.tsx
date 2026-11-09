import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import type { Session } from "./types";

// The project deliberately has no DOM test runtime. Render the real component's
// host elements and exercise its actual handlers with a small stateful hook
// harness, while retaining React's escaping and server-rendered accessibility.
const harness = vi.hoisted(() => ({
  scope: "root",
  cursor: 0,
  states: new Map<string, any[]>(),
  api: vi.fn(),
}));
vi.mock("react", async () => {
  const actual = await vi.importActual<typeof import("react")>("react");
  return {
    ...actual,
    useEffect: () => undefined,
    useState: (initial: any) => {
      let state = harness.states.get(harness.scope);
      if (!state) {
        state = [];
        harness.states.set(harness.scope, state);
      }
      const index = harness.cursor++;
      if (!(index in state))
        state[index] = typeof initial === "function" ? initial() : initial;
      const saved = state;
      return [
        state[index],
        (next: any) => {
          saved[index] = typeof next === "function" ? next(saved[index]) : next;
        },
      ];
    },
  };
});
vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  api: harness.api,
}));
import { Operations } from "./Operations";
import { ApiError } from "./api";

type Node = React.ReactElement<any>;
const session: Session = {
  operator: { id: "REVIEWER", name: "Reviewer", role: "supervisor" },
  csrf: "csrf-proof",
  mode: "demo",
};
const request = {
  id: "REQ-1",
  version: 7,
  kind: "allocation",
  status: "submitted",
  requester: "SALES-1",
  requester_name: "Inside sales",
  created_at: "2026-10-20T12:00:00Z",
  terms: {
    customer: "BUYER",
    part: "PART",
    uom: "EA",
    quantity: "2",
    source: "stock",
    hold: true,
    reason: "Customer commitment",
  },
  history: [],
};
const closePacket = {
  id: "CLOSE-1",
  version: 4,
  business_date: "2026-10-20",
  timezone: "America/Denver",
  cutoff: "2026-10-20T23:00:00Z",
  prepared_at: "2026-10-20T23:01:00Z",
  prepared_by: "PREPARER",
  evidence_status: "balanced",
  approval_status: "draft",
  exceptions: [],
  packet: { source: { sequence: 2 }, applied_correspondences: [] },
};
const inventory = {
  config: {
    site: "DEMO",
    warehouse: "SHIPPING",
    location: "SHIP",
    version: 1,
    enabled: true,
  },
  balances: [],
  supplies: [],
  allocations: [],
  ledger: [],
  reconciliations: [],
};
const board = { orders: [], source: { available: true }, pending_triggers: [] };
const workflow = { sales_requests: [], approval_requests: [request] };
const responses: Record<string, any> = {
  "/planning/board": board,
  "/workflow": workflow,
  "/planning/runs": { runs: [] },
  "/daily-close": { closes: [], correspondences: [] },
  "/inventory": inventory,
};
let activeSession = session;
let online = true;

function rootState(section = "approvals", updates: Record<number, any> = {}) {
  const values: any[] = [
    section,
    "",
    "all",
    board,
    workflow,
    { runs: [] },
    { closes: [], correspondences: [] },
    inventory,
    "",
    "",
    false,
    null,
    false,
    null,
    null,
    null,
  ];
  for (const [index, value] of Object.entries(updates))
    values[Number(index)] = value;
  harness.states.set("root", values);
}
function expand(value: any, path = "tree"): any {
  if (Array.isArray(value))
    return value.map((child, i) => expand(child, path + "/" + i));
  if (!React.isValidElement(value)) return value;
  const element = value as Node;
  if (typeof element.type === "function") {
    harness.scope = path + "/" + (element.type as Function).name;
    harness.cursor = 0;
    return expand((element.type as Function)(element.props), path + "/render");
  }
  return React.cloneElement(
    element,
    { key: element.key ?? path },
    expand(element.props.children, path + "/children"),
  );
}
function render() {
  harness.scope = "root";
  harness.cursor = 0;
  return expand(
    Operations({
      session: activeSession,
      online,
      onAuthLost: vi.fn(),
      onLock: vi.fn(),
    }),
  ) as Node;
}
function all(node: any, predicate: (element: Node) => boolean): Node[] {
  if (Array.isArray(node))
    return node.flatMap((child) => all(child, predicate));
  if (!React.isValidElement(node)) return [];
  const element = node as Node;
  return [
    ...(predicate(element) ? [element] : []),
    ...all(element.props.children, predicate),
  ];
}
function text(node: any): string {
  if (Array.isArray(node)) return node.map(text).join("");
  if (React.isValidElement(node)) return text((node as Node).props.children);
  return node === null || node === undefined || typeof node === "boolean"
    ? ""
    : String(node);
}
function button(tree: Node, label: string) {
  const found = all(
    tree,
    (node) => node.type === "button" && text(node) === label,
  );
  expect(found).toHaveLength(1);
  return found[0];
}
function formFor(tree: Node, label: string) {
  const found = all(
    tree,
    (node) =>
      node.type === "form" &&
      all(node, (child) => child.type === "button" && text(child) === label)
        .length > 0,
  );
  expect(found).toHaveLength(1);
  return found[0];
}
function submit(form: Node, values: Record<string, string>, action?: string) {
  form.props.onSubmit({
    preventDefault() {},
    currentTarget: values,
    nativeEvent: { submitter: { getAttribute: () => action } },
  });
}
function change(tree: Node, label: string, value: string) {
  const inputs = all(
    tree,
    (node) => node.type === "input" && node.props["aria-label"] === label,
  );
  expect(inputs).toHaveLength(1);
  inputs[0].props.onChange({ target: { value } });
}
function posts(path?: string) {
  return harness.api.mock.calls.filter(
    ([called, body]) => body !== undefined && (!path || called === path),
  );
}
async function settle() {
  await vi.waitFor(() => expect(harness.states.get("root")![10]).toBe(false));
}

beforeEach(() => {
  harness.states.clear();
  harness.api.mockReset();
  activeSession = session;
  online = true;
  harness.api.mockImplementation(async (path: string, body?: any) =>
    body === undefined
      ? responses[path]
      : { ...request, status: "approved", version: 8 },
  );
  let sequence = 0;
  vi.stubGlobal("crypto", { randomUUID: () => "operation-key-" + ++sequence });
  vi.stubGlobal(
    "FormData",
    class {
      constructor(private values: Record<string, string>) {}
      get(key: string) {
        return this.values[key] ?? null;
      }
    },
  );
  rootState();
});
afterEach(() => vi.unstubAllGlobals());

describe("Operating workflow controls", () => {
  it("offers independent review and sends the exact displayed revision", async () => {
    const tree = render();
    expect(renderToStaticMarkup(tree)).toContain("Approve exact terms");
    expect(renderToStaticMarkup(tree)).toContain("Reject");
    submit(
      formFor(tree, "Approve exact terms"),
      { reason: "Reviewed current terms" },
      "request-approve",
    );
    await settle();
    expect(posts("/workflow/request-approve")[0]).toEqual([
      "/workflow/request-approve",
      { id: "REQ-1", version: 7, reason: "Reviewed current terms" },
      "csrf-proof",
      "operation-key-1",
    ]);
  });

  it("hides self-approval and limits early release to administrators", () => {
    activeSession = {
      ...session,
      operator: { ...session.operator, id: "SALES-1" },
    };
    expect(renderToStaticMarkup(render())).not.toContain("Approve exact terms");
    expect(renderToStaticMarkup(render())).toContain(
      "different authorized person",
    );
    activeSession = session;
    rootState("approvals", {
      4: {
        ...workflow,
        approval_requests: [{ ...request, kind: "early_release" }],
      },
    });
    expect(renderToStaticMarkup(render())).not.toContain("Approve exact terms");
    activeSession = {
      ...session,
      operator: { ...session.operator, role: "admin" },
    };
    expect(renderToStaticMarkup(render())).toContain("Approve exact terms");
  });

  it("keeps a new allocation held by default and records a pending draft", async () => {
    rootState("approvals", { 13: {} });
    const tree = render();
    const hold = all(
      tree,
      (node) => node.type === "input" && node.props.name === "hold",
    )[0];
    expect(hold.props.defaultChecked).toBe(true);
    expect(renderToStaticMarkup(tree)).toContain(
      "Drafts and pending requests do not reserve stock",
    );
    submit(formFor(tree, "Save request draft"), {
      customer: "BUYER",
      part: "PART",
      uom: "EA",
      quantity: "3",
      hold: "on",
      priority: "37",
      reason: "Inside sales request",
    });
    await settle();
    expect(posts("/workflow/request-create")[0][1]).toMatchObject({
      kind: "allocation",
      terms: {
        source: "stock",
        hold: true,
        quantity: "3",
        priority: 37,
        customer: "BUYER",
      },
    });
  });

  it("creates a sales draft with exact entered lines and explicitly requires VISUAL binding", async () => {
    rootState("sales", { 13: {} });
    change(render(), "Line 1 part", "PART-NEW");
    change(render(), "Line 1 quantity", "2.5");
    const tree = render();
    expect(renderToStaticMarkup(tree)).toContain(
      "An existing VISUAL order must be linked",
    );
    submit(formFor(tree, "Save sales draft"), {
      customer: "BUYER",
      site: "DEMO",
      reference: "PO-123",
      need_by: "2026-11-01",
      destination_name: "Buyer",
      destination_address1: "1 Main St",
      destination_address3: "Receiving entrance",
      destination_city: "Denver",
      destination_state: "CO",
      destination_postal: "80202",
      destination_country: "US",
      reason: "Customer order request",
    });
    await settle();
    expect(posts("/workflow/sales-create")[0][1]).toMatchObject({
      customer: "BUYER",
      site: "DEMO",
      customer_reference: "PO-123",
      lines: [{ id: "1", part: "PART-NEW", quantity: "2.5", uom: "EA" }],
      destination: { name: "Buyer", address3: "Receiving entrance", city: "Denver", postal: "80202" },
    });
    expect(posts().map((call) => call[0])).toEqual(["/workflow/sales-create"]);
  });

  it("recovers an uncertain approval with its original body and idempotency key", async () => {
    let attempts = 0;
    harness.api.mockImplementation(async (path: string, body?: any) => {
      if (body === undefined) return responses[path];
      if (++attempts === 1)
        throw new Error("Response lost after server commit");
      return { ...request, status: "approved", version: 8 };
    });
    submit(
      formFor(render(), "Approve exact terms"),
      { reason: "Independent review" },
      "request-approve",
    );
    await settle();
    const uncertain = render();
    expect(renderToStaticMarkup(uncertain)).toContain("result is uncertain");
    expect(
      all(uncertain, (n) => n.props.role === "tab").every(
        (n) => n.props.disabled,
      ),
    ).toBe(true);
    expect(
      all(
        formFor(uncertain, "Approve exact terms"),
        (n) => n.type === "fieldset",
      )[0].props.disabled,
    ).toBe(true);
    button(uncertain, "Recover confirmation").props.onClick();
    await settle();
    const calls = posts("/workflow/request-approve");
    expect(calls).toHaveLength(2);
    expect(calls[1]).toEqual(calls[0]);
    expect(renderToStaticMarkup(render())).not.toContain(
      "Recover confirmation",
    );
  });

  it("refreshes on a known revision rejection without treating it as uncertain", async () => {
    harness.api.mockImplementation(async (path: string, body?: any) => {
      if (body === undefined) return responses[path];
      throw new ApiError("Request revision changed", 409);
    });
    submit(
      formFor(render(), "Approve exact terms"),
      { reason: "Reviewed stale tab" },
      "request-approve",
    );
    await settle();
    const html = renderToStaticMarkup(render());
    expect(html).toContain("Request revision changed");
    expect(html).not.toContain("Recover confirmation");
    expect(
      harness.api.mock.calls.some(
        ([path, body]) => path === "/workflow" && body === undefined,
      ),
    ).toBe(true);
  });
});

describe("Daily close review controls", () => {
  it("records exact correction identities without replacing the selected close packet", async () => {
    rootState("close", { 14: closePacket });
    harness.api.mockImplementation(async (path: string, body?: any) =>
      body === undefined
        ? responses[path]
        : {
            id: "MAP-1",
            source_ids: ["100", "101"],
            ledger_ids: ["LEDGER-1", "LEDGER-2"],
            status: "proposed",
          },
    );
    submit(formFor(render(), "Record exact correspondence"), {
      source_ids: "100, 101",
      ledger_ids: "LEDGER-1 LEDGER-2",
      reference: "CASE-9",
      reason: "Reviewed inventory correction",
    });
    await settle();
    expect(posts("/daily-close/correspondence")[0][1]).toEqual({
      source_ids: ["100", "101"],
      ledger_ids: ["LEDGER-1", "LEDGER-2"],
      metadata_only: false,
      reference: "CASE-9",
      reason: "Reviewed inventory correction",
    });
    expect(harness.states.get("root")![14]).toEqual(closePacket);
    expect(renderToStaticMarkup(render())).toContain("Close for 2026-10-20");
  });

  it.each(["sales", "operator"] as const)(
    "keeps %s close access read-only",
    (role) => {
      activeSession = { ...session, operator: { ...session.operator, role } };
      rootState("close", { 14: closePacket });
      const html = renderToStaticMarkup(render());
      expect(html).toContain("Download complete evidence packet");
      expect(html).not.toContain("Sign reviewed revision");
      expect(html).not.toContain("Prepare current close");
    },
  );

  it.each(["incomplete", "self", "offline", "stale", "correspondence author"])(
    "blocks sign-off when %s",
    (condition) => {
      rootState("close", {
        14: {
          ...closePacket,
          ...(condition === "incomplete"
            ? { evidence_status: "incomplete" }
            : {}),
          ...(condition === "self" ? { prepared_by: "REVIEWER" } : {}),
          ...(condition === "correspondence author"
            ? {
                packet: {
                  ...closePacket.packet,
                  applied_correspondences: [
                    { id: "MAP-1", created_by: "REVIEWER" },
                  ],
                },
              }
            : {}),
        },
        12: condition === "stale",
      });
      online = condition !== "offline";
      const form = formFor(render(), "Sign reviewed revision");
      expect(
        all(form, (node) => node.type === "fieldset")[0].props.disabled,
      ).toBe(true);
    },
  );

  it("signs only the displayed packet revision with explicit exception consent", async () => {
    const reviewed = {
      ...closePacket,
      evidence_status: "exceptions",
      exceptions: [
        {
          id: "X-1",
          kind: "balance_difference",
          message: "One unit under investigation",
          reference: "PART",
          disposition: "accepted_exception",
          owner: "PREPARER",
          due_at: "2026-10-22T12:00:00Z",
        },
      ],
    };
    rootState("close", { 14: reviewed });
    const tree = render();
    expect(renderToStaticMarkup(tree)).toContain(
      "this is not a balanced close",
    );
    const form = formFor(tree, "Sign reviewed revision");
    expect(
      all(form, (n) => n.type === "input" && n.props.name === "accept")[0].props
        .required,
    ).toBe(true);
    harness.api.mockImplementation(async (path: string, body?: any) =>
      body === undefined
        ? responses[path]
        : {
            ...reviewed,
            version: 5,
            approval_status: "signed",
            signed_by: "REVIEWER",
          },
    );
    submit(form, { accept: "on", reason: "Accepted documented exception" });
    await settle();
    expect(posts("/daily-close/sign")[0]).toEqual([
      "/daily-close/sign",
      {
        id: "CLOSE-1",
        version: 4,
        allow_exceptions: true,
        reason: "Accepted documented exception",
      },
      "csrf-proof",
      "operation-key-1",
    ]);
    expect(renderToStaticMarkup(render())).not.toContain(
      "Sign reviewed revision",
    );
  });
});
