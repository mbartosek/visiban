// @vitest-environment node
/// <reference types="node" />
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'

// #1854: native checkbox/radio accents must use the theme-aware `accent-primary` token, never a raw
// Tailwind palette value, and a visible native control must set it (there is no @tailwindcss/forms,
// so an unset accent renders the default browser blue). See frontend/CLAUDE.md § Native checkbox and
// radio accent. Source scan, independent of the working directory.
const SRC = resolve(fileURLToPath(new URL('..', import.meta.url)))
const TEST_DIR = join(SRC, 'test')

function walk(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) return p === TEST_DIR ? [] : walk(p)
    return /\.tsx?$/.test(p) ? [p] : []
  })
}

const files = walk(SRC)
const read = (f: string) => readFileSync(f, 'utf8')

// Any accent-* utility other than accent-primary: palette shades, bare colors, arbitrary values.
const RAW_ACCENT = /(?<![\w-])accent-(?!primary(?![\w-]))(?:\[|[a-z]+-\d{1,3}(?![\w])|black\b|white\b)/

// Visible native checkbox/radio inputs (sr-only custom controls are exempt).
const NATIVE_INPUT = /<input\b[\s\S]*?\/>/g

describe('native control accent', () => {
  it('no source file uses a raw-palette accent-* class', () => {
    expect(files.filter((f) => RAW_ACCENT.test(read(f)))).toEqual([])
  })

  it('the regex flags raw forms and allows accent-primary', () => {
    for (const bad of ['accent-green-500', 'accent-gray-50', 'accent-[#16a34a]', 'accent-black', 'accent-white', 'accent-red-500/50']) {
      expect(RAW_ACCENT.test(`class="${bad}"`)).toBe(true)
    }
    for (const ok of ['accent-primary', 'text-accent-violet', 'bg-accent-violet/20']) {
      expect(RAW_ACCENT.test(`class="${ok}"`)).toBe(false)
    }
  })

  it('every visible native checkbox/radio input sets accent-primary', () => {
    const offenders: string[] = []
    for (const f of files.filter((p) => p.endsWith('.tsx'))) {
      const src = read(f)
      for (const m of src.matchAll(NATIVE_INPUT)) {
        const tag = m[0]
        if (/type=["'{]\s*["']?(checkbox|radio)/.test(tag) && !/sr-only|accent-primary/.test(tag)) {
          offenders.push(`${f}:${src.slice(0, m.index).split('\n').length}`)
        }
      }
    }
    expect(offenders).toEqual([])
  })

  it('the checklist item checkbox uses accent-primary', () => {
    const line = read(join(SRC, 'components/Card/CardDetail.tsx'))
      .split('\n')
      .find((l) => l.includes('shrink-0') && l.includes('w-3.5 h-3.5 rounded'))
    expect(line).toContain('accent-primary')
  })
})
