import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render } from '@testing-library/react'
import { migrateLegacyKey } from '../utils/migrateLegacyKey'
import { ThemeProvider } from '../context/ThemeContext'

const OLD = 'legacy-key'
const NEW = 'user:prefs:new-key'

describe('migrateLegacyKey (#1855)', () => {
  beforeEach(() => localStorage.clear())
  afterEach(() => vi.restoreAllMocks())

  it('old only: copies to the new key and removes the old', () => {
    localStorage.setItem(OLD, 'true')
    migrateLegacyKey(OLD, NEW)
    expect(localStorage.getItem(NEW)).toBe('true')
    expect(localStorage.getItem(OLD)).toBeNull()
  })

  it('new only: leaves the new key untouched', () => {
    localStorage.setItem(NEW, 'keep')
    migrateLegacyKey(OLD, NEW)
    expect(localStorage.getItem(NEW)).toBe('keep')
    expect(localStorage.getItem(OLD)).toBeNull()
  })

  it('both: new wins and the old key is removed', () => {
    localStorage.setItem(OLD, 'stale')
    localStorage.setItem(NEW, 'fresh')
    migrateLegacyKey(OLD, NEW)
    expect(localStorage.getItem(NEW)).toBe('fresh')
    expect(localStorage.getItem(OLD)).toBeNull()
  })

  it('neither: writes nothing', () => {
    migrateLegacyKey(OLD, NEW)
    expect(localStorage.getItem(NEW)).toBeNull()
  })

  it('does not throw when storage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => migrateLegacyKey(OLD, NEW)).not.toThrow()
  })
})

describe('ThemeProvider legacy migration (#1855)', () => {
  beforeEach(() => {
    localStorage.clear()
    Object.defineProperty(window, 'matchMedia', {
      writable: true,
      value: vi.fn().mockReturnValue({
        matches: false,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
      }),
    })
  })
  afterEach(() => vi.restoreAllMocks())

  it('migrates visiban-theme to user:prefs:theme', () => {
    localStorage.setItem('visiban-theme', 'dark')
    render(<ThemeProvider><div /></ThemeProvider>)
    expect(localStorage.getItem('user:prefs:theme')).toBe('dark')
    expect(localStorage.getItem('visiban-theme')).toBeNull()
    expect(document.documentElement.dataset.theme).toBe('dark')
  })

  it('does not crash when storage throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    expect(() => render(<ThemeProvider><div /></ThemeProvider>)).not.toThrow()
  })
})
