import { describe, it, expect, vi, beforeEach, type Mock } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ImportBoardModal from '../components/Board/ImportBoardModal'
import { getSampleBoardFile, listSampleBoards } from '../api/boards'
import type { SampleBoardSummary } from '../types'

// #1949: an anchored file (a shipped sample, however it arrives) is shifted to
// the import day by default. The Dates group lets the user turn that off or
// pick another day behind "Adjust dates".

vi.mock('../api/boards', () => ({
  listSampleBoards: vi.fn(),
  getSampleBoardFile: vi.fn(),
}))

const mockList = listSampleBoards as Mock
const mockFile = getSampleBoardFile as Mock

const SAMPLE: SampleBoardSummary = {
  id: 'simple_kanban',
  title: 'Simple Kanban',
  description: 'Simple Kanban description.',
  swimlane_theme: 'team',
  card_count: 113,
  includes: ['labels', 'checklists', 'comments', 'history'],
  order: 1,
  schema_version: 2,
  date_anchor: '2026-03-15',
}

function today(): string {
  const d = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
}

const anchored = () =>
  new File([JSON.stringify({ schema_version: 2, date_anchor: '2026-03-15', name: 'B' })], 'simple_kanban.json', {
    type: 'application/json',
  })

async function renderModal(dateFormat?: string) {
  const onImport = vi.fn().mockResolvedValue(undefined)
  const user = userEvent.setup()
  render(<ImportBoardModal onImport={onImport} onCancel={vi.fn()} dateFormat={dateFormat} />)
  await screen.findAllByRole('button', { name: /^Use this sample: / })
  return { user, onImport }
}

async function upload(user: ReturnType<typeof userEvent.setup>, file: File) {
  await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement, file)
}

const moveDates = () => screen.getByRole('checkbox', { name: 'Move dates to today' })
const adjust = () => screen.getByRole('button', { name: 'Adjust dates' })

beforeEach(() => {
  mockList.mockReset().mockResolvedValue([SAMPLE])
  mockFile.mockReset().mockResolvedValue(new Blob(['{"name":"Simple Kanban"}'], { type: 'application/json' }))
})

