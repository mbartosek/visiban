/// <reference types="node" />
import { readFileSync } from 'node:fs'
import tw from '../../tailwind.config.js?raw'
import { describe, expect, it } from 'vitest'
import { INVITE_STATUS_STYLES } from '../constants/invites'
import { AGING_STAGE_CLASSES } from '../utils/agingTint'

// #1550: every on-tint text token must be declared in both theme blocks of index.css and exposed
// to Tailwind. Read from disk: Vitest stubs CSS imports (`?raw` on a .css file comes back empty).
const css = readFileSync('src/index.css', 'utf8')
const darkBlock = css.slice(css.indexOf(':root,'), css.indexOf('[data-theme="light"] {'))
const lightBlock = css.slice(css.indexOf('[data-theme="light"] {'))
const tokens = ['danger', 'warning', 'success', 'info', 'muted', 'urgent']

describe('on-tint text tokens', () => {
  it.each(tokens)('%s-on-tint is declared in both the dark and light theme blocks', (t) => {
    expect(darkBlock).toContain(`--${t}-on-tint:`)
    expect(lightBlock).toContain(`--${t}-on-tint:`)
    if (t === 'urgent') {
      expect(darkBlock).toContain('--urgent:')
      expect(lightBlock).toContain('--urgent:')
    }
  })

  it.each(tokens)('%s-on-tint is exposed in the tailwind config', (t) => {
    expect(tw).toContain(`"${t}-on-tint": "rgb(var(--${t}-on-tint) / <alpha-value>)"`)
  })
})

describe('INVITE_STATUS_STYLES', () => {
  it('uses on-tint text and never a high-alpha same-hue fill', () => {
    expect(INVITE_STATUS_STYLES.pending).toBe('bg-success/20 text-success-on-tint')
    expect(INVITE_STATUS_STYLES.expired).toBe('bg-danger/20 text-danger-on-tint')
    expect(INVITE_STATUS_STYLES.revoked).toContain('line-through')
    expect(INVITE_STATUS_STYLES.used).not.toContain('line-through')
    for (const cls of Object.values(INVITE_STATUS_STYLES)) expect(cls).not.toMatch(/\/(40|50|60)/)
  })
})

// #1594: pin the contrast claimed in index.css / CLAUDE.md for the urgent pair on its /20 fill.
const rgbOf = (block: string, name: string): number[] => {
  const line = block.split('\n').find((l) => l.trim().startsWith(`--${name}:`))
  if (!line) throw new Error(`--${name} missing`)
  return line.slice(line.indexOf(':') + 1).split(';')[0].trim().split(/\s+/).map(Number)
}
const lum = (c: number[]) => {
  const [r, g, b] = c.map((v) => {
    const x = v / 255
    return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4
  })
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}
const ratio = (a: number[], b: number[]) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

describe('urgent-on-tint contrast (#1594)', () => {
  it.each([
    ['dark', darkBlock, 9.4],
    ['light', lightBlock, 6.7],
  ])('%s theme: text holds >= 4.5:1 on the /20 fill over surface and sunken', (_n, block, pinned) => {
    const fill = rgbOf(block, 'urgent')
    const text = rgbOf(block, 'urgent-on-tint')
    for (const surf of ['surface', 'surface-sunken']) {
      const bg = rgbOf(block, surf)
      const tint = fill.map((v, i) => 0.2 * v + 0.8 * bg[i])
      const r = ratio(text, tint)
      expect(r).toBeGreaterThanOrEqual(4.5)
      expect(r).toBeGreaterThanOrEqual(pinned)
    }
  })
})

