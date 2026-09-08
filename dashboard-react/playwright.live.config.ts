import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.WB_LIVE_BASE_URL;
const app = process.env.WB_LIVE_APP ?? "cowork";
const scenario = process.env.WB_LIVE_SCENARIO ?? "lifecycle";

if (app !== "cowork") throw new Error(`No live specification registered for app: ${app}`);

if (baseURL === undefined || baseURL.length === 0) {
  throw new Error(
    "WB_LIVE_BASE_URL is required. Run `npm run test:e2e:live -- --app cowork` instead of invoking this config directly.",
  );
}

export default defineConfig({
  testDir: "./tests/live",
  testMatch: scenario === "truth-panel" ? "cowork-truth.spec.ts" : `${app}.spec.ts`,
  fullyParallel: false,
  workers: 1,
  forbidOnly: true,
  retries: 0,
  timeout: 120_000,
  expect: { timeout: 20_000 },
  reporter: "list",
  outputDir: "./test-results/live/playwright",
  use: {
    baseURL,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    video: "retain-on-failure",
  },
  projects: [
    {
      name: "live",
      use: { ...devices["Desktop Chrome"] },
    },
    {
      name: "live-firefox",
      grep: /@firefox-smoke/,
      dependencies: ["live"],
      use: { ...devices["Desktop Firefox"] },
    },
  ],
});
