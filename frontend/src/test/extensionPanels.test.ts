import { describe, it, expect, vi, beforeEach, afterEach, type MockInstance } from 'vitest'
import {
  parseExtensionPanels,
  resetExtensionPanelWarnings,
  LIMITS,
} from '../components/Board/parseExtensionPanels'

const stats = (over: Record<string, unknown> = {}) => ({
  id: 's1',
  title: 'Stats',
  type: 'stats',
  data: { items: [{ label: 'A', value: 1 }] },
  ...over,
})
const table = (over: Record<string, unknown> = {}) => ({
  id: 't1',
  title: 'Table',
  type: 'table',
  data: { columns: [{ key: 'a', label: 'A' }], rows: [{ a: 1 }] },
  ...over,
})

let warn: MockInstance<typeof console.warn>
beforeEach(() => {
  resetExtensionPanelWarnings()
  warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => warn.mockRestore())

describe('parseExtensionPanels', () => {
  it.each([undefined, null, 'x', 5, {}])('returns [] for non-array %s', (v) => {
    expect(parseExtensionPanels(v)).toEqual([])
  })

  it('parses valid stats and table panels', () => {
    const out = parseExtensionPanels([stats(), table({ data: { columns: [{ key: 'a', label: 'A', align: 'right' }], rows: [{}] } })])
    expect(out).toHaveLength(2)
    expect(out[0]).toMatchObject({ type: 'stats', items: [{ label: 'A', value: 1 }] })
    expect(out[1]).toMatchObject({ type: 'table', columns: [{ key: 'a', align: 'right' }], rows: [{ a: null }] })
  })

  it.each([
    ['null entry', null],
    ['non-object entry', 'x'],
    ['missing id', stats({ id: undefined })],
    ['missing title', stats({ title: undefined })],
    ['missing type (legacy)', stats({ type: undefined })],
    ['unknown type', stats({ type: 'chart' })],
    ['empty id', stats({ id: '' })],
    ['id 101 chars', stats({ id: 'x'.repeat(101) })],
    ['unknown type with table-shaped data', table({ type: 'chart' })],
    ['type casing TABLE with table data', table({ type: 'TABLE' })],
    ['title too long', stats({ title: 'x'.repeat(LIMITS.titleMax + 1) })],
    ['description too long', stats({ description: 'x'.repeat(LIMITS.descriptionMax + 1) })],
    ['stats no items', stats({ data: { items: [] } })],
    ['stats too many items', stats({ data: { items: Array.from({ length: LIMITS.statsItemsMax + 1 }, () => ({ label: 'a', value: 1 })) } })],
    ['stats label too long', stats({ data: { items: [{ label: 'x'.repeat(81), value: 1 }] } })],
    ['stats value too long', stats({ data: { items: [{ label: 'a', value: 'x'.repeat(201) }] } })],
    ['stats hint too long', stats({ data: { items: [{ label: 'a', value: 1, hint: 'x'.repeat(201) }] } })],
    ['NaN value', stats({ data: { items: [{ label: 'a', value: NaN }] } })],
    ['Infinity value', stats({ data: { items: [{ label: 'a', value: Infinity }] } })],
    ['boolean value', stats({ data: { items: [{ label: 'a', value: true }] } })],
    ['data not object', stats({ data: [] })],
    ['table no columns', table({ data: { columns: [], rows: [] } })],
    ['table too many columns', table({ data: { columns: Array.from({ length: 13 }, (_, i) => ({ key: `k${i}`, label: 'a' })), rows: [] } })],
    ['table duplicate key', table({ data: { columns: [{ key: 'a', label: 'A' }, { key: 'a', label: 'B' }], rows: [] } })],
    ['table __proto__ column key', table({ data: { columns: [{ key: '__proto__', label: 'A' }], rows: [] } })],
    ['table constructor column key', table({ data: { columns: [{ key: 'constructor', label: 'A' }], rows: [] } })],
    ['table prototype column key', table({ data: { columns: [{ key: 'prototype', label: 'A' }], rows: [] } })],
    ['table column key 65 chars', table({ data: { columns: [{ key: 'k'.repeat(65), label: 'A' }], rows: [] } })],
    ['table column label 81 chars', table({ data: { columns: [{ key: 'a', label: 'x'.repeat(81) }], rows: [] } })],
    ['type casing Stats', stats({ type: 'Stats' })],
    ['table bad align', table({ data: { columns: [{ key: 'a', label: 'A', align: 'center' }], rows: [] } })],
    ['table too many rows', table({ data: { columns: [{ key: 'a', label: 'A' }], rows: Array.from({ length: 201 }, () => ({ a: 1 })) } })],
    ['table boolean cell', table({ data: { columns: [{ key: 'a', label: 'A' }], rows: [{ a: false }] } })],
    ['table object cell', table({ data: { columns: [{ key: 'a', label: 'A' }], rows: [{ a: {} }] } })],
    ['table non-object row', table({ data: { columns: [{ key: 'a', label: 'A' }], rows: [1] } })],
  ])('skips the whole panel: %s', (_name, entry) => {
    expect(parseExtensionPanels([entry])).toEqual([])
  })

  it('accepts values at the limits', () => {
    const out = parseExtensionPanels([
      stats({ id: 'x'.repeat(100), title: 'x'.repeat(120), data: { items: Array.from({ length: 24 }, () => ({ label: 'x'.repeat(80), value: 'x'.repeat(200), hint: 'x'.repeat(200) })) } }),
    ])
    expect(out).toHaveLength(1)
  })

  it('accepts table values at the upper limits', () => {
    const columns = Array.from({ length: 12 }, (_, i) => ({ key: i === 0 ? 'k'.repeat(64) : `c${i}`, label: 'x'.repeat(80) }))
    const rows = Array.from({ length: 200 }, () => ({}))
    const out = parseExtensionPanels([table({ description: 'd'.repeat(500), data: { columns, rows } })])
    expect(out).toHaveLength(1)
    expect(out[0]).toMatchObject({ description: 'd'.repeat(500) })
  })

  it('tolerates null optional fields (description, hint, align) as absent', () => {
    const out = parseExtensionPanels([
      stats({ description: null, data: { items: [{ label: 'a', value: 1, hint: null }] } }),
      table({ data: { columns: [{ key: 'a', label: 'A', align: null }], rows: [] } }),
    ])
    expect(out).toHaveLength(2)
    expect(out[0]).toEqual({ id: 's1', title: 'Stats', type: 'stats', items: [{ label: 'a', value: 1 }] })
    expect(out[1]).toMatchObject({ columns: [{ align: 'left' }] })
  })

  it('a later duplicate renders when the first one with that id was malformed', () => {
    const out = parseExtensionPanels([stats({ type: 'bogus' }), stats({ title: 'second' })])
    expect(out.map((p) => p.title)).toEqual(['second'])
  })

  it('logs one summary warning for panels over the cap, and names the index for an empty id', () => {
    const many = Array.from({ length: 30 }, (_, i) => stats({ id: `p${i}` }))
    parseExtensionPanels(many)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('over panel limit (10 dropped)')
    warn.mockClear()
    parseExtensionPanels([stats({ id: '' })])
    expect(String(warn.mock.calls[0][0])).toContain('skipped panel #0: invalid id')
  })

  it('first duplicate id wins', () => {
    const out = parseExtensionPanels([stats({ title: 'first' }), table({ id: 's1', title: 'second' })])
    expect(out).toHaveLength(1)
    expect(out[0].title).toBe('first')
  })

  it('ignores extra fields at every level', () => {
    const out = parseExtensionPanels([stats({ evil: 1, className: 'x', data: { items: [{ label: 'a', value: 1, color: 'red', extra: 1 }], more: 2 } })])
    expect(out[0]).toEqual({ id: 's1', title: 'Stats', type: 'stats', items: [{ label: 'a', value: 1 }] })
  })

  it('renders at most 20 panels', () => {
    const many = Array.from({ length: 25 }, (_, i) => stats({ id: `p${i}` }))
    expect(parseExtensionPanels(many)).toHaveLength(LIMITS.maxPanels)
  })

  it('a bad panel does not affect its neighbors', () => {
    const out = parseExtensionPanels([null, stats(), { id: 'z' }])
    expect(out.map((p) => p.id)).toEqual(['s1'])
  })

  it('warns once per panel+reason across parses, and never logs title or data', () => {
    const bad = { id: 'bad', title: 'SECRET-TITLE', type: 'stats', data: { items: [{ label: 'SECRET-DATA', value: NaN }] } }
    parseExtensionPanels([bad, null])
    parseExtensionPanels([bad, null])
    expect(warn).toHaveBeenCalledTimes(2)
    const logged = warn.mock.calls.map((c) => String(c[0])).join('\n')
    expect(logged).toContain('[extension_panels] skipped panel bad: invalid item value')
    expect(logged).toContain('skipped panel #1')
    expect(logged).not.toContain('SECRET')
  })
})
