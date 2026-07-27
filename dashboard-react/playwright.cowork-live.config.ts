import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.COWORK_LIVE_BASE_URL;

if (baseURL === undefined || baseURL.length === 0) {
  throw new Error(
    "COWORK_LIVE_BASE_URL is required. Run `npm run test:e2e:cowork-live` instead of invoking this config directly.",
  );
}

export default defineConfig({
  testDir: "./tests/live",
  testMatch: "cowork-live.spec.ts",
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: "list",
  outputDir: "./test-results/cowork-live/playwright",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "cowork-live",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "cowork-live-firefox",
      grep: /@firefox-smoke/,
      dependencies: ["cowork-live"],
      use: { ...devices["Desktop Firefox"] },
    },
  ],
});
