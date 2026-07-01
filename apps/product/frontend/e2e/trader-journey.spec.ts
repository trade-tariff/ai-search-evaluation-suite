import { expect, test } from "@playwright/test";

const BACKEND_URL = process.env.E2E_BACKEND_URL || "http://127.0.0.1:8000";
const COMPLEX_CODE = "1806907090";

test("complex trader journey reaches declaration handoff and download", async ({ page, request }) => {
  const health = await request.get(`${BACKEND_URL}/api/health`);
  expect(health.ok()).toBeTruthy();

  const examplesResponse = await request.get(`${BACKEND_URL}/api/journey/examples`);
  expect(examplesResponse.ok()).toBeTruthy();
  const examplesPayload = await examplesResponse.json();
  const complexExample = (examplesPayload.examples || []).find(
    (example: any) => example.expected_code === COMPLEX_CODE,
  );
  expect(complexExample, `live KG complex demo prompt for ${COMPLEX_CODE} is required`).toBeTruthy();

  await page.goto("/");
  await expect(page.getByText("1. What are you importing?")).toBeVisible();
  await expect(page.getByText("e2e trader journey:")).toBeVisible();

  await page.getByText("Classification plumbing").click();
  await page.getByLabel("Q&A process").selectOption("local_rules");
  await expect(page.getByLabel("Q&A process")).toHaveValue("local_rules");

  await page.getByRole("button", { name: /Complex protein powder/ }).click();
  await expect(page.getByText("Trader Q&A")).toBeVisible();
  await expect(page.getByText(/candidate codes considered/)).toBeVisible();

  const narrowingOption = page.getByRole("button", {
    name: /Cocoa drink mix or hot chocolate powder|Flavoured syrup or other prepared food product/,
  });
  await expect(narrowingOption).toBeVisible();
  await narrowingOption.click();

  await expect(page.getByText("Suggested codes")).toBeVisible();
  const suggestedCode = page.getByRole("button", { name: new RegExp(`^${COMPLEX_CODE}\\b`) });
  await expect(suggestedCode).toBeVisible();
  await suggestedCode.click();
  await page.getByRole("button", { name: new RegExp(`Use ${COMPLEX_CODE}.*Customs value`) }).first().click();

  await expect(page.getByText("2. Customs value")).toBeVisible();
  await page.getByRole("button", { name: /I know the customs value/ }).click();
  await expect(page.getByLabel("Known customs value (GBP)")).toHaveValue("2065");
  await page.getByRole("button", { name: "Review answers" }).click();
  await expect(page.getByText("Review customs value answers")).toBeVisible();
  await page.getByRole("button", { name: "Calculate customs value" }).click();
  await expect(page.getByRole("heading", { name: "Customs value", exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Next: Duty inputs/ }).click();

  await expect(page.getByText("3. Duty inputs")).toBeVisible();
  await page.locator('input[type="date"]').fill("2026-06-08");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("What country are the goods from?")).toBeVisible();
  await page.locator("select.tj-input").selectOption("CN");
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Do you have a valid proof of origin?")).toBeVisible();
  await page.getByLabel("Yes - I have a valid proof of origin").check();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Composition for additional-code check")).toBeVisible();
  await expect(page.getByText("Prefilled Meursing/additional code:")).toBeVisible();
  await page.getByRole("button", { name: "Continue" }).click();
  await expect(page.getByText("Check your answers")).toBeVisible();
  await page.getByRole("button", { name: "Calculate duty" }).click();
  await expect(page.getByText("Duty calculated")).toBeVisible();
  await expect(page.getByText("7046", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: /Next: Import costs/ }).click();

  await expect(page.getByText("4. Import costs")).toBeVisible();
  await page.getByRole("button", { name: "Review import cost inputs" }).click();
  await expect(page.getByText("Review import cost inputs")).toBeVisible();
  await page.getByRole("button", { name: "Calculate import costs" }).click();
  await expect(page.getByRole("heading", { name: "Total import cost" })).toBeVisible();
  await page.getByRole("button", { name: /Next: Declaration/ }).click();

  await expect(page.getByText("5. Draft customs declaration")).toBeVisible();
  await page.getByRole("button", { name: "Review declaration inputs" }).click();
  await expect(page.getByText("Review declaration inputs")).toBeVisible();
  await page.getByRole("button", { name: "Generate declaration draft" }).click();
  await expect(page.getByText("CDS data elements")).toBeVisible();
  await expect(page.getByText("DE 6/16 Additional code(s)")).toBeVisible();

  await page.getByRole("button", { name: "File for me (broker handoff)" }).click();
  await expect(page.getByText(/^DECL-/)).toBeVisible();

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Download data for declaration" }).click();
  const downloaded = await download;
  expect(downloaded.suggestedFilename()).toContain(`declaration-${COMPLEX_CODE}`);
});
