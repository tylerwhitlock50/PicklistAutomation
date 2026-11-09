export type Quantity = string | number;
export type InventoryConfig = {
  enabled: boolean;
  site: string;
  warehouse: string;
  location: string;
  version: number;
};
export type StockBalance = {
  id: string;
  part: string;
  uom: string;
  serialized: boolean;
  serial: string | null;
  quantity: Quantity;
  committed: Quantity;
  available: Quantity;
  sync_blocked?: boolean;
};
export type FutureSupply = {
  id: string;
  work_order: string;
  part: string;
  uom: string;
  serialized: boolean;
  quantity: Quantity;
  received: Quantity;
  reserved: Quantity;
  due_date: string;
  status: "open" | "cancelled" | "closed";
  version: number;
  available: Quantity;
  shortfall: Quantity;
};
export type InventoryAllocation = {
  id: string;
  source: "stock" | "work_order";
  source_id: string | null;
  part: string;
  uom: string;
  customer: string;
  order: string | null;
  order_line: string | null;
  quantity: Quantity;
  fulfilled: Quantity;
  hold: boolean;
  review_at: string | null;
  reason: string;
  status: string;
  version: number;
  review_due: boolean;
  priority?: number;
  request_reference?: string;
  need_by?: string | null;
  transfer_from?: string;
  risks?: { code: string; message: string }[];
  shortage?: Quantity;
  expected_arrival?: string | null;
  locked_quantity?: Quantity;
  transferable_quantity?: Quantity;
  at_risk?: boolean;
  parent_id?: string;
  receipt_event_id?: string;
  items?: { stock_id: string; quantity: Quantity }[];
};
export type InventorySyncState = {
  connected: boolean;
  stream_id: string | null;
  sequence: number | null;
  observed_at: string | null;
  last_success: string | null;
  status: "matched" | "variance" | "stale" | "source_error";
  fresh?: boolean;
  freshness_seconds?: number;
  source_error?: { code: string; at: string; message: string } | null;
  issues: {
    id: string;
    kind: string;
    event_id?: string;
    part?: string;
    quantity?: Quantity;
    message: string;
    at: string;
  }[];
};
export type InventoryState = {
  sync?: InventorySyncState;
  config: InventoryConfig;
  balances: StockBalance[];
  supplies: FutureSupply[];
  allocations: InventoryAllocation[];
  ledger: {
    id: string;
    at: string;
    actor: string;
    kind: string;
    part: string;
    uom: string;
    serial: string;
    quantity: Quantity;
    balance: Quantity;
    reason: string;
    reference: string;
  }[];
  reconciliations: {
    id: string;
    at: string;
    observed_at: string;
    status: string;
    differences: {
      part: string;
      uom: string;
      serial: string;
      local: Quantity;
      visual: Quantity;
      delta: Quantity;
    }[];
  }[];
  as_of: string;
};

export type InventoryDemand = {
  orders: {
    order: string;
    site: string;
    customer_id: string;
    customer: string;
    lines: {
      order_line: string;
      part: string;
      uom: string;
      quantity: Quantity;
      serialized: boolean;
    }[];
  }[];
  as_of: string;
};
