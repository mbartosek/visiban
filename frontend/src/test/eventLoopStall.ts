/**
 * Reproduces, on demand, the event-loop ordering a loaded CI runner produces
 * by accident (#1319, occurrence 4).
 *
 * A render that is not wrapped in act() (a fetch resolving, say) commits in
 * one Scheduler task and runs its passive effects (useEffect) in a later
 * setImmediate task. waitFor() finds the new element from a MutationObserver
 * right after the commit, then drains with a setTimeout(0). On a fast machine
 * that timer is not ripe yet, so the effects run first; on a slow one it is,
 * so the test resumes, and fires its next event, in the window between the
 * commit and the effects.
 *
 * Call this from inside a waitFor() callback at the moment it finds the
 * element. It burns `ms` of CPU a few microtask hops later, after waitFor has
 * queued its trailing timer, which makes that timer ripe before the effect
 * task runs. Assert something that proves the effects have not run yet (a
 * mount fetch not called, for example), so a future change to waitFor or Node
 * that closes the window fails loudly instead of passing without testing it.
 */
export function stallEventLoopAfterWaitFor(ms = 5, hops = 20): void {
  let step = () => {
    const end = performance.now() + ms
    while (performance.now() < end) { /* deliberately busy */ }
  }
  for (let i = 0; i < hops; i++) {
    const next = step
    step = () => queueMicrotask(next)
  }
  step()
}
