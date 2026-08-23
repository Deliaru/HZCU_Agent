import { defineConfig, devices } from "@playwright/test";

const executablePath = process.env.PILOT_BROWSER_EXECUTABLE;

export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  fullyParallel: false,
  retries: 1,
  reporter: [["list"], ["html", { outputFolder: "../../output/playwright/report" }]],
  use: {
    baseURL: process.env.PILOT_E2E_BASE_URL ?? "http://127.0.0.1:3000",
    launchOptions: executablePath ? { executablePath } : undefined,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    {
      name: "desktop-chromium",
      use: { ...devices["Desktop Chrome"], viewport: { width: 1440, height: 1000 } },
    },
    {
      name: "android-chrome",
      use: { ...devices["Pixel 7"], viewport: { width: 390, height: 844 } },
    },
    {
      name: "mobile-webkit",
      use: { ...devices["iPhone 13"], viewport: { width: 390, height: 844 } },
    },
  ],
  outputDir: "../../output/playwright/results",
});
