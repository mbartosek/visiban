import { describe, it, expect, afterEach } from 'vitest'
import { StrictMode } from 'react'
import { useRef, useState } from 'react'
import { render, cleanup, fireEvent } from '@testing-library/react'
import { useFocusTrap } from '../hooks/useFocusTrap'

afterEach(() => {
  cleanup()
})

function Harness({ active, children }: { active: boolean; children: React.ReactNode }) {
  const ref = useRef<HTMLDivElement>(null)
  useFocusTrap(ref, active)
  return (
    <div ref={ref} data-testid="container">
      {children}
    </div>
  )
}

describe('useFocusTrap', () => {
  it('wraps Tab from the last focusable element to the first', () => {
    const { getByTestId } = render(
      <Harness active>
        <button data-testid="first">first</button>
        <button data-testid="middle">middle</button>
        <button data-testid="last">last</button>
      </Harness>,
    )
    const first = getByTestId('first')
    const last = getByTestId('last')
    last.focus()
    expect(document.activeElement).toBe(last)

    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(first)
  })

  it('wraps Shift+Tab from the first focusable element to the last', () => {
    const { getByTestId } = render(
      <Harness active>
        <button data-testid="first">first</button>
        <button data-testid="middle">middle</button>
        <button data-testid="last">last</button>
      </Harness>,
    )
    const first = getByTestId('first')
    const last = getByTestId('last')
    first.focus()
    expect(document.activeElement).toBe(first)

    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('skips elements inside an aria-hidden subtree when computing first/last', () => {
    const { getByTestId } = render(
      <Harness active>
        <button data-testid="first">first</button>
        <div aria-hidden="true">
          <button data-testid="hidden">hidden</button>
        </div>
        <button data-testid="last">last</button>
      </Harness>,
    )
    const first = getByTestId('first')
    const last = getByTestId('last')
    last.focus()

    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(first)

    first.focus()
    fireEvent.keyDown(document, { key: 'Tab', shiftKey: true })
    expect(document.activeElement).toBe(last)
  })

  it('is a no-op when the container has no focusable elements', () => {
    const { getByTestId } = render(
      <Harness active>
        <span>not focusable</span>
      </Harness>,
    )
    const container = getByTestId('container')
    container.tabIndex = -1
    container.focus()
    const before = document.activeElement
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(before)
  })

  it('does nothing for non-Tab keys', () => {
    const { getByTestId } = render(
      <Harness active>
        <button data-testid="first">first</button>
        <button data-testid="last">last</button>
      </Harness>,
    )
    const last = getByTestId('last')
    last.focus()
    fireEvent.keyDown(document, { key: 'Enter' })
    fireEvent.keyDown(document, { key: 'a' })
    expect(document.activeElement).toBe(last)
  })

  it('does nothing when active is false', () => {
    const { getByTestId } = render(
      <Harness active={false}>
        <button data-testid="first">first</button>
        <button data-testid="last">last</button>
      </Harness>,
    )
    const last = getByTestId('last')
    last.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(last)
  })

  it('does not wrap when focus is on a middle element (browser handles it)', () => {
    const { getByTestId } = render(
      <Harness active>
        <button data-testid="first">first</button>
        <button data-testid="middle">middle</button>
        <button data-testid="last">last</button>
      </Harness>,
    )
    const middle = getByTestId('middle')
    middle.focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(middle)
  })
})

describe('useFocusTrap focus restore and nesting (#1732)', () => {
  function Toggle() {
    const [open, setOpen] = useState(false)
    const ref = useRef<HTMLDivElement>(null)
    useFocusTrap(ref, open)
    return (
      <>
        <button data-testid="opener" onClick={() => setOpen(true)}>open</button>
        {open && (
          <div ref={ref}>
            <button data-testid="inner" autoFocus onClick={() => setOpen(false)}>close</button>
          </div>
        )}
      </>
    )
  }

  it('restores focus to the opener even when a child autoFocuses first', () => {
    const { getByTestId, queryByTestId } = render(<Toggle />)
    const opener = getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    expect(document.activeElement).toBe(getByTestId('inner'))
    fireEvent.click(getByTestId('inner'))
    expect(queryByTestId('inner')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('does not steal focus the user moved elsewhere before closing', () => {
    const { getByTestId } = render(
      <>
        <Toggle />
        <input data-testid="elsewhere" />
      </>,
    )
    const opener = getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    getByTestId('elsewhere').focus()
    fireEvent.click(getByTestId('inner'))
    expect(document.activeElement).toBe(getByTestId('elsewhere'))
  })

  it('only the innermost (most recently activated) trap handles Tab', () => {
    const tree = (innerActive: boolean) => (
      <Harness active>
        <button data-testid="outer-first">a</button>
        <Harness active={innerActive}>
          <button data-testid="inner-first">b</button>
          <button data-testid="inner-last">c</button>
        </Harness>
      </Harness>
    )
    const { getByTestId, rerender } = render(tree(false))
    rerender(tree(true))
    // inner-last is also the last focusable of the outer container; the outer
    // trap must not move focus on the same keydown.
    getByTestId('inner-last').focus()
    fireEvent.keyDown(document, { key: 'Tab' })
    expect(document.activeElement).toBe(getByTestId('inner-first'))
  })

  it('restores focus under React StrictMode (simulated unmount must not drop the opener)', () => {
    // A panel that mounts already-active (CardDetail style): StrictMode runs
    // its effect, a simulated cleanup, then the effect again, without a re-render.
    function Panel({ onClose }: { onClose: () => void }) {
      const ref = useRef<HTMLDivElement>(null)
      useFocusTrap(ref, true)
      return (
        <div ref={ref}>
          <button data-testid="inner" autoFocus onClick={onClose}>close</button>
        </div>
      )
    }
    function Host() {
      const [open, setOpen] = useState(false)
      return (
        <>
          <button data-testid="opener" onClick={() => setOpen(true)}>open</button>
          {open && <Panel onClose={() => setOpen(false)} />}
        </>
      )
    }
    const { getByTestId, queryByTestId } = render(
      <StrictMode>
        <Host />
      </StrictMode>,
    )
    const opener = getByTestId('opener')
    opener.focus()
    fireEvent.click(opener)
    expect(document.activeElement).toBe(getByTestId('inner'))
    fireEvent.click(getByTestId('inner'))
    expect(queryByTestId('inner')).toBeNull()
    expect(document.activeElement).toBe(opener)
  })

  it('falls back to restoreTarget when the opener unmounts', () => {
    function Fallback() {
      const [open, setOpen] = useState(false)
      const ref = useRef<HTMLDivElement>(null)
      const targetRef = useRef<HTMLButtonElement>(null)
      useFocusTrap(ref, open, targetRef.current)
      return (
        <>
          <button ref={targetRef} data-testid="kebab">kebab</button>
          {!open && <button data-testid="item" onClick={() => setOpen(true)}>item</button>}
          {open && (
            <div ref={ref}>
              <button data-testid="inner" autoFocus onClick={() => setOpen(false)}>x</button>
            </div>
          )}
        </>
      )
    }
    const { getByTestId } = render(<Fallback />)
    const item = getByTestId('item')
    item.focus()
    fireEvent.click(item)
    fireEvent.click(getByTestId('inner'))
    expect(document.activeElement).toBe(getByTestId('kebab'))
  })
})
