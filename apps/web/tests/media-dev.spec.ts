import { test, expect } from "@playwright/test";
import { installMediaFixture } from "./media.fixtures";

test("media StrictMode remount reads facts and clears cancelled loading", async ({
  page,
}) => {
  const state = await installMediaFixture(page);
  await page.goto("/#/subscriptions/1");
  await expect(
    page.getByRole("link", { name: "链接下载", exact: true }),
  ).toBeVisible();
  await page.getByRole("link", { name: /下载任务/ }).click();
  await expect(
    page.getByRole("heading", { name: "隔离样例 · 城市与山海", exact: true }),
  ).toBeVisible();
  await page.evaluate(() => {
    location.hash = "#/workbench";
  });
  await expect(page.getByRole("link", { name: /下载任务/ })).toHaveCount(0);
  await page.evaluate(() => {
    location.hash = "#/subscriptions";
  });
  await expect(
    page.getByRole("link", { name: "链接下载", exact: true }),
  ).toBeVisible();
  expect(
    state.calls.filter((call) => call.operation === "view").length,
  ).toBeGreaterThanOrEqual(2);
});
