/**
 * Switching a chat that was turned into an Ultra Mode chat mid-chat back to a
 * normal chat: offered only when the gateway recorded the agent it had before,
 * at once with no worker running, and after Pause all (once confirmed) when
 * workers run. A chat born in Ultra Mode offers nothing.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { Provider } from 'react-redux'
import { createTestStore } from '../../../test/helpers'
import { setCrewModeCatalog } from '../../../lib/crewMode'
import type { ChatSlot } from '../../../types'
import { useCrewModeSwitch } from './crewModeSwitch'
import { turnOnCrewModePreview } from '../../../test/crewModePreview'
import { dropPanelViews } from '../../../hooks/usePanelTabs'

const answers = vi.hoisted(() => ({ confirm: true, asked: 0 }))
const pause = vi.hoisted(() => vi.fn(async () => ({ ok: true, paused: true, stopped: [] })))
vi.mock('../../../hooks/useIsMobile', () => ({ useIsMobile: () => false }))
vi.mock('../../../hooks/usePanelTabs', async importOriginal => ({ ...await importOriginal<object>(), dropPanelViews: vi.fn() }))
vi.mock('../../../components/ConfirmDialog', () => ({
  useConfirm: () => ({ confirm: async () => { answers.asked += 1; return answers.confirm }, confirmDialog: null, cancel: () => {}, confirmOpen: false }),
}))
vi.mock('../../../api/client', async importOriginal => {
  const real = await importOriginal<{ api: object }>()
  return { ...real, api: { ...real.api, crewPause: pause } }
})

const LEAD = 'kirocrew-team-lead'
const root = (prior: string | null): ChatSlot => ({
  key: 'r', agent: LEAD, agent_kind: 'template', crew_mode_agent: LEAD, crew_mode_switch_back: prior !== null, crew_mode_prior_agent: prior ?? '',
  memory_mode: 'persistent', origin: 'user',
} as unknown as ChatSlot)
const worker = (key: string, running: boolean): ChatSlot => ({ key, agent: 'kirocrew-worker', created_by: 'r', crew_root: 'r', running } as unknown as ChatSlot)

function setup(slot: ChatSlot, workers: ChatSlot[] = []) {
  const switchAgent = vi.fn(async (_agent: string, _kind?: string) => true)
  const store = createTestStore({ dashboard: { slots: [slot, ...workers] } } as never)
  const wrapper = ({ children }: { children: ReactNode }) => <Provider store={store}>{children}</Provider>
  const hook = renderHook(
    () => useCrewModeSwitch({ currentSlot: slot, defaultAgent: 'default', hasUserMessage: true, switchAgent, openView: () => {} }),
    { wrapper },
  )
  return { hook, switchAgent }
}

describe('useCrewModeSwitch switching a mid-chat Ultra Mode chat back', () => {
  beforeEach(() => {
    turnOnCrewModePreview()
    setCrewModeCatalog(LEAD, [LEAD, 'kirocrew-conductor'])
    answers.confirm = true
    answers.asked = 0
    pause.mockClear()
    vi.mocked(dropPanelViews).mockClear()
  })
  afterEach(() => setCrewModeCatalog(undefined, undefined))

  it('is not offered on a chat born in Ultra Mode', () => {
    const { hook } = setup(root(null))
    expect(hook.result.current.switchBackCrewMode).toBeUndefined()
  })

  it('switches back to the previous agent at once when no worker runs', async () => {
    const { hook, switchAgent } = setup(root('default'), [worker('w1', false)])
    await act(async () => { hook.result.current.switchBackCrewMode?.() })
    expect(answers.asked).toBe(0)
    expect(pause).not.toHaveBeenCalled()
    expect(switchAgent).toHaveBeenCalledWith('default', 'template')
    expect(vi.mocked(dropPanelViews)).toHaveBeenCalledWith('r', ['workers', 'command-center'])
  })

  it('pauses the running workers first, once the person confirms', async () => {
    const { hook, switchAgent } = setup(root('default'), [worker('w1', true), worker('w2', true)])
    await act(async () => { hook.result.current.switchBackCrewMode?.() })
    expect(answers.asked).toBe(1)
    expect(pause).toHaveBeenCalledWith('r')
    expect(switchAgent).toHaveBeenCalledWith('default', 'template')
  })

  it('goes back to the default template for a chat that was on the default agent', async () => {
    const { hook, switchAgent } = setup(root(''))
    await act(async () => { hook.result.current.switchBackCrewMode?.() })
    expect(switchAgent).toHaveBeenCalledWith('default', 'template')
  })

  it('stays in Ultra Mode when the person cancels', async () => {
    answers.confirm = false
    const { hook, switchAgent } = setup(root('default'), [worker('w1', true)])
    await act(async () => { hook.result.current.switchBackCrewMode?.() })
    expect(pause).not.toHaveBeenCalled()
    expect(switchAgent).not.toHaveBeenCalled()
  })
})
