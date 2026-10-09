import { test, expect, type Page } from "@playwright/test";
import { resolveRoute } from "../src/app/modules";
import type { TaskDetail, TaskSource } from "../src/features/settings/api";
import {
  fixtureSubscription,
  fixtureView,
  installMediaFixture,
} from "./media.fixtures";

const sections = ["订阅管理", "链接下载", "下载任务", "账号与媒体库"];
const navigation = (page: Page) =>
  page.getByRole("navigation", { name: "订阅页面" });
function subscriptions(count = 12) {
  const view = fixtureView();
  view.subscriptions = Array.from({ length: count }, (_, index) => ({
    ...fixtureSubscription(),
    subscription_id: `subscription-${index}`,
    label:
      ["摄影收藏夹", "城市纪录片", "旅行系列", "技术 UP 投稿"][index % 4] +
      ` ${index + 1}`,
    source: {
      kind: ["favorite", "collection", "series", "uploader"][index % 4] as
        "favorite" | "collection" | "series" | "uploader",
      id: `900${index}`,
    },
    state: ["active", "paused", "auth_required", "rule_error"][index % 4] as
      "active" | "paused" | "auth_required" | "rule_error",
    baseline_ready: index !== 0 && index !== 11,
    last_scan_at: index === 0 ? null : 1791475200,
    next_scan_at: index === 0 ? 0 : 1791477000,
    code:
      index % 4 === 2
        ? "auth_required"
        : index % 4 === 3
          ? "regex_timeout"
          : "scan_complete",
    counts: { members: index === 0 ? 0 : 3, queued: index === 0 ? 0 : 1 },
  }));
  if (count === 12) view.subscriptions[11].label = "山海特辑归档";
  return view;
}
const cards = (page: Page) => page.locator(".media-subscriptions > article");

test("subscription routes have one owner and reject malformed children", () => {
  for (const [index] of sections.entries()) {
    expect(resolveRoute(`#/subscriptions/${index}`)?.module.id).toBe(
      "subscriptions",
    );
    expect(resolveRoute(`#/subscriptions/${index}`)?.section).toBe(index);
  }
  expect(resolveRoute("#/subscriptions")?.section).toBe(0);
  expect(resolveRoute("#/resources/2")).toMatchObject({
    module: { id: "subscriptions" },
    section: 0,
    redirectHash: "#/subscriptions",
  });
  for (const hash of [
    "#/subscriptions/4",
    "#/subscriptions/-1",
    "#/subscriptions/1/extra",
    "#/resources/3",
    "#/resources/2/extra",
  ])
    expect(resolveRoute(hash)).toBeNull();
});

