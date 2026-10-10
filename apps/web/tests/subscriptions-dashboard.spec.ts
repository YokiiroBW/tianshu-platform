import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {
  fixtureJob,
  fixtureSubscription,
  fixtureView,
  installMediaFixture,
  mediaNavigation,
} from "./media.fixtures";

function dashboard() {
  const view = fixtureView();
  view.overview!.jobs = {
    total: 1200,
    published: 930,
    processing: 12,
    queued: 230,
    attention: 18,
    cancelled: 10,
  };
  view.subscriptions = ["active", "paused", "rule_error", "auth_required"].map(
    (state, index) => ({
      ...fixtureSubscription(),
      subscription_id: `dashboard-sub-${index}`,
      label: [
        "城市与山海 · 收藏夹",
        "旅行纪录片 · 合集",
        "技术分享 · UP 主",
        "音乐现场 · 系列",
      ][index],
      state: state as "active" | "paused" | "rule_error" | "auth_required",
      source: {
        kind: ["favorite", "collection", "uploader", "series"][index] as
          "favorite" | "collection" | "uploader" | "series",
        id: `123456:${index + 1}`,
      },
      baseline_ready: index !== 3,
      counts: { members: 12 + index, queued: 3 + index },
    }),
  );
  view.jobs = [
    "downloading",
    "completed",
    "failed",
    "queued",
    "library_verifying",
    "published",
  ].map((state, index) =>
    fixtureJob({
      job_id: `dashboard-job-${index}`,
      title: [
        "城市与山海 · 第一季",
        "一场音乐现场",
        "旅行纪录片 · 海岛",
        "技术分享 · 新一期",
        "城市延时摄影",
        "更早的发布任务",
      ][index],
      creator: "合成视频作者",
      state,
      stage: state,
      progress: state === "downloading" ? 0.5 : null,
      code: state === "failed" ? "engine_unavailable" : "",
    }),
  );
  return view;
}
const kpis = (page: import("@playwright/test").Page) =>
  page.getByLabel("全部下载任务统计");

