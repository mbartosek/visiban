import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ImportBoardModal from '../components/Board/ImportBoardModal'
import { importBoard } from '../api/boards'
import type { ImportOptions, SampleBoardSummary } from '../types'

// #1949: the modal's date choices must survive the real `importBoard` request
// builder, which drops `options` it considers "default". Only the HTTP client
// is mocked here, so a payload the modal builds but `importBoard` swallows
// fails this file (it did: shift_dates_to never reached the server).

vi.mock('../api/client', () => ({
  default: { get: vi.fn(), post: vi.fn() },
}))

import client from '../api/client'

const mockGet = client.get as ReturnType<typeof vi.fn>
const mockPost = client.post as ReturnType<typeof vi.fn>

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

const anchored = () =>
  new File([JSON.stringify({ schema_version: 2, date_anchor: '2026-03-15', name: 'B' })], 'simple_kanban.json', {
    type: 'application/json',
  })

/** The same call shape as Dashboard's and GroupDetail's handleImportBoard. */
const onImport = (file: File, name?: string, options?: ImportOptions) =>
  (options ? importBoard(file, name, undefined, options) : importBoard(file, name)).then(() => undefined)

function sentOptions(): unknown {
  const body = mockPost.mock.calls[0][1] as FormData
  const raw = body.get('options')
  return raw === null ? null : JSON.parse(raw as string)
}

async function renderAndUpload() {
  const user = userEvent.setup()
  render(<ImportBoardModal onImport={onImport} onCancel={vi.fn()} />)
  await screen.findAllByRole('button', { name: /^Use this sample: / })
  await user.upload(document.querySelector('input[type="file"]') as HTMLInputElement, anchored())
  await screen.findByRole('group', { name: 'Dates' })
  return user
}

beforeEach(() => {
  mockGet.mockReset().mockImplementation((url: string) =>
    Promise.resolve({ data: url.endsWith('/samples/') ? [SAMPLE] : new Blob(['{}']) }),
  )
  mockPost.mockReset().mockResolvedValue({ data: { id: 1, import_summary: {} } })
})

describe('ImportBoardModal → importBoard request (#1949)', () => {
  it('a custom Adjust dates day reaches the request as shift_dates_to', async () => {
    const user = await renderAndUpload()
    await user.click(screen.getByRole('button', { name: 'Adjust dates' }))
    fireEvent.change(screen.getByLabelText('Date the board around'), { target: { value: '2026-06-01' } })
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(mockPost).toHaveBeenCalledWith('/api/v1/boards/import/', expect.any(FormData)))
    expect(sentOptions()).toEqual({ shift_dates_to: '2026-06-01' })
  })

  it('the opt-out reaches the request as shift_dates false', async () => {
    const user = await renderAndUpload()
    await user.click(screen.getByRole('checkbox', { name: 'Move dates to today' }))
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(mockPost).toHaveBeenCalled())
    expect(sentOptions()).toEqual({ shift_dates: false })
  })

  it('every default sends no options field at all', async () => {
    const user = await renderAndUpload()
    await user.click(screen.getByRole('button', { name: 'Import' }))
    await waitFor(() => expect(mockPost).toHaveBeenCalled())
    expect(sentOptions()).toBeNull()
  })
})
