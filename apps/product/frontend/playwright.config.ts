import { defineConfig, devices } from "@playwright/test";

const baseURL = process.env.APP_BASE_URL || "http://127.0.0.1:5173";

export default defineConfig({
  testDir: "./classification workflow",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL,
    trace: "retain-on-failure",
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] },
    },
  ],
});
