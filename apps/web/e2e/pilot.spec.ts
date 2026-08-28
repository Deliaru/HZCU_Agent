import { expect, test } from "@playwright/test";

async function acceptPrivacyNotice(page: import("@playwright/test").Page) {
  const notice = page.getByRole("dialog", { name: /使用前，请先了解这些边界/ });
  if (await notice.isVisible().catch(() => false)) {
    await page.getByRole("checkbox", { name: /我已完整阅读并同意/ }).check();
    await page.getByRole("button", { name: "接受并继续" }).click();
    await expect(notice).toHaveCount(0);
  }
}

async function selectTheme(
  page: import("@playwright/test").Page,
  theme: "minimal" | "character" = "minimal",
) {
  await acceptPrivacyNotice(page);
  const label = theme === "minimal" ? /简洁主题/ : /琮羽主题/;
  const option = page.getByRole("radio", { name: label });
  if (await option.isVisible().catch(() => false)) {
    await option.click();
  }
  await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
}

async function finishOnboarding(
  page: import("@playwright/test").Page,
  theme: "minimal" | "character" = "minimal",
) {
  await page.goto("/");
  await selectTheme(page, theme);
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

test("mobile onboarding locks the page and keeps actions anchored", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "desktop-chromium");

  await page.goto("/");
  await selectTheme(page);
  const panel = page.locator(".onboarding-panel");
  const scrollArea = page.locator(".onboarding-scroll-area");
  const footer = page.locator(".onboarding-panel > footer");

  await expect(panel).toBeVisible();
  await expect(page.locator("html")).toHaveClass(/product-modal-open/);
  await expect(page.locator("body")).toHaveClass(/product-modal-open/);

  const initialFooter = await footer.boundingBox();
  expect(initialFooter).not.toBeNull();
  await scrollArea.evaluate((element) => element.scrollTo({ top: element.scrollHeight }));
  await page.evaluate(() => window.scrollTo({ top: 120 }));

  const layout = await page.evaluate(() => {
    const panelElement = document.querySelector<HTMLElement>(".onboarding-panel");
    const backdropElement = document.querySelector<HTMLElement>(
      ".onboarding-backdrop",
    );
    const footerElement = document.querySelector<HTMLElement>(
      ".onboarding-panel > footer",
    );
    if (!panelElement || !backdropElement || !footerElement) {
      throw new Error("Onboarding panel is missing");
    }
    const backdropRect = backdropElement.getBoundingClientRect();
    const panelRect = panelElement.getBoundingClientRect();
    const footerRect = footerElement.getBoundingClientRect();
    return {
      backdropBottomGap: window.innerHeight - backdropRect.bottom,
      footerBottomGap: panelRect.bottom - footerRect.bottom,
      scrollY: window.scrollY,
    };
  });
  const scrolledFooter = await footer.boundingBox();

  expect(Math.abs((scrolledFooter?.y ?? 0) - initialFooter!.y)).toBeLessThanOrEqual(2.5);
  expect(layout.backdropBottomGap).toBeLessThanOrEqual(1);
  expect(layout.footerBottomGap).toBeLessThanOrEqual(2);
  expect(layout.scrollY).toBe(0);
});

test("character onboarding artwork stays inside the mobile panel", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "desktop-chromium");
  await page.goto("/");
  await selectTheme(page, "character");
  const panel = page.locator(".congyu-onboarding-panel");
  const artwork = page.locator(".congyu-onboarding-character .congyu-artwork");
  await expect(panel).toBeVisible();
  await expect(artwork).toBeVisible();

  const bounds = await page.evaluate(() => {
    const panelElement = document.querySelector<HTMLElement>(".congyu-onboarding-panel");
    const artworkElement = document.querySelector<HTMLElement>(".congyu-onboarding-character .congyu-artwork");
    const copyElement = document.querySelector<HTMLElement>(".congyu-onboarding-character > div");
    if (!panelElement || !artworkElement || !copyElement) throw new Error("Character onboarding is missing");
    const panelRect = panelElement.getBoundingClientRect();
    const artworkRect = artworkElement.getBoundingClientRect();
    const copyRect = copyElement.getBoundingClientRect();
    return {
      insideTop: artworkRect.top >= panelRect.top,
      insideRight: artworkRect.right <= panelRect.right + 1,
      copyDoesNotCoverArtwork: artworkRect.top >= copyRect.bottom - 1,
      rootWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    };
  });
  expect(bounds.insideTop).toBe(true);
  expect(bounds.insideRight).toBe(true);
  expect(bounds.copyDoesNotCoverArtwork).toBe(true);
  expect(bounds.rootWidth).toBeLessThanOrEqual(bounds.clientWidth + 1);
});

