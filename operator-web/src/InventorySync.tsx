import type React from "react";
import { inventorySyncHealth } from "./inventory-health";
import type { FutureSupply, InventorySyncState } from "./inventory-types";

const text = (data: FormData, key: string) =>
  String(data.get(key) || "").trim();
const dateTime = (value?: string | null) =>
  value ? new Date(value).toLocaleString() : "Not received";

export function InventorySync({
  sync,
  now = Date.now(),
  supplies,
  locked,
  revision,
  submit,
}: {
  sync?: InventorySyncState;
  now?: number;
  supplies: FutureSupply[];
  locked: boolean;
  revision: number;
  submit: (
    event: React.FormEvent<HTMLFormElement>,
    action: string,
    body: (data: FormData) => Record<string, unknown>,
  ) => void;
}) {
  const health = inventorySyncHealth(sync, now);
  return (
    <section className="panel inventory-sync">
      <div className="section-title">
        <div>
          <p className="eyebrow">VISUAL CONNECTION</p>
          <h3>Supply feed health</h3>
        </div>
        <span
          className={
            "pill " +
            (health.blocked || sync?.status === "variance" ? "pill-warm" : "")
          }
        >
          {health.label}
        </span>
      </div>
      <p>
        {sync?.connected
          ? "VISUAL supplies warehouse arrivals and work-order updates. Dispatch owns customer commitments, holds, and shipping decisions."
          : "Choose manual preparation or start the VISUAL connector with an empty stock ledger. The connector imports opening stock, then owns warehouse receipts and work-order updates."}
      </p>
      {health.blocked && (
        <div className="alert" role="alert">
          {health.message}
          {sync?.source_error && (
            <p>
              {sync.source_error.message} · {dateTime(sync.source_error.at)}
            </p>
          )}
        </div>
      )}
      {sync?.connected && (
        <dl className="inventory-sync-facts">
          <div>
            <dt>Last successful sync</dt>
            <dd>{dateTime(sync.last_success)}</dd>
          </div>
          <div>
            <dt>Source observed at</dt>
            <dd>{dateTime(sync.observed_at)}</dd>
          </div>
          <div>
            <dt>Stream / sequence</dt>
            <dd>
              {sync.stream_id || "—"} / {sync.sequence ?? "—"}
            </dd>
          </div>
        </dl>
      )}
      {sync?.issues.length ? (
        <div className="inventory-sync-issues">
          <p className="hint">
            {sync.issues.length} open sync{" "}
            {sync.issues.length === 1 ? "issue" : "issues"}. Acknowledgement
            records review; stock under sync review stays unavailable until its
            source discrepancy is resolved.
          </p>
          {sync.issues.map((issue) => {
            const candidates = supplies.filter(
              (supply) => !issue.part || supply.part === issue.part,
            );
            return (
              <article key={issue.id} className="inventory-sync-issue">
                <div className="section-title">
                  <strong>
                    {issue.kind === "unlinked_receipt"
                      ? "Receipt needs a work order"
                      : issue.kind.replaceAll("_", " ")}
                  </strong>
                  <span>{dateTime(issue.at)}</span>
                </div>
                <p>{issue.message}</p>
                <p className="hint">
                  {issue.part && <>Part {issue.part} · </>}
                  {issue.quantity !== undefined && (
                    <>Quantity {issue.quantity} · </>
                  )}
                  {issue.event_id
                    ? "Receipt " + issue.event_id
                    : "Issue " + issue.id}
                </p>
                {issue.kind === "unlinked_receipt" && issue.event_id && (
                  <details className="inventory-import">
                    <summary>Link receipt to a work order</summary>
                    <p>
                      Attach the stock already received to its work order. This
                      can fulfill future promises without increasing on-hand
                      inventory. Only free stock can be linked. Completed work
                      orders remain available for receipt tracing.
                    </p>
                    {candidates.length ? (
                      <form
                        key={"link-" + issue.id + "-" + revision}
                        onSubmit={(event) =>
                          submit(event, "link-receipt", (data) => {
                            const supply = supplies.find(
                              (item) => item.id === text(data, "work_order_id"),
                            );
                            if (!supply)
                              throw new Error(
                                "Choose a current work-order supply record.",
                              );
                            return {
                              event_id: issue.event_id,
                              work_order_id: supply.id,
                              version: supply.version,
                              reason: text(data, "reason"),
                            };
                          })
                        }
                      >
                        <fieldset
                          disabled={locked || health.blocked}
                          className="inventory-fields"
                        >
                          <label className="inventory-span">
                            Work order
                            <select
                              name="work_order_id"
                              required
                              defaultValue=""
                            >
                              <option value="" disabled>
                                Choose matching work-order supply
                              </option>
                              {candidates.map((supply) => (
                                <option key={supply.id} value={supply.id}>
                                  {supply.work_order} · {supply.part} ·{" "}
                                  {supply.uom} · {supply.status}
                                </option>
                              ))}
                            </select>
                          </label>
                          <label className="inventory-span">
                            Reason
                            <input
                              name="reason"
                              required
                              maxLength={1000}
                              placeholder="Production receipt reference / matching evidence"
                            />
                          </label>
                          <button type="submit">Link existing receipt</button>
                        </fieldset>
                      </form>
                    ) : (
                      <p className="inventory-callout">
                        No matching supply record is available. Check the
                        work-order feed before linking this receipt.
                      </p>
                    )}
                  </details>
                )}
                <details className="inventory-import">
                  <summary>Acknowledge review</summary>
                  <form
                    key={"ack-" + issue.id + "-" + revision}
                    onSubmit={(event) =>
                      submit(event, "sync-ack", (data) => ({
                        id: issue.id,
                        reason: text(data, "reason"),
                      }))
                    }
                  >
                    <fieldset disabled={locked} className="inventory-fields">
                      <label className="inventory-span">
                        Review note
                        <input
                          name="reason"
                          required
                          maxLength={1000}
                          placeholder="What was checked and the next corrective action"
                        />
                      </label>
                      <button type="submit">Acknowledge issue</button>
                    </fieldset>
                  </form>
                </details>
              </article>
            );
          })}
        </div>
      ) : (
        sync?.connected && (
          <p className="inventory-callout">
            {health.blocked
              ? "Connector recovery is required. Acknowledging issues cannot restore source freshness."
              : "No open connector issues. Stock differences remain visible in reconciliation below."}
          </p>
        )
      )}
    </section>
  );
}
