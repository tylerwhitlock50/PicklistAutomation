import { describe, expect, it } from "vitest";
import { inventorySyncHealth } from "./inventory-health";
import type { InventorySyncState } from "./inventory-types";

const observed = Date.parse("2026-10-20T12:00:00Z");
const state: InventorySyncState = {
  connected: true,
  stream_id: "sync",
  sequence: 4,
  observed_at: new Date(observed).toISOString(),
  last_success: new Date(observed).toISOString(),
  status: "matched",
  fresh: true,
  freshness_seconds: 300,
  source_error: null,
  issues: [],
};
describe("Inventory source freshness", () => {
  it("expires a once-fresh displayed capture after five minutes", () => {
    expect(inventorySyncHealth(state, observed + 300000).blocked).toBe(false);
    expect(inventorySyncHealth(state, observed + 300001)).toMatchObject({
      blocked: true,
      label: "SYNC STALE",
    });
  });
  it("blocks a current capture after a source failure, even without an open issue", () => {
    expect(
      inventorySyncHealth(
        {
          ...state,
          status: "source_error",
          fresh: false,
          source_error: {
            code: "capture_failed",
            at: state.observed_at!,
            message: "Source capture failed",
          },
        },
        observed + 1000,
      ),
    ).toMatchObject({ blocked: true, label: "SOURCE ERROR" });
  });
  it("respects server-declared staleness and does not globally block a fresh variance", () => {
    expect(
      inventorySyncHealth({ ...state, fresh: false }, observed).blocked,
    ).toBe(true);
    expect(
      inventorySyncHealth({ ...state, status: "variance" }, observed),
    ).toMatchObject({ blocked: false, label: "REVIEW REQUIRED" });
  });
  it("keeps manual preparation and prior snapshots without freshness fields usable", () => {
    expect(
      inventorySyncHealth({ ...state, connected: false }, observed + 999999)
        .blocked,
    ).toBe(false);
    expect(
      inventorySyncHealth({ ...state, fresh: undefined }, observed + 999999)
        .blocked,
    ).toBe(false);
    expect(
      inventorySyncHealth(
        { ...state, fresh: undefined, status: "stale" },
        observed,
      ).blocked,
    ).toBe(true);
  });
});
