import { describe, it, expect } from 'vitest'
import { isSafeHttpUrl } from '../utils/safeUrl'

describe('isSafeHttpUrl (#1087)', () => {
  it.each([
    'https://github.com/acme/widgets/issues/42',
    'http://example.com',
    'HTTPS://EXAMPLE.COM/Path?q=1#frag',
  ])('accepts %s', (u) => {
    expect(isSafeHttpUrl(u)).toBe(true)
  })

  it.each([
    'javascript:alert(1)',
    'JaVaScRiPt:alert(1)',
    '  javascript:alert(1)',
    '\tjavascript:alert(1)',
    '\u0001javascript:alert(1)',
    'java\nscript:alert(1)',
    'data:text/html,<script>alert(1)</script>',
    'DATA:text/html;base64,AAAA',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    'ftp://example.com',
    '//evil.example.com/x',
    '/relative/path',
    'example.com',
    'garbage',
    'https://',
    'https://github.com@evil.example.com/x',
    'https://user:pw@example.com',
    ' https://example.com',
    'https://example.com ',
    'https://exa\u0000mple.com',
    '',
  ])('rejects %j', (u) => {
    expect(isSafeHttpUrl(u)).toBe(false)
  })

  it('rejects null, undefined, and non-strings', () => {
    expect(isSafeHttpUrl(null)).toBe(false)
    expect(isSafeHttpUrl(undefined)).toBe(false)
    expect(isSafeHttpUrl(42)).toBe(false)
    expect(isSafeHttpUrl({})).toBe(false)
  })
})
