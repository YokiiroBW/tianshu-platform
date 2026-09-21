import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

/**
 * 人格目录与版本比较的浏览器验收。
 *
 * 三个控制台都是真实部署差异（4820 可读、4821 未配置、4822 未授权），角色服务是真实 TLS 上的
 * 合成装置，浏览器只发同源四个只读请求。这里检查的是页面**说出来的话**：读不到、没授权、位置过期
 * 与「确实没有记录」必须是四种不同的状态，任何一种都不会被显示成空目录或空历史。
 */

const ADMIN = "synthetic-admin";
const ADMIN_PASSWORD = "synthetic-local-password-014";
const TOKEN = "synthetic-ts025-persona-admin-credential";
const PEER = "127.0.0.1:4823";
const UNCONFIGURED = "http://127.0.0.1:4821";
const FORBIDDEN = "http://127.0.0.1:4822";

async function login(page: Page) {
  await page.getByLabel("管理员账号").fill(ADMIN);
  await page.getByLabel("密码", { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  // Being logged in is not authority: every test states the deployment state it actually got.
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
}

function rail(page: Page, label: string) {
  return page.locator(".persona-state .rail-label").filter({ hasText: label });
}

function subject(page: Page, id: string) {
  return page.locator(".persona-subject").filter({ hasText: id });
}

function rows(page: Page) {
  return page.locator(".persona-rows > li");
}

/** The history pager, distinct from the directory pager that shares the button words. */
function historyPager(page: Page) {
  return page.locator(".persona-history .persona-pager");
}

/** What actually overflows, so a failure names the element instead of only the page width. */
async function overflow(page: Page) {
  return page.evaluate(() => {
    const inner = window.innerWidth;
    const name = (element: HTMLElement) =>
      `${element.tagName.toLowerCase()}.${String(element.className).split(" ")[0]}`;
    const roots: string[] = [];
    for (const element of document.querySelectorAll<HTMLElement>("body *")) {
      const box = element.getBoundingClientRect();
      if (box.right <= inner + 1) continue;
      const parent = element.parentElement?.getBoundingClientRect();
      // The outermost overflowing element is the one whose parent still fits.
      if (parent && parent.right > inner + 1) continue;
      const style = getComputedStyle(element);
      roots.push(
        `${name(element)} w=${Math.round(box.width)} parent=${Math.round(parent?.width ?? 0)} min=${style.minWidth} ws=${style.whiteSpace}`,
      );
    }
    const scrolling: string[] = [];
    for (const element of document.querySelectorAll<HTMLElement>("body *")) {
      if (
        element.scrollWidth > element.clientWidth + 1 &&
        element.clientWidth > 0
      )
        scrolling.push(
          `${name(element)} client=${element.clientWidth} scroll=${element.scrollWidth}`,
        );
    }
    return {
      scrollWidth: document.documentElement.scrollWidth,
      inner,
      roots: roots.slice(0, 6),
      scrolling: scrolling.slice(0, 6),
    };
  });
}

async function noOverflow(page: Page) {
  const measured = await overflow(page);
  expect(measured.scrollWidth, JSON.stringify(measured)).toBeLessThanOrEqual(
    measured.inner + 1,
  );
}

async function openAlpha(page: Page) {
  // The first directory row is the character the fixture seeds with a full history.
  await page.locator(".persona-subject").first().click();
  await expect(rows(page).first()).toBeVisible();
}

test("目录按 20 个一页读取，第二页的缺失角色被明说，回答里没有凭据、地址或本地路径", async ({
  page,
}, testInfo) => {
  const bodies: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/web/personas/"))
      void response
        .text()
        .then((text) => bodies.push(text))
        .catch(() => undefined);
  });
  await page.goto("/#/companion/2");
  await login(page);
  await expect(rail(page, "可读")).toBeVisible();
  await expect(page.locator(".persona-state")).toContainText(
    "已授权角色 22 个 · 每页 20 个",
  );
  await expect(page.locator(".persona-subject")).toHaveCount(20);
  await expect(page.locator(".persona-catalog .persona-pager")).toContainText(
    "第 1 页 · 本页 20 个角色 · 共 22 个",
  );
  // One unusable answer is a stated row, never a hidden one.
  await expect(subject(page, "actor:broken")).toContainText("回答无法使用");
  await page.getByRole("button", { name: "下一页角色" }).click();
  await expect(page.locator(".persona-subject")).toHaveCount(2);
  await expect(page.locator(".persona-catalog .persona-pager")).toContainText(
    "第 2 页 · 本页 2 个角色",
  );
  await expect(subject(page, "actor:missing")).toContainText(
    "角色服务里没有这个角色",
  );
  await page.screenshot({
    path: testInfo.outputPath("directory-page-2.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "上一页角色" }).click();
  await expect(page.locator(".persona-subject")).toHaveCount(20);
  await expect.poll(() => bodies.length).toBeGreaterThan(1);
  for (const body of bodies) {
    for (const secret of [
      TOKEN,
      "Bearer",
      PEER,
      "localhost.pem",
      "characters-local",
    ])
      expect(body).not.toContain(secret);
  }
});

test("四类历史各自成页，选中版本只显示它真正的字段", async ({
  page,
}, testInfo) => {
  await page.goto("/#/companion/2");
  await login(page);
  await openAlpha(page);
  await expect(rows(page)).toHaveCount(20);
  await expect(historyPager(page)).toContainText(
    "第 1 页 · 本页 20 条 · 基线版本",
  );
  await historyPager(page).getByRole("button", { name: "下一页" }).click();
  await expect(rows(page)).toHaveCount(5);
  await historyPager(page)
    .getByRole("button", { name: "重新打开第一页" })
    .click();
  await expect(rows(page)).toHaveCount(20);
  await page.getByRole("button", { name: "发布", exact: true }).click();
  await expect(rows(page)).toHaveCount(4);
  await expect(rows(page).first()).toContainText("第 1 次发布");
  await expect(rows(page).last()).toContainText("第 4 次发布");
  await page.getByRole("button", { name: "批准", exact: true }).click();
  // Three approvals and one rejection, all four kept as their own kind of record.
  await expect(rows(page)).toHaveCount(4);
  // A rejection is never displayed as an approval, and only the rejected one says 驳回.
  await expect(rows(page).first()).toContainText("批准");
  await expect(rows(page).last()).toContainText("驳回");
  await page.getByRole("button", { name: "回退", exact: true }).click();
  await expect(rows(page)).toHaveCount(1);
  await expect(rows(page).first()).toContainText("回退");
  await page.getByRole("button", { name: "修订", exact: true }).click();
  await expect(rows(page)).toHaveCount(20);
  await rows(page).first().getByRole("button", { name: "查看这一版" }).click();
  const panel = page.locator(".persona-revision");
  await expect(panel).toContainText("角色 A");
  await expect(panel).toContainText("这一版没有这个字段");
  await expect(panel).toContainText("这一版没有本页不解释的字段");
  await page.screenshot({
    path: testInfo.outputPath("history-and-revision.png"),
    fullPage: true,
  });
  // Switching character clears the chosen version: beta never shows alpha's revision.
  await subject(page, "actor:beta").click();
  await expect(rows(page).first()).toBeVisible();
  await expect(page.locator(".persona-revision")).toHaveCount(0);
  await expect(page.locator(".persona-detail")).not.toContainText("角色 A");
  await expect(page.locator(".persona-detail")).toContainText("尚未选择版本");
});

test("比较两版：改动、移除和本页不解释的字段都逐项列出", async ({
  page,
}, testInfo) => {
  await page.goto("/#/companion/2");
  await login(page);
  await openAlpha(page);
  // The second row still carries 称呼; the last revision drops it and adds a field.
  await rows(page).nth(1).getByRole("button", { name: "设为对比基线" }).click();
  await expect(page.locator(".persona-notice")).toContainText("设为对比基线");
  await historyPager(page).getByRole("button", { name: "下一页" }).click();
  await expect(rows(page)).toHaveCount(5);
  await rows(page).last().getByRole("button", { name: "与基线比较" }).click();
  const comparison = page.locator(".persona-revision");
  await expect(comparison).toContainText("语气 · 改动");
  await expect(comparison).toContainText("表达 · 相同");
  await expect(comparison).toContainText("称呼 · 移除");
  await expect(comparison).toContainText(
    "另有本页不解释的字段：extension_flag。",
  );
  const removed = comparison
    .locator(".persona-change")
    .filter({ hasText: "称呼 · 移除" });
  await expect(removed).toContainText("基线");
  await expect(removed).toContainText("对照（缺）");
  await page.screenshot({
    path: testInfo.outputPath("comparison.png"),
    fullPage: true,
  });
});

test("未配置与未授权是两种明说的状态，不是空目录", async ({ page }) => {
  await page.goto(`${UNCONFIGURED}/#/companion/2`);
  await login(page);
  await expect(rail(page, "未配置")).toBeVisible();
  await expect(page.locator(".persona-state")).toContainText(
    "personas_not_configured",
  );
  await expect(page.locator(".state-panel")).toContainText(
    "人格页需要部署登记一个角色服务",
  );
  await expect(page.locator(".persona-subject")).toHaveCount(0);
  await page.goto(`${FORBIDDEN}/#/companion/2`);
  await login(page);
  await expect(rail(page, "未授权")).toBeVisible();
  await expect(page.locator(".persona-state")).toContainText(
    "persona_read_required",
  );
  await expect(page.locator(".state-panel h2")).toHaveText("未授权");
  await expect(page.locator(".persona-subject")).toHaveCount(0);
});

test("角色服务整体读不到时是读取失败，恢复后重新可读", async ({
  page,
  request,
}) => {
  await page.goto("/#/companion/2");
  await login(page);
  await expect(page.locator(".persona-subject")).toHaveCount(20);
  // The peer speaks real TLS, so its control path needs the fixture certificate to be trusted.
  const control = `https://${PEER}/control/scenario`;
  const options = { data: { scenario: "unreadable" }, ignoreHTTPSErrors: true };
  await request.post(control, options);
  await page.getByRole("button", { name: "重新读取" }).click();
  await expect(rail(page, "读取失败")).toBeVisible();
  await expect(page.locator(".persona-state")).toContainText(
    "invalid_upstream",
  );
  // The rows left over from the good read are never passed off as this read's result.
  await expect(page.locator(".persona-state")).toContainText(
    "上一次成功读取的结果",
  );
  await expect(page.getByText("目录为空")).toHaveCount(0);
  await expect(page.getByText("目录读取失败")).toHaveCount(0);
  await request.post(control, {
    data: { scenario: "ready" },
    ignoreHTTPSErrors: true,
  });
  await page.getByRole("button", { name: "重新读取" }).click();
  await expect(rail(page, "可读")).toBeVisible();
  await expect(page.locator(".persona-state")).not.toContainText(
    "上一次成功读取的结果",
  );
  await expect(page.locator(".persona-subject")).toHaveCount(20);
});

test("目录一次都读不到时是读取失败，不是空目录", async ({ page }) => {
  let refused = 0;
  await page.route("**/api/web/personas/catalog", async (route) => {
    refused += 1;
    await route.fulfill({
      status: 503,
      json: { code: "dependency_unavailable" },
    });
  });
  await page.goto("/#/companion/2");
  await login(page);
  await expect(rail(page, "读取失败")).toBeVisible();
  await expect(page.locator(".persona-state")).toContainText(
    "dependency_unavailable",
  );
  await expect(page.locator(".state-panel")).toContainText("目录读取失败");
  await expect(page.locator(".persona-subject")).toHaveCount(0);
  await expect(page.getByText("目录为空")).toHaveCount(0);
  await expect(page.getByText("上一次成功读取的结果")).toHaveCount(0);
  // One refusal is one answer: a failed read is not retried behind the operator's back.
  await page.waitForTimeout(600);
  expect(refused).toBe(1);
});

test("续读位置过期时要求手动重开，不自动翻页", async ({ page }) => {
  await page.goto("/#/companion/2");
  await login(page);
  await openAlpha(page);
  await expect(rows(page)).toHaveCount(20);
  let asked = 0;
  await page.route("**/api/web/personas/history", async (route) => {
    asked += 1;
    await route.fulfill({
      status: 409,
      json: { code: "version_conflict", message: "人格已经变化" },
    });
  });
  await historyPager(page).getByRole("button", { name: "下一页" }).click();
  await expect(rail(page, "版本已过期")).toBeVisible();
  const reopen = page
    .locator(".persona-detail > .status-rail")
    .getByRole("button", { name: "重新打开第一页" });
  await expect(reopen).toBeVisible();
  await page.waitForTimeout(600);
  expect(asked).toBe(1);
  await page.unroute("**/api/web/personas/history");
  await reopen.click();
  await expect(rows(page)).toHaveCount(20);
  // The stale notice is gone: what stays in the detail pane is only the "no version chosen" rail.
  await expect(
    page.getByText("这里不会自动翻页，请重新打开第一页。"),
  ).toHaveCount(0);
  await expect(
    page
      .locator(".persona-detail .rail-label")
      .filter({ hasText: "版本已过期" }),
  ).toHaveCount(0);
});

test("登录被撤销后不留下旧面板", async ({ page }) => {
  await page.goto("/#/companion/2");
  await login(page);
  await expect(page.locator(".persona-subject")).toHaveCount(20);
  await page.route("**/api/web/personas/catalog", async (route) => {
    await route.fulfill({ status: 401, json: { code: "session_expired" } });
  });
  await page.route("**/api/web/session", async (route) => {
    await route.fulfill({ json: { authenticated: false, csrf: "stub-csrf" } });
  });
  await page.getByRole("button", { name: "重新读取" }).click();
  await expect(page.locator(".persona-state")).toContainText("登录已过期");
  await expect(page.getByLabel("管理员账号")).toBeVisible();
  await expect(page.locator(".persona-subject")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "退出登录" })).toHaveCount(0);
});

test("浅色与深色、四种宽度都不横向溢出，且没有 axe 违规", async ({
  page,
}, testInfo) => {
  await page.goto("/#/companion/2");
  await login(page);
  await openAlpha(page);
  await expect(rows(page)).toHaveCount(20);
  for (const theme of ["light", "dark"] as const) {
    await page.getByRole("button", { name: "外观设置", exact: true }).click();
    await page
      .getByRole("radio", {
        name: theme === "light" ? "浅色" : "深色",
        exact: true,
      })
      .check();
    await page.keyboard.press("Escape");
    expect(
      (
        await new AxeBuilder({ page })
          .withTags(["wcag2a", "wcag2aa", "wcag21aa", "wcag22aa"])
          .analyze()
      ).violations,
    ).toEqual([]);
    for (const size of [
      { width: 1440, height: 1000 },
      { width: 1024, height: 768 },
      { width: 390, height: 844 },
      { width: 320, height: 640 },
    ]) {
      await page.setViewportSize(size);
      await noOverflow(page);
      await page.screenshot({
        path: testInfo.outputPath(`personas-${theme}-${size.width}.png`),
        fullPage: true,
      });
    }
    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.screenshot({
      path: testInfo.outputPath(`personas-${theme}.png`),
      fullPage: true,
    });
    // 200% text is still readable without a horizontal scrollbar.
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "200%";
    });
    await noOverflow(page);
    await page.evaluate(() => {
      document.documentElement.style.fontSize = "";
    });
  }
});