describe('ImportBoardModal — Dates for a chosen file (#1949)', () => {
  it('shows the Dates group for a file with a date_anchor, checked, and sends nothing by default', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())

    expect(await screen.findByRole('group', { name: 'Dates' })).toBeInTheDocument()
    expect(moveDates()).toBeChecked()
    expect(screen.getByText('Dated around today, so the board does not open mostly overdue.')).toBeInTheDocument()
    // The disclosure starts closed: occasional users never see the date field.
    expect(adjust()).toHaveAttribute('aria-expanded', 'false')
    // No dangling IDREF while the panel is not mounted.
    expect(adjust()).not.toHaveAttribute('aria-controls')
    expect(screen.queryByLabelText('Date the board around')).not.toBeInTheDocument()
    // The date choice is part of what the Import button describes.
    expect(screen.getByRole('button', { name: 'Import' })).toHaveAttribute(
      'aria-describedby', 'import-summary import-dates-hint',
    )

    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    // The server shifts an anchored file by default, so the call is the plain two-argument one.
    expect(onImport.mock.calls[0]).toHaveLength(2)
  })

  it('unchecking sends shift_dates false and hides Adjust dates', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('checkbox', { name: 'Move dates to today' }))

    expect(screen.getByText('Dates are imported exactly as written in the file.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Adjust dates' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0][2]).toEqual({ shift_dates: false })
  })

  it('keeps include choices alongside the opt-out', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('checkbox', { name: 'Move dates to today' }))
    await user.click(screen.getByRole('checkbox', { name: 'Labels' }))
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0][2]).toEqual({
      labels: false, cards: true, comments: true, checklist: true, history: true, shift_dates: false,
    })
  })

  it('Adjust dates reveals a date field defaulting to today; an unchanged date is not sent', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))

    expect(adjust()).toHaveAttribute('aria-expanded', 'true')
    const input = screen.getByLabelText('Date the board around')
    expect(adjust()).toHaveAttribute('aria-controls', input.parentElement?.id)
    expect(input).toHaveValue(today())
    expect(input).toHaveAttribute('max', today())

    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0]).toHaveLength(2)
  })

  it('a changed date is sent as shift_dates_to and named in the hint', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))
    fireEvent.change(screen.getByLabelText('Date the board around'), { target: { value: '2026-06-01' } })

    // Shown in the user's date format (MM/DD/YYYY by default), sent as ISO.
    expect(screen.getByText('Dated around 06/01/2026 instead of today.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0][2]).toEqual({ shift_dates_to: '2026-06-01' })
  })

  it('names a custom day in the user\'s own date format', async () => {
    const { user } = await renderModal('DD/MM/YYYY')
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))
    fireEvent.change(screen.getByLabelText('Date the board around'), { target: { value: '2026-06-01' } })
    expect(screen.getByText('Dated around 01/06/2026 instead of today.')).toBeInTheDocument()
  })

  it('a typed day after today is refused inline and blocks the import', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))
    const input = screen.getByLabelText('Date the board around')
    expect(screen.getByRole('alert')).toHaveTextContent('')
    expect(input).not.toHaveAttribute('aria-invalid')
    fireEvent.change(input, { target: { value: '2999-01-01' } })

    expect(screen.getByRole('alert')).toHaveTextContent('Pick today or an earlier day.')
    expect(input).toHaveAttribute('aria-invalid', 'true')
    expect(input).toHaveAccessibleDescription('Pick today or an earlier day.')
    // The hint does not claim the refused day.
    expect(screen.getByText('Dated around today, so the board does not open mostly overdue.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Import' }))
    expect(onImport).not.toHaveBeenCalled()
    expect(input).toHaveFocus()

    fireEvent.change(input, { target: { value: '2026-06-01' } })
    expect(screen.getByRole('alert')).toHaveTextContent('')
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0][2]).toEqual({ shift_dates_to: '2026-06-01' })
  })

  it('a refused day does not block an import with shifting turned off', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))
    fireEvent.change(screen.getByLabelText('Date the board around'), { target: { value: '2999-01-01' } })
    await user.click(moveDates())
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0][2]).toEqual({ shift_dates: false })
  })

  it('does not read an over-limit file for an anchor', async () => {
    const { user } = await renderModal()
    const big = anchored()
    Object.defineProperty(big, 'size', { value: 10 * 1024 * 1024 + 1 })
    await upload(user, big)
    await screen.findByText(big.name)
    await new Promise((r) => setTimeout(r, 20))
    expect(screen.queryByRole('group', { name: 'Dates' })).not.toBeInTheDocument()
  })

  it('a cleared date falls back to today and is not sent', async () => {
    const { user, onImport } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))
    fireEvent.change(screen.getByLabelText('Date the board around'), { target: { value: '' } })
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0]).toHaveLength(2)
  })

  it('closing the disclosure again hides the date field', async () => {
    const { user } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))
    expect(adjust()).toHaveAttribute('aria-controls', screen.getByLabelText('Date the board around').parentElement?.id)
    await user.click(adjust())
    expect(adjust()).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByLabelText('Date the board around')).not.toBeInTheDocument()
  })

  it('shows no Dates group for a file without a date_anchor, unparseable JSON, or a CSV', async () => {
    const { user } = await renderModal()
    for (const file of [
      new File(['{"name":"B"}'], 'board.json', { type: 'application/json' }),
      // A non-string anchor is no anchor on the server either (#1949), so there is nothing to opt out of.
      new File(['{"date_anchor": 20260315}'], 'board.json', { type: 'application/json' }),
      new File(['not json'], 'board.json', { type: 'application/json' }),
      new File(['null'], 'board.json', { type: 'application/json' }),
      new File(['Title,Column,Swimlane\n'], 'board.csv', { type: 'text/csv' }),
    ]) {
      await upload(user, file)
      await screen.findByText(file.name)
      // Give the file read a turn to settle before asserting absence.
      await new Promise((r) => setTimeout(r, 20))
      expect(screen.queryByRole('group', { name: 'Dates' })).not.toBeInTheDocument()
    }
  })

  it('a new file starts checked again with the adjuster closed', async () => {
    const { user } = await renderModal()
    await upload(user, anchored())
    await user.click(await screen.findByRole('button', { name: 'Adjust dates' }))
    await user.click(moveDates())
    await upload(user, anchored())
    await waitFor(() => expect(moveDates()).toBeChecked())
    expect(adjust()).toHaveAttribute('aria-expanded', 'false')
  })
})

describe('ImportBoardModal — Dates for a sample (#1949)', () => {
  async function pickSample(user: ReturnType<typeof userEvent.setup>) {
    await user.click(screen.getByRole('button', { name: 'Use this sample: Simple Kanban' }))
    await screen.findByText(/From sample:/)
  }

  it('sends the manifest anchor plus a changed target day', async () => {
    const { user, onImport } = await renderModal()
    await pickSample(user)
    expect(moveDates()).toBeChecked()
    await user.click(adjust())
    fireEvent.change(screen.getByLabelText('Date the board around'), { target: { value: '2026-05-01' } })
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0][2]).toEqual({ shift_dates_from: '2026-03-15', shift_dates_to: '2026-05-01' })
  })

  it('unchecking sends shift_dates false and never shift_dates_from', async () => {
    const { user, onImport } = await renderModal()
    await pickSample(user)
    await user.click(moveDates())
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(onImport).toHaveBeenCalled())
    expect(onImport.mock.calls[0][2]).toEqual({ shift_dates: false })
  })
})
