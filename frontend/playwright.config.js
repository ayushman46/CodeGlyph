import { defineConfig, devices } from '@playwright/test';

const browserChannel = process.env.PLAYWRIGHT_BROWSER_CHANNEL || undefined;

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  fullyParallel: true,
  reporter: process.env.CI ? 'line' : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL || 'http://127.0.0.1:4173',
    trace: 'retain-on-failure',
    channel: browserChannel,
    ...devices['Desktop Chrome'],
  },
  webServer: [
    {
      // Marketing pages: the normal production build.
      command: 'npm run preview -- --host 127.0.0.1 --port 4173',
      url: 'http://127.0.0.1:4173',
      reuseExistingServer: !process.env.CI,
      timeout: 120_000,
    },
    {
      // Workspace: a separate build with a fake session; every /api call is
      // mocked inside the tests, so no backend or Supabase is needed.
      command: 'VITE_E2E_FAKE_SESSION=true VITE_API_BASE_URL=/api npx vite build --outDir dist-e2e --emptyOutDir && npx vite preview --outDir dist-e2e --host 127.0.0.1 --port 4174',
      url: 'http://127.0.0.1:4174',
      reuseExistingServer: !process.env.CI,
      timeout: 180_000,
    },
  ],
});