test("overview uses full counters independently of its hundred recent jobs", async ({
  page,
}) => {
  const view = dashboard();
  view.jobs = Array.from({ length: 100 }, (_, index) =>
    fixtureJob({
      job_id: `recent-${index}`,
      title: `近期视频 ${index}`,
      state: "completed",
    }),
  );
  await installMediaFixture(page, view);
  await page.goto("/#/subscriptions");
  await expect(kpis(page).locator(".overview-kpi-value")).toHaveText([
    "930",
    "12",
    "230",
    "18",
  ]);
  await expect(page.locator(".overview-total")).toHaveText(
    "累计 1,200 个任务 · 其中 10 个已取消",
  );
  await expect(page.locator(".overview-recent-row")).toHaveCount(5);
  await expect(
    page.getByText("本次快照中的最近 5 项", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("img", {
      name: "订阅状态：运行中 1 个，已暂停 1 个，需要处理 2 个",
    }),
  ).toBeVisible();
  await expect(page.locator(".overview-subscription-card")).toHaveCount(3);
  for (const link of await kpis(page).getByRole("link").all())
    await expect(link).toHaveAttribute("href", "#/subscriptions/2");
  await kpis(page)
    .getByRole("link", { name: /^需要处理，18 个任务/ })
    .click();
  await expect(page).toHaveURL(/#\/subscriptions\/2$/);
  await expect(page.getByLabel("筛选任务")).toBeVisible();
});

test("overview preserves real zeros and concise empty sections", async ({
  page,
}) => {
  const view = fixtureView();
  view.jobs = [];
  view.subscriptions = [];
  view.accounts = [];
  view.targets = [];
  view.overview!.jobs = {
    total: 0,
    published: 0,
    processing: 0,
    queued: 0,
    attention: 0,
    cancelled: 0,
  };
  await installMediaFixture(page, view);
  await page.goto("/#/subscriptions");
  await expect(kpis(page).locator(".overview-kpi-value")).toHaveText([
    "0",
    "0",
    "0",
    "0",
  ]);
  await expect(page.getByText("还没有视频订阅", { exact: true })).toBeVisible();
  await expect(
    page.getByText("近期列表暂无任务", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("尚未登录 B 站，公开视频可直接解析。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("尚未登记发布目标", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".overview-ring-segment")).toHaveCount(0);
  await expect(page.locator(".overview-ring-center strong")).toHaveText("0");
  await expect(
    page.getByRole("link", { name: "添加视频订阅", exact: true }),
  ).toHaveAttribute("href", "#/subscriptions/0");
  await expect(
    page.getByRole("link", { name: "解析视频链接", exact: true }),
  ).toHaveAttribute("href", "#/subscriptions/1");
});

test("older servers without overview never turn a recent snapshot into totals", async ({
  page,
}) => {
  const view = dashboard();
  delete view.overview;
  const state = await installMediaFixture(page, view);
  await page.goto("/#/subscriptions");
  await expect(kpis(page).locator(".overview-kpi-value")).toHaveText([
    "—",
    "—",
    "—",
    "—",
  ]);
  await expect(
    page.getByText("任务总量暂不可用。仍可到下载中心查看已读取的任务。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByText("暂未取得磁盘容量", { exact: true }),
  ).toBeVisible();
  await expect(page.locator(".overview-total")).toHaveCount(0);
  await expect(page.locator(".overview-recent-row")).toHaveCount(5);
  state.view.overview = null;
  await page.getByRole("button", { name: "读取状态", exact: true }).click();
  await expect(kpis(page).locator(".overview-kpi-value")).toHaveText([
    "—",
    "—",
    "—",
    "—",
  ]);
});

test("staging storage can be unknown without fabricating zero capacity", async ({
  page,
}) => {
  const view = dashboard();
  view.overview!.storage = {
    state: "unavailable",
    total_bytes: null,
    used_bytes: null,
    free_bytes: null,
  };
  const state = await installMediaFixture(page, view);
  await page.goto("/#/subscriptions");
  const disk = page.getByRole("region", { name: "下载暂存磁盘", exact: true });
  await expect(
    disk.getByText("暂未取得磁盘容量", { exact: true }),
  ).toBeVisible();
  await expect(disk.getByRole("progressbar")).toHaveCount(0);
  await expect(disk).not.toContainText("0 B");
  state.view.overview!.storage = {
    state: "available",
    total_bytes: 1024,
    used_bytes: null,
    free_bytes: 1024,
  };
  await page.getByRole("button", { name: "读取状态", exact: true }).click();
  await expect(
    disk.getByText("暂未取得磁盘容量", { exact: true }),
  ).toBeVisible();
  state.view.overview!.storage = {
    state: "available",
    total_bytes: 1024 ** 4,
    used_bytes: 1024 ** 3 * 640,
    free_bytes: 1024 ** 3 * 384,
  };
  await page.getByRole("button", { name: "读取状态", exact: true }).click();
  await expect(disk.getByText("384 GiB", { exact: true })).toBeVisible();
  await expect(disk.getByRole("progressbar")).toHaveAttribute(
    "max",
    String(1024 ** 4),
  );
  await expect(disk).toContainText(
    "平台下载暂存所在磁盘，容量包含同盘其他文件。",
  );
});

test("overview read failures show an error then retain an explicitly stale snapshot", async ({
  page,
}) => {
  const state = await installMediaFixture(page, dashboard());
  state.unavailable = true;
  await page.goto("/#/subscriptions");
  await expect(
    page.getByText("暂时无法读取视频订阅", { exact: true }),
  ).toBeVisible();
  await expect(kpis(page)).toHaveCount(0);
  state.unavailable = false;
  await page.getByRole("button", { name: "读取状态", exact: true }).click();
  await expect(kpis(page).locator(".overview-kpi-value").first()).toHaveText(
    "930",
  );
  state.unavailable = true;
  await page.getByRole("button", { name: "读取状态", exact: true }).click();
  await expect(
    page.getByText("下面仍是上次成功读取的快照。连接恢复后会自动更新。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(kpis(page).locator(".overview-kpi-value").first()).toHaveText(
    "930",
  );
  state.unavailable = false;
  state.view.engine = {
    state: "unavailable",
    version: null,
    code: "engine_unavailable",
  };
  await page.getByRole("button", { name: "读取状态", exact: true }).click();
  await expect(
    page.getByText("下面仍是上次成功读取的快照。连接恢复后会自动更新。", {
      exact: true,
    }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "引擎、账号与发布目标" }),
  ).toContainText("下载引擎暂不可用。");
});

test("overview quick links reuse the existing media panels", async ({
  page,
}) => {
  await installMediaFixture(page, dashboard());
  for (const [name, index, heading] of [
    ["视频订阅", 0, "订阅你关注的来源"],
    ["链接下载", 1, "从视频链接开始"],
    ["下载中心", 2, "下载与入库进度"],
    ["账号与媒体库", 3, "扫码登录"],
  ] as const) {
    await page.goto("/#/subscriptions");
    await page
      .getByRole("region", { name: "订阅快捷入口" })
      .getByRole("link", { name: new RegExp(`^${name}`) })
      .click();
    await expect(page).toHaveURL(new RegExp(`#/subscriptions/${index}$`));
    await expect(
      page.locator(".page-heading, main > .section-nav"),
    ).toHaveCount(0);
    await expect(
      page.getByRole("heading", { name: heading, exact: true }),
    ).toBeVisible();
  }
});

test("overview stays compact when media is unconfigured", async ({
  page,
}, info) => {
  const view = fixtureView();
  view.configured = false;
  view.overview = null;
  view.accounts = [];
  view.jobs = [];
  view.subscriptions = [];
  view.targets = [];
  await installMediaFixture(page, view);
  await page.goto("/#/subscriptions");
  await expect(
    page.getByText("视频订阅服务尚未配置", { exact: true }),
  ).toBeVisible();
  const bounds = await page
    .locator(".media-overview-page > .state-panel")
    .boundingBox();
  expect(bounds!.height).toBeLessThan(260);
  await expect(kpis(page)).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath(`overview-unconfigured-${info.project.name}.png`),
    fullPage: true,
  });
});

test("overview matches the shared light and dark design without a duplicate page banner", async ({
  page,
}, info) => {
  await installMediaFixture(page, dashboard());
  for (const theme of ["light", "dark"] as const) {
    await page.emulateMedia({ colorScheme: theme, reducedMotion: "reduce" });
    await page.goto("/#/subscriptions");
    await expect(
      page.getByRole("region", { name: "订阅概览", exact: true }),
    ).toBeVisible();
    await expect(page.locator("html")).toHaveAttribute("data-theme", theme);
    await expect(
      page.locator(".page-heading, main > .section-nav"),
    ).toHaveCount(0);
    await expect(page.getByRole("heading", { level: 1 })).toHaveClass(
      "sr-only",
    );
    await expect(
      page.getByText("关注视频来源，管理下载与媒体入库", { exact: true }),
    ).toHaveCount(0);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const ring = await page.locator(".overview-ring svg").boundingBox();
    expect(ring!.height).toBeGreaterThan(150);
    expect(Math.abs(ring!.width - ring!.height)).toBeLessThan(1);
    expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
    await page.screenshot({
      path: info.outputPath(`overview-${theme}-${info.project.name}.png`),
      fullPage: true,
    });
  }
  await mediaNavigation(page);
  if (info.project.name === "mobile")
    await page.screenshot({
      path: info.outputPath("overview-mobile-navigation.png"),
      fullPage: true,
    });
});
