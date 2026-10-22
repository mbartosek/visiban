import { test, expect } from '@playwright/test'
import { routeAuth } from './helpers'
import { BOARD_FULL, CARD, CARD_STALE } from './fixtures/board'

test.describe('card aging indicator', () => {
  test('long-idle card renders the darkest aging stage with idle days', async ({ page }) => {
    await routeAuth(page)

    // Custom board fixture: include both a fresh card and a stale one so we can
    // assert the overlay appears on the stale card only.
    const board = { ...BOARD_FULL, cards: [CARD, CARD_STALE] }
    await page.route(`**/api/v1/boards/${BOARD_FULL.id}/full/`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(board) }),
    )
    await page.route(`**/api/v1/boards/${BOARD_FULL.id}/cards/`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ count: 2, results: [CARD, CARD_STALE] }) }),
    )
    await page.route(`**/api/v1/boards/${BOARD_FULL.id}/saved-filters/`, (route) =>
      route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify([]) }),
    )
    await page.routeWebSocket(`**/ws/boards/${BOARD_FULL.id}/`, (ws) => {
      ws.send(JSON.stringify({ event: 'connected', data: {} }))
    })

    await page.goto(`/boards/${BOARD_FULL.id}`)
    await expect(page.getByText(CARD_STALE.title).first()).toBeVisible({ timeout: 10_000 })

    // Locate the stale card's container and assert the overlay element exists.
    // CARD_STALE last moved 2026-04-01 against a 7-day threshold, so it is far
    // past 3x and sits in the darkest stage of the ramp (#1841): a
    // `div[aria-hidden="true"]` carrying `bg-stale-severe/20` plus an inset
    // ring, inside the card root (the card's outer wrapper uses `rounded-md`).
    // The ratio only grows with the wall clock, so the stage cannot drift down.
    const staleCard = page.getByText(CARD_STALE.title).first().locator('xpath=ancestor::div[contains(@class, "rounded-md")][1]')
    await expect(staleCard.locator('div.bg-stale-severe\\/20.ring-inset').first()).toBeVisible()

    // The badge states the idle days, not just "Stale".
    await expect(staleCard.getByText(/^Stale · \d+d$/)).toBeVisible()

    // Regression guard: the fresh card has no aging overlay at all.
    const freshCard = page.getByText(CARD.title).first().locator('xpath=ancestor::div[contains(@class, "rounded-md")][1]')
    await expect(freshCard.locator('div.pointer-events-none.inset-0')).toHaveCount(0)
  })
})
