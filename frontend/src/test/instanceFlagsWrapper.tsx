import type { ReactNode } from 'react'
import {
  DEFAULT_INSTANCE_FLAGS,
  InstanceFlagsContext,
  type InstanceFlags,
} from '../contexts/InstanceFlagsContext'

/** RTL `wrapper` that supplies instance flags the way App does (#1913). */
export function flagsWrapper(flags: Partial<InstanceFlags>) {
  const value = { ...DEFAULT_INSTANCE_FLAGS, ...flags }
  return function FlagsWrapper({ children }: { children: ReactNode }) {
    return <InstanceFlagsContext.Provider value={value}>{children}</InstanceFlagsContext.Provider>
  }
}
