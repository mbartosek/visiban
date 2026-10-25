import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'

// Real api/auth (so the request sharing is exercised); only the HTTP client is mocked.
vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}))

import client from '../api/client'
import { resetSiteConfigShare } from '../api/auth'
import LoginPage from '../components/Auth/LoginPage'
import { useInstanceFlagsState } from '../hooks/useInstanceFlagsState'
import { renderHook } from '@testing-library/react'

const mockGet = client.get as ReturnType<typeof vi.fn>

describe('login page load makes one site-config request (#1913)', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    resetSiteConfigShare()
    mockGet.mockImplementation((url: string) => {
      if (url.includes('providers')) {
        return Promise.resolve({ data: { google: false, github: false, gitlab: false, oidc: false, oidc_name: null } })
      }
      return Promise.resolve({
        data: { registration_open: true, registration_mode: 'open', demo_mode: false, demo_login: null, invite_email_available: false },
      })
    })
  })

  it('the app-level flags hook and LoginPage share a single GET /auth/site-config/', async () => {
    const hook = renderHook(() => useInstanceFlagsState(false))
    await waitFor(() => expect(hook.result.current.loaded).toBe(true))
    render(<MemoryRouter><LoginPage onLogin={vi.fn()} /></MemoryRouter>)
    await screen.findByRole('button', { name: /sign in|log in/i })
    const siteConfigCalls = mockGet.mock.calls.filter(([url]) => String(url).includes('site-config'))
    expect(siteConfigCalls).toHaveLength(1)
  })
})
