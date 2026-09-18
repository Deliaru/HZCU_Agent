import { expect, test } from "@playwright/test";
import { PRIVACY_NOTICE_VERSION } from "../lib/privacy-consent";

test.beforeEach(async ({ page }) => {
  // No live API or database is needed for these presentation regressions.
  await page.route("**/api/v1/**", (route) => route.fulfill({
    status: 503,
    contentType: "application/json",
    body: JSON.stringify({ detail: "Offline layout test" }),
  }));
  await page.addInitScript((version) => {
    if (!localStorage.getItem("hzcu-agent-theme")) {
      localStorage.setItem("hzcu-agent-theme", "minimal");
    }
    localStorage.setItem("hzcu-agent-privacy-consent", JSON.stringify({
      version,
      acceptedAt: new Date().toISOString(),
    }));
  }, PRIVACY_NOTICE_VERSION);
});

test("minimal chrome stays consistent across navigation and reload", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop-chromium");
  await page.goto("/");
  const rail = page.locator(".app-rail");
  await expect(rail).toHaveCSS("background-color", "rgb(242, 246, 251)");
  await expect(rail).toHaveCSS("width", "88px");
  for (const href of ["/questions", "/sources", "/admin", "/"]) {
    await page.locator(`.app-rail nav a[href="${href}"]`).click();
    await expect(page.locator("html")).toHaveAttribute("data-theme", "minimal");
    await expect(rail).toHaveCSS("background-color", "rgb(242, 246, 251)");
    await expect(rail).toHaveCSS("width", "88px");
  }
  await page.reload();
  await expect(rail).toHaveCSS("background-color", "rgb(242, 246, 251)");
});

test("welcome has no decorative columns and a bounded input flow", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop-chromium");
  for (const size of [{ width: 2500, height: 1280 }, { width: 1440, height: 900 }, { width: 1024, height: 720 }]) {
    await page.setViewportSize(size);
    await page.goto("/");
    const welcome = page.locator(".workspace.is-welcome");
    await expect(welcome).toBeVisible();
    await expect(welcome.locator(".hero-wordmark")).toBeHidden();
    await expect(welcome.locator(".evidence-desk")).toBeHidden();
    expect(await welcome.locator(".hero-copy").evaluate((el) =>
      getComputedStyle(el, "::before").display)).toBe("none");
    const composer = welcome.locator(".composer");
    await composer.scrollIntoViewIfNeeded();
    const inputBox = await composer.boundingBox();
    const cardsBox = await welcome.locator(".question-prompts").boundingBox();
    expect(inputBox!.width).toBeLessThanOrEqual(860);
    expect(Math.abs(inputBox!.x - cardsBox!.x)).toBeLessThan(2);
    await expect(welcome.locator("textarea")).toBeInViewport();
  }
});

test("new conversation button is centered in its outer slot", async ({ page }, info) => {
  test.skip(info.project.name !== "desktop-chromium");
  await page.goto("/");
  const slot = page.locator(".new-thread-slot");
  const button = slot.locator(".new-thread");
  await expect(button).toBeVisible();
  const outer = (await slot.boundingBox())!;
  const inner = (await button.boundingBox())!;
  expect(Math.abs(inner.y - outer.y - (outer.y + outer.height - inner.y - inner.height))).toBeLessThan(1);
  expect(Math.abs(inner.x - outer.x - (outer.x + outer.width - inner.x - inner.width))).toBeLessThan(1);
});
