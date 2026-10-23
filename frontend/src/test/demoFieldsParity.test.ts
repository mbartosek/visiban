// #1664 / #1872: the five instance flags (`git_lens_enabled`, `maintenance_mode`,
// `maintenance_message`, `demo_mode`, `demo_next_reset_at`) live on both `User`
// (the /auth/user/ identity object, frozen there) and `SiteConfig`
// (/auth/site-config/, where instance state belongs). The backend computes both
// from one helper; this compile-time assertion (checked by
// `tsc -p tsconfig.app.json`) fails the build if the two TypeScript types drift.
import { describe, expect, it } from "vitest";
import type { SiteConfig, User } from "../types";

type Equal<A, B> =
  (<T>() => T extends A ? 1 : 2) extends <T>() => T extends B ? 1 : 2 ? true : false;
type Assert<T extends true> = T;

// Optionality is allowed to differ (SiteConfig always sends these; User marks
// them optional); the value types may not.
type DemoMode = Assert<Equal<NonNullable<User["demo_mode"]>, SiteConfig["demo_mode"]>>;
type DemoNextReset = Assert<
  Equal<NonNullable<User["demo_next_reset_at"]> | null, NonNullable<SiteConfig["demo_next_reset_at"]> | null>
>;
type GitLens = Assert<Equal<NonNullable<User["git_lens_enabled"]>, SiteConfig["git_lens_enabled"]>>;
type MaintenanceMode = Assert<Equal<NonNullable<User["maintenance_mode"]>, SiteConfig["maintenance_mode"]>>;
type MaintenanceMessage = Assert<
  Equal<NonNullable<User["maintenance_message"]>, SiteConfig["maintenance_message"]>
>;
export type InstanceFlagParity = [DemoMode, DemoNextReset, GitLens, MaintenanceMode, MaintenanceMessage];

describe("User / SiteConfig instance-flag parity (#1664, #1872)", () => {
  it("is enforced at compile time", () => {
    const ok: InstanceFlagParity = [true, true, true, true, true];
    expect(ok).toEqual([true, true, true, true, true]);
  });
});
