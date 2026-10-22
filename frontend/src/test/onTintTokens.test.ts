/// <reference types="node" />
import { readFileSync } from 'node:fs'
import tw from '../../tailwind.config.js?raw'
import { describe, expect, it } from 'vitest'
import { INVITE_STATUS_STYLES } from '../constants/invites'
import { AGING_STAGE_CLASSES } from '../utils/agingTint'
import { CHOICE_COLORS } from '../constants/choiceColors'

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

// Hue, alpha and opacity are parsed from the real stage classes, so changing a
// stage's fill or opacity re-runs these checks against the new values.
const parse = (stage: keyof typeof AGING_STAGE_CLASSES) => {
  const { overlayClass, bodyOpacityClass } = AGING_STAGE_CLASSES[stage]
  const fill = /\bbg-([a-z-]+)\/(\d+)\b/.exec(overlayClass ?? '')
  const op = /^opacity-(\d+)$/.exec(bodyOpacityClass)
  if (!fill || !op) throw new Error(`unparseable classes for ${stage}`)
  return { hue: fill[1], alpha: Number(fill[2]) / 100, opacity: Number(op[1]) / 100 }
}
const blend = (top: number[], bottom: number[], a: number) => top.map((v, i) => a * v + (1 - a) * bottom[i])

// #1841: pin the card aging ramp contrast recorded in frontend/CLAUDE.md § Card aging ramp.
// The overlay is `bg-<hue>/<alpha>` over the card's bg-surface; the card body (title and
// urgency badge) renders at the stage's opacity, so its effective color is the text blended
// toward that composite. Each stage lists the text tokens that may sit on it.
describe('card aging ramp contrast (#1841)', () => {
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


// #1844: metadata text on a tinted card face. CardItem puts AGING_STAGE_CLASSES[stage].metaClass
// (`card-aging-meta`) on the metadata row; index.css rescopes the row's text tokens to on-tint values
// there. Everything below is derived from the real CSS and stage classes, and the ratios are the ones
// recorded in frontend/CLAUDE.md § Card aging ramp.
describe('card metadata on an aging tint (#1844)', () => {
  const scopeStart = css.indexOf('.card-aging-meta {')
  const scope = css.slice(scopeStart, css.indexOf('}', scopeStart))
  const remap: Record<string, string> = Object.fromEntries(
    [...scope.matchAll(/--([a-z-]+):\s*var\(--([a-z-]+)\);/g)].map((m) => [m[1], m[2]]),
  )
  const resolve = (block: string, token: string) => rgbOf(block, remap[token] ?? token)
  // Every plain text token the metadata row renders, in CardItem and in the child components it
  // mounts there (CustomFieldLink, MultiSelectChips). The source scan below keeps this list honest.
  const META = ['fg-secondary', 'fg-tertiary', 'fg-muted', 'fg-faint', 'info', 'danger', 'success']
  const TINTED = ['warning', 'stale', 'stale2x', 'stale3x'] as const
  const themes = [
    ['dark', darkBlock],
    ['light', lightBlock],
  ] as const
  // Pinned floor per stage: the lowest ratio among META after the rescope.
  const floors: Record<(typeof TINTED)[number], [dark: number, light: number]> = {
    warning: [5.59, 6.41],
    stale: [4.78, 5.27],
    stale2x: [4.97, 5.06],
    stale3x: [5.08, 4.55],
  }
  const metaRatio = (block: string, stage: (typeof TINTED)[number], color: number[]) => {
    const { hue, alpha, opacity } = parse(stage)
    const tint = blend(rgbOf(block, hue), rgbOf(block, 'surface'), alpha)
    return ratio(blend(color, tint, opacity), tint)
  }

  it('rescopes exactly the metadata text tokens that fall below 4.5:1 on a tint', () => {
    expect(scopeStart).toBeGreaterThan(-1)
    expect(remap).toEqual({
      'fg-tertiary': 'muted-on-tint',
      'fg-muted': 'muted-on-tint',
      'fg-faint': 'muted-on-tint',
      info: 'info-on-tint',
      danger: 'danger-on-tint',
      success: 'success-on-aging-tint',
    })
  })

  it('success-on-aging-tint is declared in both theme blocks and kept out of tailwind', () => {
    expect(darkBlock).toContain('--success-on-aging-tint:')
    expect(lightBlock).toContain('--success-on-aging-tint:')
    expect(tw).not.toContain('success-on-aging-tint')
  })

  // fg-secondary is not rescoped; frontend/CLAUDE.md says it currently renders the same tone as
  // muted-on-tint. If either token moves, this fails and the doc sentence must be revisited.
  it('fg-secondary currently equals muted-on-tint in both themes', () => {
    for (const [, block] of themes) expect(rgbOf(block, 'fg-secondary')).toEqual(rgbOf(block, 'muted-on-tint'))
  })

  it.each(TINTED.flatMap((st) => themes.map(([n, b], i) => [st, n, b, i] as const)))(
    '%s, %s theme: every metadata text token holds >= 4.5:1',
    (stage, _n, block, i) => {
      const worst = Math.min(...META.map((t) => metaRatio(block, stage, resolve(block, t))))
      expect(worst).toBeGreaterThanOrEqual(4.5)
      expect(worst).toBeGreaterThanOrEqual(floors[stage][i] - 0.005)
    },
  )

  it('without the rescope every remapped token is sub-AA on at least one stage, in each theme it fails', () => {
    for (const t of Object.keys(remap)) {
      const worst = Math.min(...themes.flatMap(([, b]) => TINTED.map((st) => metaRatio(b, st, rgbOf(b, t)))))
      expect(worst, t).toBeLessThan(4.5)
    }
    // The ratios the issue reported for fg-tertiary at the 3x stage.
    expect(metaRatio(darkBlock, 'stale3x', rgbOf(darkBlock, 'fg-tertiary'))).toBeCloseTo(3.85, 2)
    expect(metaRatio(lightBlock, 'stale3x', rgbOf(lightBlock, 'fg-tertiary'))).toBeCloseTo(3.88, 2)
  })

  it('fg-muted is already sub-AA on an untinted dark card, so no overlay or opacity tuning could fix it', () => {
    expect(ratio(rgbOf(darkBlock, 'fg-muted'), rgbOf(darkBlock, 'surface'))).toBeCloseTo(3.07, 2)
  })

  it('choice badges and multi-select chips keep >= 4.5:1 under the stage opacity', () => {
    const hex = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16))
    for (const stage of TINTED) {
      const { hue, alpha, opacity } = parse(stage)
      for (const [n, block] of themes) {
        const tint = blend(rgbOf(block, hue), rgbOf(block, 'surface'), alpha)
        const onTint = (fg: number[], bg: number[]) => ratio(blend(fg, tint, opacity), blend(bg, tint, opacity))
        for (const [key, spec] of Object.entries(CHOICE_COLORS)) {
          const pair = spec[n]
          expect(onTint(hex(pair.fg), hex(pair.bg)), `${stage} ${n} ${key}`).toBeGreaterThanOrEqual(4.5)
        }
        // MultiSelectChips: bg-surface-hover text-fg-secondary (fg-secondary is not rescoped).
        expect(onTint(resolve(block, 'fg-secondary'), rgbOf(block, 'surface-hover'))).toBeGreaterThanOrEqual(4.5)
      }
    }
  })

  // A new metadata element in a token outside META (for example plain `text-warning`) would not be
  // rescoped, so it fails here until it is either rescoped or listed with a reason.
  it('every text token on the card face is covered by the rescope or listed with a reason', () => {
    const allowed = new Set([
      ...META,
      'fg', // card title and the aging tooltip (bg-sunken); pinned in the #1841 ramp test above
    ])
    const files = ['CardItem', 'CustomFieldLink', 'MultiSelectChips', 'ChoiceValue']
    for (const f of files) {
      const src = readFileSync(`src/components/Card/${f}.tsx`, 'utf8')
      for (const m of src.matchAll(/\btext-(fg(?:-[a-z]+)?|info|danger|success|warning|muted|urgent)(-on-tint)?\b/g)) {
        if (m[2]) continue // explicit on-tint tokens (urgency badge), pinned in the #1841 test above
        expect(allowed.has(m[1]), `${f}.tsx uses text-${m[1]}`).toBe(true)
      }
    }
  })

  // The rescope rebinds the token, not just its text utility, so a fill, border, ring or decoration
  // in a rebound token inside the row would also shift. Each such use must be listed here with a reason.
  it('every non-text utility on a rebound token is listed with a reason', () => {
    const intended = new Set([
      'border-fg-tertiary', // dotted quick-edit underline under a chip value: follows the rescoped text tone
      'bg-info', // selected-card fill on the card root, outside the metadata row (and dropped under a tint)
    ])
    // Literal regex (no RegExp constructor); the token list must match the rescope exactly.
    const rebound = ['fg-tertiary', 'fg-muted', 'fg-faint', 'info', 'danger', 'success']
    expect([...rebound].sort()).toEqual(Object.keys(remap).sort())
    const re = /\b(?:bg|border|ring|outline|fill|stroke|decoration|divide|shadow|from|via|to|accent|caret|placeholder)-(?:fg-tertiary|fg-muted|fg-faint|info|danger|success)(?![a-z-])/g
    for (const f of ['CardItem', 'CustomFieldLink', 'MultiSelectChips', 'ChoiceValue']) {
      const src = readFileSync(`src/components/Card/${f}.tsx`, 'utf8')
      for (const [cls] of src.matchAll(re)) expect(intended.has(cls), `${f}.tsx uses ${cls}`).toBe(true)
    }
  })
})