test("mobile first theme choice uses compact cards and keeps the hello sticker safe", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "desktop-chromium");
  await page.goto("/");
  await acceptPrivacyNotice(page);
  const panel = page.locator(".theme-first-visit .theme-picker-panel");
  const options = page.locator(".theme-first-visit [role=radio]");
  const preview = page.locator(".theme-first-visit .theme-preview-character");
  const sticker = preview.locator(".congyu-artwork");
  await expect(panel).toBeVisible();
  await expect(options).toHaveCount(2);

  const bounds = await page.evaluate(() => {
    const panelElement = document.querySelector<HTMLElement>(".theme-first-visit .theme-picker-panel");
    const previewElement = document.querySelector<HTMLElement>(".theme-first-visit .theme-preview-character");
    const stickerElement = previewElement?.querySelector<HTMLElement>(".congyu-artwork");
    const optionElements = [...document.querySelectorAll<HTMLElement>(".theme-first-visit [role=radio]")];
    if (!panelElement || !previewElement || !stickerElement) throw new Error("Theme picker is missing");
    const panelRect = panelElement.getBoundingClientRect();
    const previewRect = previewElement.getBoundingClientRect();
    const stickerRect = stickerElement.getBoundingClientRect();
    return {
      panelFits: panelRect.top >= 0 && panelRect.bottom <= window.innerHeight + 1,
      cardsAreHorizontal: optionElements.every((element) => {
        const image = element.querySelector<HTMLElement>(".theme-preview");
        const copyElement = element.querySelector<HTMLElement>(".theme-option-copy");
        if (!image || !copyElement) return false;
        return image.getBoundingClientRect().right <= copyElement.getBoundingClientRect().left + 1;
      }),
      stickerInside:
        stickerRect.left >= previewRect.left - 1 &&
        stickerRect.right <= previewRect.right + 1 &&
        stickerRect.top >= previewRect.top - 1 &&
        stickerRect.bottom <= previewRect.bottom + 1,
    };
  });
  expect(bounds.panelFits).toBe(true);
  expect(bounds.cardsAreHorizontal).toBe(true);
  expect(bounds.stickerInside).toBe(true);
});

test("first visit theme choice persists on this device", async ({ page }) => {
  await page.goto("/");
  const notice = page.getByRole("dialog", { name: /使用前，请先了解这些边界/ });
  await expect(notice).toBeVisible();
  await expect(page.locator(".theme-first-visit")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "接受并继续" })).toBeDisabled();
  await page.getByRole("checkbox", { name: /我已完整阅读并同意/ }).check();
  await page.getByRole("button", { name: "接受并继续" }).click();
  await expect(notice).toHaveCount(0);
  await expect(page.locator(".theme-first-visit")).toBeVisible();
  await selectTheme(page, "character");
  await expect(page.locator(".theme-first-visit")).toHaveCount(0);
  await expect(page.locator(".congyu-agent")).toBeVisible();

  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "character");
  await expect(page.locator(".theme-first-visit")).toHaveCount(0);
});

for (const theme of ["minimal", "character"] as const) {
  test(`mobile viewport remains stable in ${theme} theme`, async ({
    page,
  }, testInfo) => {
    test.skip(testInfo.project.name === "desktop-chromium");
    await finishOnboarding(page, theme);
    const layout = await page.evaluate((activeTheme) => {
      const shell = document.querySelector<HTMLElement>(activeTheme === "character" ? ".congyu-agent" : ".stage6-shell");
      const composer = document.querySelector<HTMLElement>(activeTheme === "character" ? ".congyu-welcome-composer" : ".composer-inner");
      if (!shell || !composer) throw new Error("Agent shell is missing");
      return {
        rootWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
        shellBottom: shell.getBoundingClientRect().bottom,
        composerHeight: composer.getBoundingClientRect().height,
        viewportHeight: window.innerHeight,
      };
    }, theme);
    expect(layout.rootWidth).toBeLessThanOrEqual(layout.clientWidth + 1);
    if (theme === "character") {
      expect(layout.shellBottom).toBeGreaterThanOrEqual(layout.viewportHeight);
    } else {
      expect(layout.shellBottom).toBeCloseTo(layout.viewportHeight, 0);
    }
    expect(layout.composerHeight).toBeGreaterThanOrEqual(76);
  });
}

