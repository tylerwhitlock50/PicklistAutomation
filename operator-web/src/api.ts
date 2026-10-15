export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}
export async function api<T>(
  path: string,
  body?: unknown,
  csrf?: string,
  id?: string,
): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    method: body === undefined ? "GET" : "POST",
    credentials: "same-origin",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      ...(csrf ? { "X-CSRF-Token": csrf } : {}),
      ...(id ? { "Idempotency-Key": id } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const data = await response.json();
  if (!response.ok)
    throw new ApiError(data.message || "Request failed.", response.status);
  return data as T;
}
export function packedQuantity(
  work: { packages: { contents: { line_id: string; quantity: string }[] }[] },
  lineId: string,
) {
  return work.packages.reduce(
    (sum, p) =>
      sum +
      p.contents
        .filter((i) => i.line_id === lineId)
        .reduce((s, i) => s + Number(i.quantity), 0),
    0,
  );
}
