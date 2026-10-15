// Keep uncertain requests in memory and retry their original identity. A lost
// response must not create another scan, resolution, session or completion.
let pendingAuditRequest = null;
async function auditFetch(url, options) {
  const signature = JSON.stringify([url, options.body || ""]);
  if (pendingAuditRequest && pendingAuditRequest.signature !== signature) {
    throw new Error("An earlier audit confirmation is uncertain. Retry that action first.");
  }
  const key = pendingAuditRequest?.key || crypto.randomUUID();
  pendingAuditRequest = { signature, key };
  const response = await fetch(url, { ...options, headers: { ...options.headers, "Idempotency-Key": key } });
  // Consume and retain the body before clearing pending state; a truncated
  // response is uncertain too. Return a new response to the existing workflow.
  const body = await response.text();
  if (response.status < 500) {
    if (response.headers.get("Content-Type")?.includes("application/json")) JSON.parse(body);
    pendingAuditRequest = null;
  }
  return new Response(body, { status: response.status, headers: response.headers });
}
window.addEventListener("beforeunload", (event) => {
  if (pendingAuditRequest) { event.preventDefault(); event.returnValue = ""; }
});