test("character welcome is a distinct stage with complete safe artwork", async ({
  page,
}, testInfo) => {
  await finishOnboarding(page, "character");
  await expect(page.locator(".congyu-welcome-stage")).toBeVisible();
  await expect(page.locator(".workspace")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "打开会话列表" })).toBeVisible();
  await page.getByRole("button", { name: "打开会话列表" }).click();
  await expect(page.locator(".congyu-history-drawer")).toBeVisible();
  await page.getByRole("button", { name: "关闭会话列表" }).click();

  const layout = await page.evaluate((desktop) => {
    const hero = document.querySelector<HTMLElement>(desktop ? ".congyu-desktop-hero" : ".congyu-mobile-hero");
    const image = hero?.querySelector("img");
    if (!hero || !image) throw new Error("Congyu welcome artwork is missing");
    const heroRect = hero.getBoundingClientRect();
    const introRect = document.querySelector<HTMLElement>(".congyu-intro")?.getBoundingClientRect();
    const composerRect = document.querySelector<HTMLElement>(".congyu-welcome-composer")?.getBoundingClientRect();
    const imageStyle = getComputedStyle(image);
    return {
      heroHeight: heroRect.height,
      objectFit: imageStyle.objectFit,
      naturalWidth: image.naturalWidth,
      introBottom: introRect?.bottom ?? 0,
      heroTop: heroRect.top,
      heroBottom: heroRect.bottom,
      composerTop: composerRect?.top ?? 0,
      rootWidth: document.documentElement.scrollWidth,
      clientWidth: document.documentElement.clientWidth,
    };
  }, testInfo.project.name === "desktop-chromium");
  expect(layout.heroHeight).toBeGreaterThanOrEqual(testInfo.project.name === "desktop-chromium" ? 700 : 480);
  expect(layout.objectFit).toBe("contain");
  expect(layout.rootWidth).toBeLessThanOrEqual(layout.clientWidth + 1);
  if (testInfo.project.name !== "desktop-chromium") {
    expect(layout.naturalWidth).toBeGreaterThanOrEqual(900);
    expect(layout.heroTop).toBeGreaterThanOrEqual(layout.introBottom);
    expect(layout.composerTop).toBeGreaterThanOrEqual(layout.heroBottom);
    const fixedBackground = await page.evaluate(async () => {
      const sky = document.querySelector<HTMLElement>(".congyu-sky");
      if (!sky) throw new Error("Congyu background is missing");
      const before = sky.getBoundingClientRect().top;
      window.scrollTo(0, 420);
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
      const after = sky.getBoundingClientRect().top;
      window.scrollTo(0, 0);
      return { before, after };
    });
    expect(Math.abs(fixedBackground.before)).toBeLessThanOrEqual(1);
    expect(Math.abs(fixedBackground.after)).toBeLessThanOrEqual(1);
  }

  await page.getByRole("button", { name: /我的手帐/ }).click();
  const spaceStickerBounds = await page.evaluate(() => {
    const header = document.querySelector<HTMLElement>(".congyu-space-book > header");
    const sticker = header?.querySelector<HTMLElement>(".congyu-artwork-hello");
    if (!header || !sticker) throw new Error("My space hello sticker is missing");
    const headerRect = header.getBoundingClientRect();
    const stickerRect = sticker.getBoundingClientRect();
    return {
      fullyInside:
        stickerRect.left >= headerRect.left - 1 &&
        stickerRect.right <= headerRect.right + 1 &&
        stickerRect.top >= headerRect.top - 1 &&
        stickerRect.bottom <= headerRect.bottom + 1,
    };
  });
  expect(spaceStickerBounds.fullyInside).toBe(true);
  await page.getByRole("button", { name: "关闭我的空间" }).click();

  await page.addStyleTag({ content: ".congyu-agent img{visibility:hidden!important}" });
  await expect(page.locator(".congyu-welcome-stage")).toBeVisible();
  await expect(page.getByRole("heading", { name: /校园里的事/ })).toBeVisible();
  await expect(page.getByLabel("输入校园问题")).toBeVisible();
});

test("character investigation room keeps notebook and mobile status in budget", async ({
  page,
}, testInfo) => {
  await finishOnboarding(page, "character");
  await page.getByLabel("输入校园问题").fill("工程学院有几个专业");
  await page.getByRole("button", { name: "开始调查" }).click();
  await expect(page.locator(".congyu-agent-room")).toBeVisible();
  await expect(page.locator(".congyu-working-card, .congyu-answer-card")).toBeVisible();

  if (testInfo.project.name === "desktop-chromium") {
    const notebook = page.locator(".congyu-notebook");
    await expect(notebook).toBeVisible();
    const width = await notebook.evaluate((element) => element.getBoundingClientRect().width);
    const artWidth = await page.locator(".congyu-status-page .congyu-artwork").evaluate((element) => element.getBoundingClientRect().width);
    expect(width).toBeGreaterThanOrEqual(360);
    expect(width).toBeLessThanOrEqual(401);
    expect(artWidth).toBeGreaterThanOrEqual(180);
    const animationName = await page.locator(".congyu-status-page .congyu-artwork").evaluate((element) => getComputedStyle(element).animationName);
    expect(animationName).not.toBe("none");
  } else {
    const statusArt = page.locator(".congyu-mobile-status .congyu-artwork");
    await expect(statusArt).toBeVisible();
    const artWidth = await statusArt.evaluate((element) => element.getBoundingClientRect().width);
    expect(artWidth).toBeGreaterThanOrEqual(110);
    const animationName = await statusArt.evaluate((element) => getComputedStyle(element).animationName);
    expect(animationName).not.toBe("none");
    const composerHeight = await page.locator(".congyu-room-composer").evaluate((element) => element.getBoundingClientRect().height);
    expect(composerHeight).toBeGreaterThanOrEqual(76);
  }
});

