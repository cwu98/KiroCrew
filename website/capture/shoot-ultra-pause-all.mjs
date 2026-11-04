/**
 * Screenshot harness for Ultra Mode Pause all / Resume all.
 *
 * Serves capture/ultra-pause-all.tsx with a programmatic Vite dev server on
 * loopback and photographs the REAL Board Workers list in dark and light:
 * running, the confirm dialog, paused. The dark pass is also recorded as a clip.
 * The reads are stubbed inside the entry, so no gateway is involved.
 *
 * Usage (from website/): node capture/shoot-ultra-pause-all.mjs [outDir]
 */
import { chromium } from 'playwright'
import { createServer } from 'vite'
import { mkdirSync, readdirSync, renameSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve, join } from 'node:path'
import { chromiumExecutable } from '../scripts/lib/chromium-executable.mjs'

const OUT = process.argv[2] || resolve(dirname(fileURLToPath(import.meta.url)), '../../temp-screenshots/ultra-pause-all')
mkdirSync(OUT, { recursive: true })

const server = await createServer({
  configFile: './vite.config.ts',
  server: { host: '127.0.0.1', port: 5198, strictPort: false },
  logLevel: 'warn',
})
await server.listen()
const base = server.resolvedUrls?.local?.[0]?.replace(/\/$/, '') || `http://127.0.0.1:${server.config.server.port}`

const browser = await chromium.launch({
  executablePath: chromiumExecutable(),
  env: { ...process.env, LD_LIBRARY_PATH: '' },
})

for (const theme of ['dark', 'light']) {
  const videoDir = join(OUT, `video-${theme}`)
  const context = await browser.newContext({
    viewport: { width: 760, height: 600 },
    deviceScaleFactor: theme === 'dark' ? 1 : 2,
    ...(theme === 'dark' ? { recordVideo: { dir: videoDir, size: { width: 760, height: 600 } } } : {}),
  })
  const page = await context.newPage()
  page.on('pageerror', e => console.log('PAGE EXCEPTION:', e.message))
  await page.goto(`${base}/capture/ultra-pause-all.html?theme=${theme}`)
  await page.locator('[data-testid="ultra-view-list"]').click()
  await page.locator('[data-testid="crew-pause-all"]').waitFor({ timeout: 30000 })
  await page.waitForTimeout(700)
  await page.screenshot({ path: `${OUT}/a-running-${theme}.png` })
  await page.locator('[data-testid="crew-pause-all"]').click()
  await page.getByRole('dialog').waitFor()
  await page.waitForTimeout(500)
  await page.screenshot({ path: `${OUT}/b-confirm-${theme}.png` })
  await page.getByRole('dialog').getByRole('button', { name: 'Pause all' }).click()
  await page.locator('[data-testid="crew-resume-all"]').waitFor()
  await page.waitForTimeout(700)
  await page.screenshot({ path: `${OUT}/c-paused-${theme}.png` })
  await page.locator('[data-testid="crew-member-continue"]').first().click()
  await page.waitForTimeout(700)
  await page.locator('[data-testid="crew-resume-all"]').click()
  await page.locator('[data-testid="crew-pause-all"]').waitFor()
  await page.waitForTimeout(900)
  await page.screenshot({ path: `${OUT}/d-resumed-${theme}.png` })
  await context.close()
  if (theme === 'dark') {
    const [file] = readdirSync(videoDir).filter(f => f.endsWith('.webm'))
    renameSync(join(videoDir, file), `${OUT}/pause-all-clip.webm`)
  }
  console.log('wrote', theme)
}
await browser.close()
await server.close()
