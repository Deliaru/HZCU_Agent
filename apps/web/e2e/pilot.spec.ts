import { expect, test } from "@playwright/test";

async function finishOnboarding(page: import("@playwright/test").Page) {
  await page.goto("/");
  const skip = page.getByRole("button", { name: "先跳过" });
  const onboardingVisible = await skip
    .waitFor({ state: "visible", timeout: 10_000 })
    .then(() => true)
    .catch(() => false);
  if (onboardingVisible) {
    await skip.click();
    await expect(skip).toBeHidden({ timeout: 10_000 });
  }
}

test("anonymous pilot completes question, evidence and history journey", async ({
  page,
}) => {
  await finishOnboarding(page);
  await page.getByLabel("输入校园问题").fill("转专业前应该重点核对哪些学校规定？");
  await page.getByRole("button", { name: "发送" }).click();
  await expect(page.locator(".live-trace, .agent-message")).toBeVisible();
  await expect(page.locator(".agent-message")).toBeVisible({ timeout: 80_000 });
  await expect(page.locator(".agent-message .markdown")).not.toBeEmpty();

  await page.reload();
  await expect(page.locator(".agent-message")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(".onboarding-backdrop")).toHaveCount(0, {
    timeout: 10_000,
  });
  await page.getByRole("button", { name: /有帮助/ }).last().click();
});

test("my space supports manual todo lifecycle", async ({ page }) => {
  await finishOnboarding(page);
  const mobileMenu = page.getByRole("button", { name: "打开会话历史" });
  if (await mobileMenu.isVisible()) await mobileMenu.click();
  await page.getByRole("button", { name: /我的空间/ }).click();
  await page.getByRole("navigation", { name: "我的空间分区" }).getByRole("button", {
    name: /^待办 \d+$/,
  }).click();
  await page.getByPlaceholder("手动添加一项待办").fill("查看新学期校历");
  await page.getByRole("button", { name: "添加" }).click();
  await expect(page.getByText("查看新学期校历")).toBeVisible();
  await page.getByRole("button", { name: "完成待办" }).click();
  await expect(page.locator(".space-todos article.done")).toBeVisible();
});

test("mobile drawers remain usable without horizontal overflow", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "desktop-chromium");
  await finishOnboarding(page);
  await page.getByRole("button", { name: "打开会话历史" }).click();
  await expect(page.locator(".conversation-rail")).toHaveClass(/rail-open/);
  await page.getByRole("button", { name: "关闭会话历史" }).click();
  const dimensions = await page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
  expect(dimensions.scrollWidth).toBeLessThanOrEqual(dimensions.clientWidth + 1);
});
