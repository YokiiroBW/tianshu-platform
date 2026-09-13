import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { resolveRoute } from "../src/app/modules";

test("route boundaries reject unknown and malformed addresses", () => {
  expect(resolveRoute("")?.module.id).toBe("workbench");
  expect(resolveRoute("#/settings/1")?.section).toBe(1);
  for (const hash of [
    "#/missing",
    "#/room/0",
    "#/settings/-1",
    "#/settings/9",
    "#/home/0/extra",
    "#/%ZZ",
  ])
    expect(resolveRoute(hash)).toBeNull();
});

test("unconfigured shell, lazy room, no business requests or media", async ({
  page,
}) => {
  const requests: string[] = [];
  page.on("request", (request) => requests.push(request.url()));
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "接入前的准备" }),
  ).toBeVisible();
  expect(requests.some((url) => /RoomPage-/.test(url))).toBe(false);
  await expect(page.locator("canvas, video, audio")).toHaveCount(0);
  await page.goto("/#/room");
  await expect(
    page.getByRole("heading", { name: "小屋尚未开放" }),
  ).toBeVisible();
  expect(requests.some((url) => /RoomPage-/.test(url))).toBe(true);
  expect(requests.filter((url) => /\/api\/|^https:/.test(url))).toEqual([]);
  await page.getByRole("link", { name: "前往陪伴" }).click();
  await expect(
    page.getByRole("heading", { name: "文字对话尚未接入" }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: /实时语音|共同观影/ }),
  ).toHaveCount(0);
});

test("connection search exposes real empty result and reset", async ({
  page,
}) => {
  await page.goto("/#/settings/1");
  await expect(page.locator(".connection-list li")).toHaveCount(5);
  await page.getByRole("searchbox").fill("不存在的连接类型".repeat(12));
  await expect(
    page.getByRole("heading", { name: "没有匹配的连接类型" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "清除筛选" }).click();
  await expect(page.locator(".connection-list li")).toHaveCount(5);
  await page.goto("/#/home/1");
  await expect(
    page.getByText("容器运行、健康检查和观测时间将分别展示；当前状态未知。"),
  ).toBeVisible();
});

test("drawer keyboard trap, Escape return, theme persistence and motion", async ({
  page,
}) => {
  await page.goto("/");
  const trigger = page.getByRole("button", { name: "外观设置", exact: true });
  await trigger.focus();
  await page.keyboard.press("Enter");
  const dialog = page.getByRole("dialog", { name: "外观设置" });
  await expect(dialog).toBeVisible();
  for (let index = 0; index < 9; index++) {
    await page.keyboard.press("Tab");
    expect(
      await dialog.evaluate((element) =>
        element.contains(document.activeElement),
      ),
    ).toBe(true);
  }
  await dialog.getByRole("radio", { name: "深色", exact: true }).check();
  await dialog.getByRole("checkbox", { name: /减少透明度/ }).check();
  expect(
    await dialog.evaluate(
      (element) => getComputedStyle(element).animationDuration,
    ),
  ).toBe("0s");
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
  await page.reload();
  await expect(page.locator("html")).toHaveAttribute("data-theme", "dark");
  await expect(page.locator("html")).toHaveAttribute(
    "data-transparency",
    "reduce",
  );
});

test("mobile menu closes after keyboard navigation and route focus moves", async ({
  page,
}, testInfo) => {
  test.skip(testInfo.project.name !== "mobile");
  await page.goto("/");
  await page.getByRole("button", { name: "打开导航" }).click();
  const dialog = page.getByRole("dialog", { name: "导航", exact: true });
  await dialog.getByRole("link", { name: "记忆", exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(dialog).not.toBeVisible();
  await expect(page.locator("main")).toBeFocused();
  await expect(
    page.getByRole("heading", { name: "记忆星图尚未接入" }),
  ).toBeVisible();
});

test("failed chunk retains navigation and reload recovery", async ({
  page,
}) => {
  await page.route("**/RoomPage-*.js", (route) => route.abort());
  await page.goto("/#/room");
  await expect(page.getByRole("alert")).toContainText("页面未能加载");
  await page.unroute("**/RoomPage-*.js");
  await page.getByRole("button", { name: "重新加载", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "小屋尚未开放" }),
  ).toBeVisible();
});

test("pending chunk shows loading then real unconfigured state", async ({
  page,
}) => {
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/RoomPage-*.js", async (route) => {
    await pending;
    await route.continue();
  });
  await page.goto("/#/room", { waitUntil: "domcontentloaded" });
  await expect(page.getByRole("status")).toContainText("正在加载页面");
  release();
  await expect(
    page.getByRole("heading", { name: "小屋尚未开放" }),
  ).toBeVisible();
});

test("unknown route, offline notice and blocked preference storage", async ({
  page,
  context,
}) => {
  await page.addInitScript(() => {
    Storage.prototype.setItem = () => {
      throw new Error("blocked");
    };
  });
  await page.goto("/#/no-such-module");
  await expect(
    page.getByRole("heading", { name: "找不到这个页面" }),
  ).toBeVisible();
  await page.getByRole("link", { name: "返回工作台", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "接入前的准备" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "外观设置", exact: true }).click();
  await expect(page.getByRole("status")).toContainText("浏览器未允许保存偏好");
  await page.keyboard.press("Escape");
  await context.setOffline(true);
  await expect(page.getByRole("status")).toContainText("浏览器当前离线");
  await context.setOffline(false);
  await expect(
    page.getByText("浏览器当前离线。已加载的页面仍可浏览。"),
  ).not.toBeVisible();
});

test("representative light/dark layout, 200% text and accessibility", async ({
  page,
}, testInfo) => {
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "接入前的准备" }),
  ).toBeVisible();
  for (const theme of ["light", "dark"]) {
    await page.getByRole("button", { name: "外观设置", exact: true }).click();
    await page
      .getByRole("radio", {
        name: theme === "light" ? "浅色" : "深色",
        exact: true,
      })
      .check();
    expect(
      (
        await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
          .analyze()
      ).violations,
    ).toEqual([]);
    await page.keyboard.press("Escape");
    expect(
      (
        await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
          .analyze()
      ).violations,
    ).toEqual([]);
    await page.screenshot({
      path: testInfo.outputPath(`${theme}.png`),
      fullPage: true,
    });
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "200%";
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "";
    });
  }
});
