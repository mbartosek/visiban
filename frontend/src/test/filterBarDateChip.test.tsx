import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import FilterBar, { EMPTY_FILTER } from '../components/Board/FilterBar'
import type { BoardFull } from '../types'

// #1993 — the active-filter chip for a date custom field must follow the
// user's date_format, not a hardcoded MM/DD/YYYY.
const board = {
  id: 1,
  name: 'B',
  members: [],
  labels: [],
  columns: [],
  swimlanes: [],
  cards: [],
  custom_field_definitions: [
    { id: 7, name: 'Expected close', field_type: 'date', choices: [], help_text: '', position: 0, is_pinned: false },
  ],
} as unknown as BoardFull

function renderBar(userDateFormat?: string) {
  return render(
    <FilterBar
      board={board}
      filters={{ ...EMPTY_FILTER, customFields: { 7: { kind: 'date', equals: '2026-11-26' } }, visibleCustomFieldFilterIds: [7] }}
      onChange={vi.fn()}
      userDateFormat={userDateFormat}
    />,
  )
}

describe('FilterBar date custom-field chip', () => {
  it.each([
    ['DD/MM/YYYY', 'Expected close: 26/11/2026'],
    ['MM/DD/YYYY', 'Expected close: 11/26/2026'],
    [undefined, 'Expected close: 11/26/2026'],
  ])('renders with format %s', (fmt, text) => {
    renderBar(fmt)
    expect(screen.getAllByText(text, { exact: false }).length).toBeGreaterThan(0)
  })
})
