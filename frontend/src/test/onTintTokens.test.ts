/// <reference types="node" />
import { readFileSync } from 'node:fs'
import tw from '../../tailwind.config.js?raw'
import { describe, expect, it } from 'vitest'
import { INVITE_STATUS_STYLES } from '../constants/invites'

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
