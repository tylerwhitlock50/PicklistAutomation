export type Destination = {
  name: string;
  address1: string;
  address2?: string;
  address3?: string;
  city: string;
  state: string;
  postal: string;
  country: string;
};
export type Operator = {
  id: string;
  name: string;
  role: "operator" | "supervisor";
};
export type Line = {
  id: string;
  order_line: string;
  part: string;
  quantity: string;
  uom: string;
  serialized: boolean;
  serials: string[];
  warehouse: string;
  location: string;
  upc?: string;
  picked: string;
  picked_serials: string[];
};
export type Package = {
  id: string;
  tracking: string | null;
  contents: { line_id: string; quantity: string; serials: string[] }[];
};
export type Order = {
  site: string;
  order: string;
  customer: string;
  ship_to: string;
  destination: Destination;
  lines: Line[];
  policy: Record<string, unknown>;
};
export type Work = Order & {
  id: string;
  cart: string;
  operator: string;
  state: string;
  version: number;
  lines: Line[];
  packages: Package[];
  verification: { at: string; result: string; bindings: unknown[] } | null;
  packlist: string | null;
  exception: { reason: string } | null;
  updated_at: string;
  events?: { at: string; actor: string; action: string }[];
};
export type Session = {
  operator: Operator;
  csrf: string;
  mode: "demo" | "shadow" | "live";
};
