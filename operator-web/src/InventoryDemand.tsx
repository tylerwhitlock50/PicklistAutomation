import React, { useId, useState } from "react";
import type { InventoryAllocation, InventoryDemand } from "./inventory-types";

export function DemandFields({
  demand,
  part,
  uom,
  defaults,
}: {
  demand: InventoryDemand | null;
  part?: string;
  uom?: string;
  defaults?: Pick<InventoryAllocation, "customer" | "order" | "order_line">;
}) {
  const listId = useId();
  const [orderId, setOrderId] = useState("");
  const orders = (demand?.orders || []).filter((order) =>
    order.lines.some(
      (line) => (!part || line.part === part) && (!uom || line.uom === uom),
    ),
  );
  const selected = orders.find((order) => order.order === orderId);
  const matchingLines =
    selected?.lines.filter(
      (line) => (!part || line.part === part) && (!uom || line.uom === uom),
    ) || [];
  const customers = [
    ...new Map(
      (demand?.orders || []).map((order) => [
        order.customer_id,
        order.customer,
      ]),
    ).entries(),
  ];
  return (
    <>
      {!!orders.length && (
        <label className="inventory-span">
          Choose an open sales order
          <select
            value={orderId}
            onChange={(event) => setOrderId(event.target.value)}
          >
            <option value="">Customer pool or enter an order manually</option>
            {orders.map((order) => (
              <option key={order.order} value={order.order}>
                {order.order} · {order.customer} ({order.customer_id})
              </option>
            ))}
          </select>
        </label>
      )}
      <div
        className="inventory-fields inventory-span inventory-demand-fields"
        key={selected?.order || "manual"}
      >
        <label>
          Customer ID
          <input
            name="customer"
            required
            maxLength={200}
            list={listId}
            defaultValue={selected?.customer_id || defaults?.customer || ""}
            readOnly={!!selected}
          />
        </label>
        <datalist id={listId}>
          {customers.map(([id, name]) => (
            <option key={id} value={id}>
              {name}
            </option>
          ))}
        </datalist>
        <label>
          Sales order (optional)
          <input
            name="order"
            maxLength={200}
            defaultValue={selected?.order || defaults?.order || ""}
            readOnly={!!selected}
          />
        </label>
        {selected ? (
          <label className="inventory-span">
            Order line
            <select name="order_line" defaultValue="">
              <option value="">Any matching line on this order</option>
              {matchingLines.map((line) => (
                <option key={line.order_line} value={line.order_line}>
                  Line {line.order_line} · {line.part} · {line.quantity}{" "}
                  {line.uom} open
                </option>
              ))}
            </select>
          </label>
        ) : (
          <label>
            Order line (optional)
            <input
              name="order_line"
              maxLength={200}
              defaultValue={defaults?.order_line || ""}
            />
          </label>
        )}
        {selected && (
          <p className="hint inventory-span">
            Order and line bindings are checked against current VISUAL demand
            when saved.
          </p>
        )}
      </div>
    </>
  );
}

export function CommitmentFields({
  allocation,
}: {
  allocation?: InventoryAllocation;
}) {
  return (
    <>
      <label>
        Priority
        <select name="priority" defaultValue={allocation?.priority ?? 50}>
          {[
            ...new Set([
              0,
              25,
              50,
              75,
              100,
              ...(allocation?.priority !== undefined
                ? [allocation.priority]
                : []),
            ]),
          ]
            .sort((a, b) => a - b)
            .map((value) => (
              <option key={value} value={value}>
                {value === 0
                  ? "0 · Urgent"
                  : value === 50
                    ? "50 · Normal"
                    : value === 100
                      ? "100 · Low"
                      : value}
              </option>
            ))}
        </select>
      </label>
      <label>
        Customer need-by date
        <input
          name="need_by"
          type="date"
          defaultValue={allocation?.need_by || ""}
        />
      </label>
      <label className="inventory-span">
        Inside sales request reference
        <input
          name="request_reference"
          maxLength={200}
          defaultValue={allocation?.request_reference || ""}
          placeholder="Request, ticket, or customer reference"
        />
      </label>
      <p className="hint inventory-span">
        Lower priority numbers receive future production first. Existing stock
        allocations stay protected.
      </p>
    </>
  );
}
