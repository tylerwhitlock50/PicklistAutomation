import { expect, it } from "vitest";
import { effectivePolicy } from "./admin-policy";

it("shows inherited customer values while retaining explicit false, zero and null overrides", () => {
  const defaults = {
    accumulate: true,
    target_guns: 20,
    sweep_weekday: 4,
    mix_orders: true,
  };
  expect(effectivePolicy(defaults, {})).toMatchObject({
    account_type: "major",
    target_guns: 20,
    release_cadence: "threshold",
    mix_orders: true,
  });
  expect(
    effectivePolicy(defaults, {
      accumulate: false,
      target_guns: 0,
      sweep_weekday: null,
      mix_orders: false,
    }),
  ).toMatchObject({
    account_type: "standard",
    target_guns: 0,
    release_cadence: "completion",
    sweep_weekday: null,
    mix_orders: false,
  });
  expect(defaults).toEqual({
    accumulate: true,
    target_guns: 20,
    sweep_weekday: 4,
    mix_orders: true,
  });
});

it("shows the release engine defaults for imported legacy policy fields", () => {
  expect(
    effectivePolicy(undefined, { accumulate: true, min_guns: 15 }),
  ).toMatchObject({
    account_type: "major",
    target_guns: 15,
    release_cadence: "threshold",
    mix_orders: false,
  });
});