// #1841: pin the card aging ramp contrast recorded in frontend/CLAUDE.md § Card aging ramp.
// The overlay is `bg-<hue>/<alpha>` over the card's bg-surface; the card body (title and
// urgency badge) renders at the stage's opacity, so its effective color is the text blended
// toward that composite. Each stage lists the text tokens that may sit on it.
describe('card aging ramp contrast (#1841)', () => {
  // Hue, alpha and opacity are parsed from the real stage classes, so changing a
  // stage's fill or opacity re-runs this check against the new values.
  const parse = (stage: keyof typeof AGING_STAGE_CLASSES) => {
    const { overlayClass, bodyOpacityClass } = AGING_STAGE_CLASSES[stage]
    const fill = /\bbg-([a-z-]+)\/(\d+)\b/.exec(overlayClass ?? '')
    const op = /^opacity-(\d+)$/.exec(bodyOpacityClass)
    if (!fill || !op) throw new Error(`unparseable classes for ${stage}`)
    return { hue: fill[1], alpha: Number(fill[2]) / 100, opacity: Number(op[1]) / 100 }
  }
  type Stage = [name: string, hue: string, alpha: number, opacity: number, texts: string[]]
  const withTexts = (stage: 'warning' | 'stale' | 'stale2x' | 'stale3x', texts: string[]): Stage => {
    const { hue, alpha, opacity } = parse(stage)
    return [stage, hue, alpha, opacity, texts]
  }
  const stages: Stage[] = [
    withTexts('warning', ['fg', 'warning-on-tint', 'danger-on-tint', 'info-on-tint']),
    withTexts('stale', ['fg', 'warning-on-tint', 'danger-on-tint', 'info-on-tint']),
    withTexts('stale2x', ['fg', 'danger-on-tint', 'info-on-tint']),
    withTexts('stale3x', ['fg', 'danger-on-tint', 'info-on-tint']),
  ]
  const blend = (top: number[], bottom: number[], a: number) => top.map((v, i) => a * v + (1 - a) * bottom[i])

  it.each(stages)('%s: card title and badge text hold >= 4.5:1 in both themes', (_n, hue, alpha, opacity, texts) => {
    for (const block of [darkBlock, lightBlock]) {
      const tint = blend(rgbOf(block, hue), rgbOf(block, 'surface'), alpha)
      for (const t of texts) {
        expect(ratio(blend(rgbOf(block, t), tint, opacity), tint)).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  it('amber warning-on-tint is sub-AA on the red stages, which is why due-soon moves to danger-on-tint there', () => {
    const { hue, alpha, opacity } = parse('stale3x')
    const tint = blend(rgbOf(lightBlock, hue), rgbOf(lightBlock, 'surface'), alpha)
    expect(ratio(blend(rgbOf(lightBlock, 'warning-on-tint'), tint, opacity), tint)).toBeLessThan(4.5)
  })

  it.each([
    ['dark', darkBlock, 3.2],
    ['light', lightBlock, 6.9],
  ])('%s theme: the 3x inset ring holds >= 3:1 against its own fill', (_n, block, pinned) => {
    const sev = rgbOf(block, 'stale-severe')
    const r = ratio(sev, blend(sev, rgbOf(block, 'surface'), parse('stale3x').alpha))
    expect(r).toBeGreaterThanOrEqual(3)
    expect(r).toBeGreaterThanOrEqual(pinned)
  })

  it('stale-severe is declared in both themes and exposed to tailwind', () => {
    expect(darkBlock).toContain('--stale-severe:')
    expect(lightBlock).toContain('--stale-severe:')
    expect(tw).toContain('"stale-severe": "rgb(var(--stale-severe) / <alpha-value>)"')
  })
})

// #1857: leftovers missed by the #1550 / #1237 / #831 sweeps. Source-level guards so they stay fixed.
describe('design-system leftovers (#1857)', () => {
  const read = (p: string) => readFileSync(p, 'utf8')

  it.each(['src/components/Board/BoardView.tsx', 'src/pages/GroupDetail.tsx'])(
    '%s passes on-tint text to JoinedNotice children',
    (p) => {
      const src = read(p)
      expect(src).toContain('<JoinedNotice')
      expect(src).not.toMatch(/<strong className="text-success">/)
      expect(src).toContain('text-success-on-tint')
    },
  )

  it('RoleInfoTooltip trigger respects the 12px typography floor', () => {
    const src = read('src/components/Common/RoleInfoTooltip.tsx')
    expect(src).not.toMatch(/text-\[(\d|1[01])px\]/)
    expect(src).toContain('text-xs')
  })

  it('ActivityTabPanel has no raw hex colors and its tokens are declared in both themes', () => {
    expect(read('src/components/Card/ActivityTabPanel.tsx')).not.toMatch(/["'`]#[0-9a-fA-F]{3,8}["'`]/)
    for (const t of ['activity-date', 'activity-assign']) {
      expect(darkBlock).toContain(`--${t}:`)
      expect(lightBlock).toContain(`--${t}:`)
      expect(tw).toContain(`"${t}": "rgb(var(--${t}) / <alpha-value>)"`)
    }
  })
})
