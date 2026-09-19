import { expect, test } from "@playwright/test";
import { PRIVACY_NOTICE_VERSION } from "../lib/privacy-consent";

test("privacy precedes announcement fetch; acknowledgements survive reload", async ({ page }) => {
  let reads = 0;
  let requests = 0;
  let failOnce = true;
  await page.addInitScript(() => {
    localStorage.setItem("hzcu-agent-theme", "minimal");
  });
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, body: "offline fixture" }));
  await page.route("**/api/v1/announcements/unread", (route) => {
    requests++;
    return route.fulfill({ json: reads ? [] : [{
      id: "notice_test", title: "测试公告", content: "第一行\n<script>alert(1)</script>",
      active: true, created_at: "2026-09-19T00:00:00Z",
    }] });
  });
  await page.route("**/api/v1/announcements/notice_test/read", (route) => {
    if (failOnce) { failOnce = false; return route.fulfill({ status: 503, body: "retry" }); }
    reads++;
    return route.fulfill({ status: 204 });
  });
  await page.goto("/");
  await expect(page.getByRole("dialog", { name: /使用前，请先了解这些边界/ })).toBeVisible();
  expect(requests).toBe(0);
  await page.getByRole("checkbox", { name: /我已完整阅读并同意/ }).check();
  await page.getByRole("button", { name: "接受并继续" }).click();
  const notice = page.getByRole("dialog", { name: "测试公告" });
  await expect(notice).toBeVisible();
  await expect(notice.locator(".announcement-body")).toContainText("<script>alert(1)</script>");
  await expect(notice.locator("script")).toHaveCount(0);
  await page.keyboard.press("Escape");
  await expect(notice).toBeVisible();
  const confirm = notice.getByRole("button", { name: "确认已读，进入网站" });
  await expect(confirm).toBeDisabled();
  await notice.getByRole("checkbox").check();
  await confirm.click();
  await expect(notice.getByRole("alert")).toBeVisible();
  await expect(notice).toBeVisible();
  await confirm.click();
  await expect(notice).toHaveCount(0);
  expect(reads).toBe(1);
  await page.reload();
  await expect(page.locator(".stage6-shell")).toBeVisible();
  await expect(notice).toHaveCount(0);
});

test("multiple announcements require individual confirmations", async ({ page }) => {
  await page.addInitScript((version) => {
    localStorage.setItem("hzcu-agent-theme", "minimal");
    localStorage.setItem("hzcu-agent-privacy-consent", JSON.stringify({ version, acceptedAt: new Date().toISOString() }));
  }, PRIVACY_NOTICE_VERSION);
  await page.route("**/api/v1/**", (route) => route.fulfill({ status: 503, body: "offline fixture" }));
  await page.route("**/api/v1/announcements/unread", (route) => route.fulfill({ json: [1, 2].map((n) => ({
    id: `notice_${n}`, title: `公告${n}`, content: "正文", active: true, created_at: "2026-09-19T00:00:00Z",
  })) }));
  await page.route("**/api/v1/announcements/*/read", (route) => route.fulfill({ status: 204 }));
  await page.goto("/");
  await expect(page.getByRole("dialog", { name: "公告1" })).toBeVisible();
  await page.locator(".announcement-dialog input").check();
  await page.getByRole("button", { name: "确认已读，查看下一条" }).click();
  await expect(page.getByRole("dialog", { name: "公告2" })).toBeVisible();
  await expect(page.locator(".announcement-dialog input")).not.toBeChecked();
});
