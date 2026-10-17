/** No media API interception: console auth, rule subprocess, queue and EPUB are real. */
import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { selectMediaSection } from "./media.fixtures";

test("book subscription in existing shell searches, previews, subscribes, publishes and imports", async ({
  page,
  request,
}, info) => {
  const fixture = JSON.parse(
    readFileSync(resolve(".runtime/book-browser-input.json"), "utf8"),
  );
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/#/subscriptions/8");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "书籍订阅", exact: true, level: 2 }),
  ).toBeVisible();
  await page.getByRole("button", { name: "书源管理", exact: true }).click();
  await page.getByRole("button", { name: "导入书源", exact: true }).click();
  await page.getByLabel("规则 JSON").fill("{invalid");
  await page.getByRole("button", { name: "导入规则", exact: true }).click();
  await expect(page.getByText(/JSON 格式有误/)).toBeVisible();
  await page.getByLabel("规则 JSON").fill(JSON.stringify(fixture.source));
  await page.getByRole("button", { name: "导入规则", exact: true }).click();
  await expect(
    page.getByText("导入或更新 1 项，0 项格式不符合要求。"),
  ).toBeVisible();
  await page.getByRole("button", { name: "关闭导入阅读书源" }).click();
  await page
    .getByRole("button", { name: "聚合搜索", exact: true })
    .first()
    .click();
  await page.getByLabel("书名、作者或 ISBN", { exact: true }).fill("山海");
  await page
    .locator("form.book-search-form")
    .getByRole("button", { name: "聚合搜索" })
    .click();
  await expect(page.locator(".book-card h3")).toHaveText("山海之间");
  await page.screenshot({
    path: info.outputPath("books-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "预览与资料" }).click();
  const drawer = page.getByRole("dialog");
  await expect(drawer.locator(".book-prose")).toContainText("清晨的雾气", {
    timeout: 20000,
  });
  await page.screenshot({
    path: info.outputPath("books-reader-desktop.png"),
    fullPage: true,
  });
  await drawer.getByRole("button", { name: "订阅并下载" }).click();
  await expect(page.getByLabel("书籍存档目录")).toBeVisible();
  await page.getByLabel("书籍存档目录").fill(fixture.directory);
  await page.getByRole("button", { name: "保存整理设置" }).click();
  await expect(page.locator(".book-subscription h3")).toHaveText("山海之间");
  await page.getByRole("button", { name: "暂停追更" }).click();
  await expect(page.getByRole("button", { name: "恢复追更" })).toBeVisible();
  await page.getByRole("button", { name: "恢复追更" }).click();
  await page
    .locator(".book-page-heading")
    .getByRole("link", { name: "下载中心" })
    .click();
  await expect(
    page.locator(".media-job").getByText("已归档", { exact: true }),
  ).toBeVisible({ timeout: 30000 });
  await expect(page.locator(".media-job")).toContainText("2 / 2 章");
  await page.getByRole("button", { name: "查看任务详情" }).click();
  const epubHref = await page
    .getByRole("link", { name: "下载 EPUB", exact: true })
    .getAttribute("href");
  expect((await request.get(epubHref!)).status()).toBe(401);
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("link", { name: "下载 EPUB", exact: true }).click();
  const download = await downloadEvent;
  expect(download.suggestedFilename()).toContain("山海之间");
  await download.saveAs(info.outputPath("actual-book.epub"));
  await page.getByRole("link", { name: "打开书籍订阅与资料" }).click();
  await page.getByRole("button", { name: "本地书库", exact: true }).click();
  await expect(page.locator(".book-card h3")).toHaveText("山海之间");
  await page.getByRole("button", { name: "预览与资料" }).click();
  await drawer.getByRole("tab", { name: "书籍资料" }).click();
  await drawer.getByLabel("书名", { exact: true }).fill("山海之间·校订");
  await drawer.getByRole("button", { name: "保存选定资料" }).click();
  await expect(drawer.getByText("已人工选择", { exact: true })).toBeVisible();
  await page.keyboard.press("Escape");
  await page.locator("input[type=file]").setInputFiles({
    name: "合成短篇.txt",
    mimeType: "text/plain",
    buffer: Buffer.from(
      "第一章 抵达\n旅人在雨停时走进小镇。\n第二章 夜色\n街边的灯渐渐亮起。",
    ),
  });
  await expect(
    page.getByRole("heading", { name: "合成短篇", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: info.outputPath("books-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "预览与资料" }).first().click();
  await expect(drawer.locator(".book-prose")).toContainText("旅人");
  await page.screenshot({
    path: info.outputPath("books-reader-mobile.png"),
    fullPage: true,
  });
  expect(
    await drawer.evaluate(
      (element) => element.scrollWidth <= element.clientWidth,
    ),
  ).toBe(true);
  await page.keyboard.press("Escape");
  await selectMediaSection(page, "视频订阅");
  await expect(
    page.getByText("视频订阅服务尚未配置", { exact: true }),
  ).toBeVisible();
  await selectMediaSection(page, "书籍订阅");
  await expect(
    page.getByRole("heading", { name: "书籍订阅", exact: true, level: 2 }),
  ).toBeVisible();
  expect(errors).toEqual([]);
});
