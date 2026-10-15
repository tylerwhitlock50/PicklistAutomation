import type { Policy } from "./Admin";

// Match the release engine's fallback rules without saving inferred defaults as
// customer overrides. Editing one field must retain inheritance for other fields.
export function effectivePolicy(
  defaults: Policy | undefined,
  overrides: Policy,
): Policy {
  const p = { ...defaults, ...overrides };
  const target = p.target_guns ?? p.min_guns ?? 0;
  const account = p.account_type ?? (p.accumulate ? "major" : "standard");
  return {
    ...p,
    account_type: account,
    target_guns: target,
    release_cadence:
      p.release_cadence ??
      (target > 0 || p.sweep_weekday != null ? "threshold" : "completion"),
    mix_orders: p.mix_orders ?? account !== "major",
  };
}