test("subscription is a main navigation entry with one child navigation and legacy replacement", async ({
  page,
}, info) => {
  await installMediaFixture(page, subscriptions(4));
  await page.goto("/#/subscriptions");
  await expect(
    page.getByRole("region", { name: "订阅管理", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("订阅");
  await expect(navigation(page).getByRole("link")).toHaveCount(4);
  await expect(
    navigation(page).getByRole("link", { name: "订阅管理", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await expect(page.getByRole("tablist")).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath(`subscriptions-${info.project.name}.png`),
    fullPage: true,
  });
  const menu = page.getByRole("button", { name: "打开导航" });
  if (await menu.isVisible()) await menu.click();
  const workspace = page
    .getByRole("navigation", { name: "工作区导航" })
    .filter({ visible: true });
  const names = await workspace.getByRole("link").allTextContents();
  expect(names[names.indexOf("用户") + 1]).toBe("订阅");
  await expect(
    workspace.getByRole("link", { name: "订阅", exact: true }),
  ).toHaveAttribute("aria-current", "page");
  await workspace
    .getByRole("link", { name: "资料与资源", exact: true })
    .click();
  await expect(
    page.getByRole("navigation", { name: "资料与资源页面" }).getByRole("link"),
  ).toHaveText(["研究资料", "资产库"]);
  await page.evaluate(() => {
    location.hash = "#/resources/2";
  });
  await expect(page).toHaveURL(/#\/subscriptions$/);
  await expect(
    page.getByRole("region", { name: "订阅管理", exact: true }),
  ).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/#\/resources$/);
  await page.goForward();
  await expect(page).toHaveURL(/#\/subscriptions$/);
});

test("subscription child routes survive direct entry reload and browser return", async ({
  page,
}) => {
  await installMediaFixture(page);
  for (const [index, label] of sections.entries()) {
    await page.goto(`/#/subscriptions/${index}`);
    await expect(
      navigation(page).getByRole("link", { name: label, exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await page.reload();
    await expect(
      navigation(page).getByRole("link", { name: label, exact: true }),
    ).toHaveAttribute("aria-current", "page");
    await expect(page).toHaveTitle(`${label} · 天枢`);
  }
  await navigation(page)
    .getByRole("link", { name: "下载任务", exact: true })
    .click();
  await expect(page.getByLabel("筛选任务")).toBeVisible();
  await navigation(page)
    .getByRole("link", { name: "链接下载", exact: true })
    .click();
  await expect(page.getByLabel("B 站视频链接")).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(/#\/subscriptions\/2$/);
  await expect(page.getByLabel("筛选任务")).toBeVisible();
});

test("subscription search reaches every loaded row and summaries retain their read scope", async ({
  page,
}) => {
  const state = await installMediaFixture(page, subscriptions());
  await page.goto("/#/subscriptions");
  await expect(cards(page)).toHaveCount(10);
  const stats = page.getByLabel("已读取的订阅状态");
  await expect(stats).toContainText("12 个订阅");
  await expect(
    stats.locator("div").filter({ hasText: "需要处理" }).locator("dd"),
  ).toHaveText("6");
  await page.getByLabel("查找订阅").fill("山海");
  await expect(cards(page)).toHaveCount(1);
  await expect(cards(page)).toContainText("山海特辑归档");
  await expect(stats).toContainText("12 个订阅");
  await page
    .getByRole("combobox", { name: /^订阅状态/ })
    .selectOption("active");
  await expect(page.getByText("没有匹配的订阅", { exact: true })).toBeVisible();
  await expect(page.getByText("还没有订阅", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "查看全部订阅", exact: true }).click();
  await expect(cards(page)).toHaveCount(10);
  await page.getByRole("button", { name: "显示更多订阅", exact: true }).click();
  await expect(cards(page)).toHaveCount(12);
  for (const text of ["90011", "隔离 B 站账号", "隔离网络视频库", "系列"]) {
    await page.getByLabel("查找订阅").fill(text);
    await expect(page.getByText(/找到 \d+ 个订阅/)).toBeVisible();
    expect(await cards(page).count()).toBeGreaterThan(0);
  }
  await page.getByRole("button", { name: "清除查找与筛选" }).click();
  for (const [filter, count] of [
    ["active", 3],
    ["paused", 3],
    ["attention", 6],
    ["auth_required", 3],
    ["rule_error", 3],
    ["uninitialized", 2],
  ] as const) {
    await page
      .getByRole("combobox", { name: /^订阅状态/ })
      .selectOption(filter);
    await expect(cards(page)).toHaveCount(count);
  }
  await page
    .getByRole("combobox", { name: /^订阅状态/ })
    .selectOption("active");
  state.view.subscriptions[0].state = "paused";
  await page.getByRole("button", { name: "读取状态", exact: true }).click();
  await expect(cards(page)).toHaveCount(2);
  await expect(
    stats.locator("div").filter({ hasText: "运行中" }).locator("dd"),
  ).toHaveText("2");
  await expect(page.getByRole("combobox", { name: /^订阅状态/ })).toHaveValue(
    "active",
  );
});

test("subscription cards explain missing scans and recovery while empty remains empty", async ({
  page,
}) => {
  const state = await installMediaFixture(page, subscriptions(4));
  await page.goto("/#/subscriptions");
  await expect(cards(page).nth(0)).toContainText("尚未成功扫描");
  await expect(cards(page).nth(0)).toContainText("等待后台安排扫描");
  await expect(cards(page).nth(1)).toContainText("恢复订阅后安排");
  await expect(cards(page).nth(2)).toContainText("重新登录后继续");
  await expect(cards(page).nth(3)).toContainText("修正规则后继续");
  await expect(cards(page).nth(3)).toContainText("正则执行超时，请修正规则");
  await page.getByRole("link", { name: "更新 B 站登录" }).click();
  await expect(page).toHaveURL(/#\/subscriptions\/3$/);
  await expect(
    page.getByRole("button", { name: "生成登录二维码", exact: true }),
  ).toBeVisible();
  state.view.subscriptions = [];
  await navigation(page)
    .getByRole("link", { name: "订阅管理", exact: true })
    .click();
  await expect(page.getByText("还没有订阅", { exact: true })).toBeVisible();
  await expect(page.getByLabel("查找订阅")).toHaveCount(0);
  await expect(page.getByLabel("已读取的订阅状态")).toContainText("0 个订阅");
});

test("legacy anonymous bookmark returns through login to canonical subscriptions", async ({
  page,
}) => {
  const state = await installMediaFixture(page);
  state.authenticated = false;
  await page.route("**/api/web/login", async (route) => {
    state.authenticated = true;
    await route.fulfill({ json: { authenticated: true } });
  });
  await page.goto("/#/resources/2");
  await expect(
    page.getByRole("heading", { name: "登录天枢", exact: true }),
  ).toBeVisible();
  expect(state.calls).toHaveLength(0);
  await page.getByLabel("管理员账号").fill("synthetic-navigation-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-navigation-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page).toHaveURL(/#\/subscriptions$/);
  await expect(
    page.getByRole("region", { name: "订阅管理", exact: true }),
  ).toBeVisible();
});

test("task centre media responsibility link opens download tasks directly", async ({
  page,
}) => {
  await installMediaFixture(page);
  const source: TaskSource = {
    id: "resources.download",
    kind: "media.download",
    connected: true,
    state: "available",
    code: "",
    records: 1,
    cancel_code: null,
  };
  const task: TaskDetail = {
    task_id: "media-task-navigation",
    source: source.id,
    kind: "media.download",
    title: "导航联验视频",
    target: "隔离网络视频库",
    status: "in_progress",
    stage: "downloading",
    code: "",
    created_at: "2026-10-10T08:00:00Z",
    updated_at: "2026-10-10T08:00:00Z",
    settled: false,
    pending: true,
    attention: false,
    source_state: "available",
    cancel: { supported: true, code: "" },
    evidence: {},
    module: { page: "#/resources/2" },
    request: {},
    timeline: {},
  };
  await page.route("**/api/web/tasks/**", async (route) => {
    const detail = route.request().url().endsWith("/detail");
    await route.fulfill({
      json: detail
        ? { generated_at: task.updated_at, sources: [source], task }
        : {
            generated_at: task.updated_at,
            filters: { status: null, source: null, page_size: 10 },
            statuses: ["in_progress"],
            sources: [source],
            items: [task],
            page: {
              size: 10,
              returned: 1,
              has_more: false,
              sort: "created_at_desc",
              next_cursor: null,
            },
          },
    });
  });
  await page.goto("/#/settings/0");
  await page.getByRole("button", { name: "查看详情", exact: true }).click();
  const link = page.getByRole("link", {
    name: "到责任模块页面查看",
    exact: true,
  });
  await expect(link).toHaveAttribute("href", "#/subscriptions/2");
  await link.click();
  await expect(page).toHaveURL(/#\/subscriptions\/2$/);
  await expect(page.getByLabel("筛选任务")).toBeVisible();
  await page.reload();
  await expect(
    navigation(page).getByRole("link", { name: "下载任务", exact: true }),
  ).toHaveAttribute("aria-current", "page");
});
