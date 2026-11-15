import { defineConfig, devices } from '@playwright/test'
import path from 'node:path'

const port = Number(process.env.PLAYWRIGHT_DOOR_PORT || 8766)
const baseURL = `http://127.0.0.1:${port}`

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.pw.ts',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 60_000,
  expect: { timeout: 10_000 },
  reporter: [['line']],
  outputDir: '../data/verification/playwright-artifacts',
  use: {
    ...devices['Desktop Chrome'],
    baseURL,
    headless: true,
    viewport: { width: 1440, height: 1000 },
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  webServer: {
    command: `npm run build && ../.venv/bin/python ../scripts/run_fixture_server.py --port ${port}`,
    url: `${baseURL}/healthz`,
    reuseExistingServer: false,
    timeout: 120_000,
    env: { AGENT_WARMUP_ENABLED: '0', PYTHONPATH: path.resolve('..') },
  },
})
