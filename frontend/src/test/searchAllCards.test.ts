import { describe, it, expect, vi, beforeEach } from 'vitest'

vi.mock('../api/client', () => ({ default: { get: vi.fn() } }))

import client from '../api/client'
import { searchAllCards, CARD_SEARCH_MAX_PAGES, CARD_SEARCH_PAGE_SIZE } from '../api/cards'

const get = client.get as ReturnType<typeof vi.fn>
const rows = (start: number, n: number) => Array.from({ length: n }, (_, i) => ({ id: start + i }))
const resp = (data: unknown[], truncated: boolean, total: number) => ({
  data,
  headers: { 'x-total-count': String(total), 'x-truncated': String(truncated) },
})

describe('searchAllCards', () => {
  beforeEach(() => vi.clearAllMocks())

  it('returns a single page when not truncated', async () => {
    get.mockResolvedValueOnce(resp(rows(1, 3), false, 3))
    const r = await searchAllCards(1, 'x')
    expect(r).toEqual({ cards: rows(1, 3), truncated: false, total: 3 })
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('pages with limit/offset until X-Truncated is false', async () => {
    get
      .mockResolvedValueOnce(resp(rows(1, 200), true, 250))
      .mockResolvedValueOnce(resp(rows(201, 50), false, 250))
    const r = await searchAllCards(7, 'x')
    expect(r.cards).toHaveLength(250)
    expect(r.truncated).toBe(false)
    expect(get.mock.calls[0][1].params).toMatchObject({ search: 'x', limit: 200, offset: 0 })
    expect(get.mock.calls[1][1].params).toMatchObject({ offset: 200 })
  })

  it('stops at the page ceiling and flags truncation', async () => {
    get.mockImplementation(async (_u: string, cfg: { params: { offset: number } }) =>
      resp(rows(cfg.params.offset + 1, CARD_SEARCH_PAGE_SIZE), true, 99999))
    const r = await searchAllCards(1, 'x')
    expect(get).toHaveBeenCalledTimes(CARD_SEARCH_MAX_PAGES)
    expect(r.truncated).toBe(true)
    expect(r.total).toBe(99999)
    expect(r.cards).toHaveLength(CARD_SEARCH_MAX_PAGES * CARD_SEARCH_PAGE_SIZE)
  })

  it('stops on an empty page even if the server still says truncated', async () => {
    get.mockResolvedValueOnce(resp([], true, 10))
    const r = await searchAllCards(1, 'x')
    expect(r.truncated).toBe(true)
    expect(get).toHaveBeenCalledTimes(1)
  })

  it('returns partial matches flagged truncated when a later page fails', async () => {
    get
      .mockResolvedValueOnce(resp(rows(1, 200), true, 450))
      .mockRejectedValueOnce(Object.assign(new Error('Too Many Requests'), { name: 'AxiosError' }))
    const r = await searchAllCards(1, 'x')
    expect(r.cards).toHaveLength(200)
    expect(r.truncated).toBe(true)
    expect(r.total).toBe(450)
  })

  it('rejects when the first page fails', async () => {
    get.mockRejectedValueOnce(new Error('boom'))
    await expect(searchAllCards(1, 'x')).rejects.toThrow('boom')
  })

  it('rejects on abort even after page 1 succeeded', async () => {
    get
      .mockResolvedValueOnce(resp(rows(1, 200), true, 450))
      .mockRejectedValueOnce(Object.assign(new Error('canceled'), { name: 'CanceledError' }))
    await expect(searchAllCards(1, 'x')).rejects.toThrow('canceled')
  })
})
