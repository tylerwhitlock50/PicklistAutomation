import type { InventorySyncState } from "./inventory-types";

export function inventorySyncHealth(
  sync?: InventorySyncState,
  now = Date.now(),
) {
  if (!sync?.connected)
    return { blocked: false, label: "MANUAL PREPARATION", message: "" };
  if (sync.source_error || sync.status === "source_error")
    return {
      blocked: true,
      label: "SOURCE ERROR",
      message:
        "VISUAL sync reported a source error. Stock planning, picking, and shipping release are paused until a fresh valid batch is received.",
    };
  const observed = Date.parse(sync.observed_at || "");
  const limit = (sync.freshness_seconds || 300) * 1000;
  if (
    sync.fresh === false ||
    sync.status === "stale" ||
    (sync.fresh === true &&
      (!Number.isFinite(observed) || now - observed > limit))
  )
    return {
      blocked: true,
      label: "SYNC STALE",
      message:
        "VISUAL has not supplied a current warehouse capture within " +
        Math.round(limit / 60000) +
        " minutes. Stock planning, picking, and shipping release are paused until a fresh valid batch is received. Reload inventory after the connector recovers.",
    };
  return {
    blocked: false,
    label: sync.status === "variance" ? "REVIEW REQUIRED" : "CONNECTED",
    message: "",
  };
}
