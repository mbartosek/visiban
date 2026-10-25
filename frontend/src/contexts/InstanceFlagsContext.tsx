import { createContext, useContext } from "react";
import type { SiteConfig } from "../types";

/**
 * Instance-wide flags the signed-in app reads from GET /api/v1/auth/site-config/
 * (#1913). They used to be read from the deprecated `User.maintenance_mode`,
 * `maintenance_message`, `demo_mode` and `demo_next_reset_at` copies (#1872).
 *
 * `git_lens_enabled` is deliberately not part of this: #1872 did not deprecate
 * its `User` copy.
 */
export type InstanceFlags = Pick<
  SiteConfig,
  "maintenance_mode" | "maintenance_message" | "demo_mode" | "demo_next_reset_at"
>;

/**
 * What a component sees before site-config has loaded, when the fetch fails,
 * or when it renders outside the provider: everything off. A failed fetch must
 * degrade to "no banner, normal controls" — the server still enforces
 * maintenance and the demo fence, so this only costs the explanatory UI.
 */
export const DEFAULT_INSTANCE_FLAGS: InstanceFlags = {
  maintenance_mode: false,
  maintenance_message: "",
  demo_mode: false,
  demo_next_reset_at: null,
};

export const InstanceFlagsContext = createContext<InstanceFlags>(DEFAULT_INSTANCE_FLAGS);

export function useInstanceFlags(): InstanceFlags {
  return useContext(InstanceFlagsContext);
}
