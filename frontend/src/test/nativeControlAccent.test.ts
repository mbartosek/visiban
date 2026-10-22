/// <reference types="node" />
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join } from 'node:path'
import { describe, expect, it } from 'vitest'

// #1854: native checkbox/radio accents must use the theme-aware `accent-primary` token, never a raw
// Tailwind palette value (frontend/CLAUDE.md § Native checkbox and radio accent). Source scan.
function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) return name === 'test' ? [] : walk(p)
    return /\.tsx?$/.test(p) ? [p] : []
  })
}

const RAW_ACCENT = /\baccent-(?!primary\b)[a-z]+-\d{2,3}\b/

describe('native control accent', () => {
  it('no source file uses a raw-palette accent-* class', () => {
    const offenders = walk('src').filter((f) => RAW_ACCENT.test(readFileSync(f, 'utf8')))
    expect(offenders).toEqual([])
  })

  it('the checklist item checkbox uses accent-primary', () => {
    const src = readFileSync('src/components/Card/CardDetail.tsx', 'utf8')
    expect(src).toMatch(/w-3\.5 h-3\.5 rounded accent-primary shrink-0/)
  })
})
