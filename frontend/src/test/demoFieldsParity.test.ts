// #1664: `demo_mode` and `demo_next_reset_at` live on both `User` (the
// /auth/user/ identity object) and `SiteConfig` (/auth/site-config/). The
// duplication is intentional; this compile-time assertion (checked by
// `tsc -p tsconfig.app.json`) fails the build if the two types drift.
import { describe, expect, it } from "vitest";
import type { SiteConfig, User } from "../types";

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Assert<T extends true> = T;

// Optionality is allowed to differ (SiteConfig always sends demo_mode); the
// value types may not.
type DemoMode = Assert<Equal<NonNullable<User["demo_mode"]>, SiteConfig["demo_mode"]>>;
type DemoNextReset = Assert<
  Equal<NonNullable<User["demo_next_reset_at"]> | null, NonNullable<SiteConfig["demo_next_reset_at"]> | null>
>;
export type DemoParity = [DemoMode, DemoNextReset];

describe("User / SiteConfig demo_* field parity (#1664)", () => {
  it("is enforced at compile time", () => {
    const ok: DemoParity = [true, true];
    expect(ok).toEqual([true, true]);
  });
});
