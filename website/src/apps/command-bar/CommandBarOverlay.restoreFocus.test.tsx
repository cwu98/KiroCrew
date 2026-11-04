import { useState } from 'react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'

import CommandBarOverlay from './CommandBarOverlay'

/**
 * Focus returns to the opener when the command bar is dismissed with Escape,
 * for both ways the bar opens.
 *
 * `useDialogFocusTrap` records the element to restore focus to by reading
 * `document.activeElement` in its open effect, which runs after the commit
 * phase. An `autoFocus` on the bar's own input would therefore have focused
 * that input before the effect reads it, so the effect would record the input
 * and closing the bar would aim focus at a detached node, dropping the user on
 * `<body>`. The input leaves focus entry to the trap, so the effect reads the
 * opener (what holds focus when the bar mounts) and Escape returns there.
 *
 * These run against the REAL hook — unlike the other CommandBarOverlay suites,
 * this file deliberately does NOT mock `useDialogFocusTrap`, because the hook's
 * focus capture and restore are exactly what is under test. Only the store,
 * palette providers and viewport are stubbed, as elsewhere, so the component
 * mounts without a live gateway.
 *
 * The host mirrors the real one (`QuickSearchSurface`): the overlay is mounted
 * ONLY while open, so a close unmounts it and runs the trap's restore — which is
 * what the production bar does, and what makes `document.activeElement` at the
 * trap's mount be the opener.
 */

const dispatch = vi.fn()
const navigate = vi.fn()
const newSessionWithToken = vi.fn()
const enterInsertOrNewSession = vi.fn()

const storeState = {
  dashboard: { slots: [] as Record<string, unknown>[], unreadSlots: [] as string[] },
  chat: { slotStatusDetail: {} as Record<string, unknown>, activeSlot: null as string | null },
}

vi.mock('../../store', () => ({
  useAppDispatch: () => dispatch,
  useAppSelector: (fn: (s: unknown) => unknown) => fn(storeState),
  useAppStore: () => ({ getState: () => storeState }),
}))
vi.mock('../../store/chatSlice', () => ({
  createSlot: (arg: unknown) => ({ type: 'createSlot', arg }),
  setPendingInput: (text: string) => ({ type: 'setPendingInput', text }),
  switchSlot: (key: string) => ({ type: 'switchSlot', key }),
}))
vi.mock('../../components/commandPalette/paletteActions', () => ({
  usePaletteActions: () => ({ navigate, enterInsertOrNewSession, newSessionWithToken }),
}))
vi.mock('../../components/commandPalette/providers/sessionsProvider', () => ({
  useSessionsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../components/commandPalette/providers/recentsProvider', async importOriginal => ({
  ...(await importOriginal<typeof import('../../components/commandPalette/providers/recentsProvider')>()),
  useRecentsProvider: () => ({ search: vi.fn(async () => []) }),
}))
vi.mock('../../hooks/useVisualViewport', () => ({ useVisualViewport: () => ({ height: 800 }) }))
vi.mock('../../hooks/useTheme', () => ({ useTheme: () => ({ cycle: vi.fn() }) }))

/**
 * A host that owns the `open` state the real dashboard owns and mounts the
 * overlay ONLY while open. The opener sets it true; the bar's `onClose` sets it
 * false, which unmounts the overlay and so runs the focus-restore. `opener` is
 * the element that holds focus when the bar opens — the header "Run a command"
 * button in one path, the message box in the other.
 */
function Host({ opener }: { opener: 'button' | 'composer' }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      {opener === 'button' ? (
        <button data-testid="opener" onClick={() => setOpen(true)}>
          Run a command
        </button>
      ) : (
        <textarea
          data-testid="opener"
          aria-label="Message box"
          onKeyDown={e => {
            // Stand-in for the composer's Ctrl+K binding that opens the bar.
            if ((e.ctrlKey || e.metaKey) && e.key === 'k') setOpen(true)
          }}
        />
      )}
      {open ? <CommandBarOverlay open onClose={() => setOpen(false)} /> : null}
    </>
  )
}

function renderHost(opener: 'button' | 'composer') {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={client}>
      <Host opener={opener} />
    </QueryClientProvider>,
  )
}

describe('CommandBarOverlay focus restore on Escape', () => {
  beforeEach(() => {
    dispatch.mockReset()
    storeState.dashboard = { slots: [], unreadSlots: [] }
    storeState.chat = { slotStatusDetail: {}, activeSlot: null }
    localStorage.clear()
  })

  it('returns focus to the header button after Escape', async () => {
    renderHost('button')
    const opener = screen.getByTestId('opener')
    opener.focus()
    expect(opener).toHaveFocus()

    fireEvent.click(opener)
    // The bar is open and the focus trap has moved focus into its input.
    const input = await screen.findByRole('combobox')
    expect(input).toHaveFocus()

    fireEvent.keyDown(input, { key: 'Escape' })
    // onClose unmounts the overlay; the trap restores focus to the opener.
    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    expect(opener).toHaveFocus()
  })

  it('returns focus to the message box after Ctrl+K opens the bar and Escape closes it', async () => {
    renderHost('composer')
    const opener = screen.getByTestId('opener')
    opener.focus()
    expect(opener).toHaveFocus()

    fireEvent.keyDown(opener, { key: 'k', ctrlKey: true })
    const input = await screen.findByRole('combobox')
    expect(input).toHaveFocus()

    fireEvent.keyDown(input, { key: 'Escape' })
    await waitFor(() => expect(screen.queryByRole('combobox')).toBeNull())
    expect(opener).toHaveFocus()
  })
})
