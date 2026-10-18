/** Real console/session/CSRF/GraphQL adapter/queue/files; only external peers are synthetic. */
import { test, expect } from "@playwright/test";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

test("comic search, page preview, subscription, lossless folder import and reader receipts", async ({
  page,
}, info) => {
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.goto("/#/subscriptions/9");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "漫画订阅", exact: true }),
  ).toBeVisible();
  await page.getByLabel("作品或作者", { exact: true }).fill("星际");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(page.locator(".comic-card")).toHaveCount(1);
  await expect(page.locator(".comic-card")).toContainText("星际邮差");
  await expect(page.locator(".comic-card img")).toHaveJSProperty(
    "naturalWidth",
    81,
  );
  await page.screenshot({
    path: info.outputPath("comics-search.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "查看章节与预览" }).click();
  await expect(page.locator(".comic-chapter")).toHaveCount(2);
  await page.getByRole("button", { name: "预览前 3 页" }).first().click();
  await expect(
    page.getByRole("img", { name: "第 1 页", exact: true }),
  ).toHaveJSProperty("naturalWidth", 81);
  await page.getByRole("button", { name: "关闭预览" }).click();
  await page
    .getByRole("button", { name: "订阅追更", exact: true })
    .last()
    .click();
  await expect(
    page.getByRole("status").filter({ hasText: "已保存追更设置" }),
  ).toBeVisible();
  await expect
    .poll(
      async () => {
        await page.getByRole("button", { name: "刷新章节与阅读状态" }).click();
        return page
          .getByRole("link", { name: "在 Komga 阅读", exact: true })
          .count();
      },
      { timeout: 45000, intervals: [1000, 2000] },
    )
    .toBe(2);
  await expect(
    page.getByRole("link", { name: "在 Komga 阅读", exact: true }).first(),
  ).toHaveAttribute("href", /\/book\/.+\/read$/);
  await page.screenshot({
    path: info.outputPath("comics-detail.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "返回列表" }).click();
  await page
    .getByRole("navigation", { name: "漫画订阅功能" })
    .getByRole("button", { name: "订阅追更" })
    .click();
  await expect(page.locator(".comic-stack article")).toContainText("星际邮差");
  await page.getByRole("button", { name: "暂停追更" }).click();
  await expect(page.getByRole("button", { name: "继续追更" })).toBeVisible();
  await page.getByRole("button", { name: "继续追更" }).click();
  await page.getByRole("button", { name: "导入图片与 CBZ" }).click();
  await page.getByRole("button", { name: "检查导入", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "导入预览 · 1 章" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "无损转换并归档" }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "原图片和漫画包保持原样" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "漫画书库", exact: true }).click();
  await expect(
    page.locator(".comic-card").filter({ hasText: "我的合成图集" }),
  ).toBeVisible();
  await page
    .locator(".comic-card")
    .filter({ hasText: "我的合成图集" })
    .getByRole("button", { name: "查看章节与预览" })
    .click();
  await expect
    .poll(
      async () => {
        await page.getByRole("button", { name: "刷新章节与阅读状态" }).click();
        return page
          .getByRole("link", { name: "在 Komga 阅读", exact: true })
          .count();
      },
      { timeout: 30000, intervals: [1000, 2000] },
    )
    .toBe(1);
  const fixture = JSON.parse(
    readFileSync(resolve(".runtime/comic-browser-input.json"), "utf8"),
  );
  expect(readdirSync(resolve(fixture.input, "我的合成图集")).sort()).toEqual([
    "1.png",
    "10.png",
    "2.png",
  ]);
  await page.setViewportSize({ width: 393, height: 852 });
  await page.screenshot({
    path: info.outputPath("comics-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(errors).toEqual([]);
});

test("comic preview files require the live console login", async ({
  request,
}) => {
  const response = await request.get(
    "/api/web/media/comics/files/" + "a".repeat(24) + "/cover",
  );
  expect(response.status()).toBe(401);
});
