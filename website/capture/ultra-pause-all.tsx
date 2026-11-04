/**
 * Capture page: Ultra Mode Pause all / Resume all on the Board's Workers list, the
 * sidebar row's "Paused", the main chat's paused notice, and the composer's Ultra
 * Mode button in its OFF and ON (switch back) looks.
 *
 * The panel, row, notice and button are the real components; only the crew list
 * and the pause / resume / continue routes are fixtures (in memory, a pause
 * flips the list's `paused` and every running worker's status).
 * `?theme=light` for the light palette.
 */
import { useSyncExternalStore } from 'react'
import { createRoot } from 'react-dom/client'
import { Provider } from 'react-redux'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { api } from '../src/api/client'
import { store } from '../src/store'
import { initI18n } from '../src/i18n/all'
import UltraCrewPanel from '../src/pages/chat/command-center/UltraCrewPanel'
import { CrewRowAttention } from '../src/pages/chat-sidebar/CrewRowCounts'
import CrewPausedNoticeCard from '../src/pages/chat/CrewPausedNoticeCard'
import { CrewModeChip } from '../src/components/CrewModeChip'
import { PREVIEW_CREW_MODE } from '../src/utils/previewFlags'
import '../src/index.css'

const params = new URLSearchParams(location.search)
document.documentElement.setAttribute('data-theme', params.get('theme') === 'light' ? 'light' : 'dark')
localStorage.setItem(PREVIEW_CREW_MODE, '1')

const ago = (min: number) => new Date(Date.now() - min * 60_000).toISOString()
const W = (key: string, title: string, status: string, group: string, by = 'root', summary = '') =>
  ({ key, title, agent: 'kirocrew-worker', status, group, last_activity: ago(2), ledger_item: '', created_by: by, dispatched_by: by, summary })

const RUNNING = ['w-runtime', 'w-docs', 'w-faq', 'w-load']
let paused: string[] | null = null
const listeners = new Set<() => void>()
const emit = () => listeners.forEach(fn => fn())
const workers = () => [
  W('w-spec', 'Runtime spec', 'idle', 'done'),
  ...RUNNING.map(key => {
    const stopped = paused?.includes(key)
    const title = { 'w-runtime': 'Slack code channels runtime', 'w-docs': 'Docs team', 'w-faq': 'FAQ answers', 'w-load': 'Load test' }[key]!
    return W(key, title, stopped ? 'idle' : 'working', stopped ? 'waiting' : 'working', key === 'w-faq' ? 'w-docs' : 'root')
  }),
  W('w-approval', 'Approval flow', 'needs_approval', 'approval'),
]
Object.assign(api, {
  crewWorkers: async () => ({ root: 'root', workers: workers(), ...(paused ? { paused: { stopped: paused, paused_at: ago(0) } } : {}) }),
  crewBoard: async () => ({ conductor: null, items: [], asks: {} }),
  sessionSummary: async () => ({ enabled: false, stale: false, intents: [], constraints: [], generated_at: null, user_turns: null, last_activity: null }),
  crewPause: async () => { paused = [...RUNNING]; emit(); return { ok: true, paused: true, stopped: paused } },
  crewResume: async () => { paused = null; emit(); return { ok: true, paused: false, resumed: {} } },
  continueSlot: async (key: string) => { paused = paused ? paused.filter(k => k !== key) : null; emit(); return { ok: true, slot: key } },
})

function Shell() {
  const isPaused = useSyncExternalStore(fn => { listeners.add(fn); return () => listeners.delete(fn) }, () => paused !== null)
  return (
    <div style={{ display: 'flex', gap: 8, padding: 8 }} className="bg-bg text-text" data-testid="capture-frame">
      <div style={{ width: 300 }} className="flex flex-col gap-3">
        <div className="rounded-lg border border-border bg-bg-elevated p-2.5 text-[13px]" data-testid="capture-sidebar-row">
          <div className="text-[11px] text-muted">ULTRA MODE</div>
          <div className="font-semibold">Slack Code AI Implementation</div>
          <CrewRowAttention needsYou={1} stalled={0} running={4} done={false} paused={isPaused} title="" />
        </div>
        {isPaused && <CrewPausedNoticeCard stopped={RUNNING.length} />}
        <div className="flex items-center gap-2 rounded-lg border border-border p-2" data-testid="capture-chips">
          <CrewModeChip testId="chip-off" onTurnOn={() => {}} />
          <CrewModeChip testId="chip-on" onTurnOn={() => {}} pressed onTurnOff={() => {}} />
        </div>
      </div>
      <div style={{ width: 420, height: 560 }} className="border border-border rounded-lg">
        <UltraCrewPanel root="root" viewing="root" active />
      </div>
    </div>
  )
}

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
initI18n('en')
createRoot(document.getElementById('root')!).render(
  <Provider store={store}>
    <QueryClientProvider client={qc}>
      <Shell />
    </QueryClientProvider>
  </Provider>,
)
