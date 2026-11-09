import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it, vi } from "vitest";
import { DemandFields, CommitmentFields } from "./InventoryDemand";
import { InventorySync } from "./InventorySync";
import type {
  InventoryAllocation,
  InventoryDemand,
  InventorySyncState,
} from "./inventory-types";

const demand: InventoryDemand = {
  as_of: "2026-10-20T12:00:00Z",
  orders: [
    {
      order: "ORDER-EA",
      site: "SITE",
      customer_id: "BUYER",
      customer: "Buyer name",
      lines: [
        {
          order_line: "1",
          part: "PART",
          uom: "EA",
          quantity: "3",
          serialized: false,
        },
      ],
    },
    {
      order: "ORDER-BOX",
      site: "SITE",
      customer_id: "BUYER",
      customer: "Buyer name",
      lines: [
        {
          order_line: "2",
          part: "PART",
          uom: "BOX",
          quantity: "1",
          serialized: false,
        },
      ],
    },
    {
      order: "ORDER-OTHER",
      site: "SITE",
      customer_id: "OTHER",
      customer: "Another buyer",
      lines: [
        {
          order_line: "1",
          part: "OTHER",
          uom: "EA",
          quantity: "3",
          serialized: false,
        },
      ],
    },
  ],
};
const sync: InventorySyncState = {
  connected: true,
  stream_id: "stream",
  sequence: 1,
  observed_at: "2026-10-20T12:00:00Z",
  last_success: "2026-10-20T12:00:00Z",
  status: "variance",
  issues: [
    {
      id: "receipt",
      kind: "unlinked_receipt",
      event_id: "R-1",
      part: "PART",
      quantity: "2",
      message: "Receipt needs its production source",
      at: "2026-10-20T12:00:00Z",
    },
  ],
};

describe("Inventory commitment controls", () => {
  it("renders source recovery status separately from acknowledgable issues", () => {
    const html = renderToStaticMarkup(
      <InventorySync
        sync={{
          ...sync,
          status: "source_error",
          fresh: false,
          issues: [],
          source_error: {
            code: "capture_failed",
            at: sync.observed_at!,
            message: "Source validation failed",
          },
        }}
        supplies={[]}
        locked={false}
        revision={0}
        submit={vi.fn()}
      />,
    );
    expect(html).toContain("SOURCE ERROR");
    expect(html).toContain("Source validation failed");
    expect(html).toContain(
      "Acknowledging issues cannot restore source freshness",
    );
    expect(html).not.toContain("Acknowledge issue");
  });

  it("offers open order choices only for the selected part and stocking unit", () => {
    const html = renderToStaticMarkup(
      <DemandFields demand={demand} part="PART" uom="EA" />,
    );
    expect(html).toContain('value="ORDER-EA"');
    expect(html).not.toContain('value="ORDER-BOX"');
    expect(html).not.toContain('value="ORDER-OTHER"');
    expect(html).toContain('value="BUYER"');
  });
  it("keeps customer pools and manual binding usable when order lookup fails", () => {
    const html = renderToStaticMarkup(
      <DemandFields
        demand={null}
        part="PART"
        uom="EA"
        defaults={{ customer: "BUYER", order: "OLD-ORDER", order_line: "7" }}
      />,
    );
    expect(html).toContain('name="customer"');
    expect(html).toContain('value="BUYER"');
    expect(html).toContain('value="OLD-ORDER"');
    expect(html).toContain('value="7"');
    expect(html).not.toContain('readOnly=""');
  });
  it("preserves an existing nonstandard priority during unrelated edits", () => {
    const html = renderToStaticMarkup(
      <CommitmentFields
        allocation={
          {
            priority: 37,
            need_by: "2026-11-01",
            request_reference: "SALES-9",
          } as InventoryAllocation
        }
      />,
    );
    expect(html).toContain('value="37" selected=""');
    expect(html).toContain('value="2026-11-01"');
    expect(html).toContain('value="SALES-9"');
  });
  it("keeps issue resolution read-only for locked sessions and does not promise quarantine release", () => {
    const html = renderToStaticMarkup(
      <InventorySync
        sync={sync}
        supplies={[]}
        locked={true}
        revision={0}
        submit={vi.fn()}
      />,
    );
    expect(html).toContain('<fieldset disabled=""');
    expect(html).toContain("stock under sync review stays unavailable");
    expect(html).toContain("No matching supply record is available");
  });
});