test("character sources and login use dedicated page compositions", async ({ page }) => {
  await page.addInitScript(() => {
    localStorage.setItem("hzcu-agent-theme", "character");
    localStorage.setItem(
      "hzcu-agent-privacy-consent",
      JSON.stringify({ version: "2026-08-28", acceptedAt: new Date().toISOString() }),
    );
  });
  await page.goto("/sources");
  await expect(page.locator(".congyu-library")).toBeVisible();
  await expect(page.locator(".sources-shell")).toHaveCount(0);
  await page.goto("/login");
  await expect(page.locator(".congyu-login")).toBeVisible();
  await expect(page.locator(".congyu-login-character .congyu-artwork")).toBeVisible();
});

test("character reduced motion removes sweep and displacement", async ({ page }) => {
  await page.emulateMedia({ reducedMotion: "reduce" });
  await finishOnboarding(page, "character");
  const duration = await page.evaluate(() => {
    const hero = [...document.querySelectorAll<HTMLElement>(".congyu-mobile-hero, .congyu-desktop-hero")].find((element) => element.getBoundingClientRect().height > 0);
    if (!hero) throw new Error("Visible Congyu hero is missing");
    return getComputedStyle(hero).animationDuration;
  });
  expect(parseFloat(duration)).toBeLessThanOrEqual(0.01);
});

test("character artwork failure keeps content and actions usable", async ({ page }) => {
  await page.route("**/_next/image**", (route) => route.abort());
  await finishOnboarding(page, "character");
  await expect(page.getByRole("heading", { name: /校园里的事/ })).toBeVisible();
  await expect(page.getByLabel("输入校园问题")).toBeVisible();
  await page.getByLabel("输入校园问题").fill("图片不可用时仍可提问");
  await expect(page.getByRole("button", { name: "开始调查" })).toBeEnabled();
  await expect(page.locator(".congyu-welcome-stage")).toBeVisible();
});

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

test("theme switch keeps the current draft and product state", async ({ page }) => {
  await finishOnboarding(page, "minimal");
  const draft = "这段问题在切换主题后仍应保留";
  await page.getByLabel("输入校园问题").fill(draft);

  const mobileMenu = page.getByRole("button", { name: "打开会话历史" });
  if (await mobileMenu.isVisible()) await mobileMenu.click();
  await page.getByRole("button", { name: /我的空间/ }).click();
  await page
    .getByRole("navigation", { name: "我的空间分区" })
    .getByRole("button", { name: /界面/ })
    .click();
  await page.getByRole("radio", { name: /琮羽主题/ }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "character");
  await page.getByRole("button", { name: "关闭我的空间" }).click();

  await expect(page.getByLabel("输入校园问题")).toHaveValue(draft);
});

test("deleting personal data keeps the device theme and clears local workspace", async ({ page }) => {
  await finishOnboarding(page, "character");
  await page.getByRole("button", { name: /我的手帐/ }).click();
  await page
    .getByRole("navigation", { name: "我的空间分区" })
    .getByRole("button", { name: "数据" })
    .click();
  page.once("dialog", (dialog) => dialog.accept());
  await page.getByRole("button", { name: "删除全部个人数据" }).click();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "character");
  await expect(page.locator(".congyu-welcome-stage")).toBeVisible({ timeout: 15_000 });
  await expect(page.locator(".congyu-history-drawer > div > button")).toHaveCount(0);
});

test("mobile agent shell keeps the root viewport locked", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name === "desktop-chromium");
  await finishOnboarding(page);

  await expect(page.locator("body")).toHaveCSS("position", "fixed");
  await page.evaluate(() => window.scrollTo({ top: 160 }));

  const layout = await page.evaluate(() => {
    const shell = document.querySelector<HTMLElement>(".stage6-shell");
    const composer = document.querySelector<HTMLElement>(".composer-inner");
    if (!shell || !composer) throw new Error("Agent shell is missing");
    const shellRect = shell.getBoundingClientRect();
    return {
      composerHeight: composer.getBoundingClientRect().height,
      scrollY: window.scrollY,
      shellBottom: shellRect.bottom,
      viewportHeight: window.innerHeight,
    };
  });

  expect(layout.scrollY).toBe(0);
  expect(layout.shellBottom).toBeCloseTo(layout.viewportHeight, 0);
  expect(layout.composerHeight).toBeGreaterThanOrEqual(76);
});
