/**
 * TS-018 任务中心：真实浏览器会话，真实同源后台，合成 HA。
 *
 * 后台是 `tests/backend/run_web_fixture.py`（网页 4814，合成 HA 4817），它登记的正是本平台
 * 已持久化的两个操作来源：模型配置发布（`platform.models`）与家庭设备控制（`platform.home`）。
 * 这里断言的是网页真的把这些记录投影出来，而不是把未发生的操作显示成成功。
 */
import {
  test,
  expect,
  type APIRequestContext,
  type Page,
} from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const ADMIN = "synthetic-admin";
const ADMIN_PASSWORD = "synthetic-local-password-014";
const HOME = "http://127.0.0.1:4817";

type HomeLog = {
  mode: string;
  service_mode: string | null;
  requests: { method: string; path: string }[];
  states: Record<string, { state: string }>;
};

/** The synthetic HA is one long-lived process: every case starts from a known state. */
async function resetHome(request: APIRequestContext) {
  await request.post(`${HOME}/fixture/mode`, {
    data: { mode: "normal", service_mode: null },
  });
  for (const [entity_id, state] of [
    ["light.study", "off"],
    ["switch.kettle", "off"],
    ["sensor.living_temperature", "23.5"],
  ]) {
    await request.post(`${HOME}/fixture/state`, { data: { entity_id, state } });
  }
  await request.post(`${HOME}/fixture/log/clear`, { data: {} });
}

async function homeLog(request: APIRequestContext): Promise<HomeLog> {
  return (await (await request.get(`${HOME}/fixture/log`)).json()) as HomeLog;
}

async function homeServices(request: APIRequestContext) {
  const log = await homeLog(request);
  return log.requests.filter(
    (row) => row.method === "POST" && row.path.startsWith("/api/services/"),
  );
}

/** 只做真实登录：任务中心不需要解锁任何写权限，因此登录本身就是全部授权。 */
async function login(page: Page) {
  await page.goto("/#/settings/0");
  await page.getByLabel("管理员账号").fill(ADMIN);
  await page.getByLabel("密码", { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("button", { name: "重新读取" })).toBeVisible();
}

function panel(page: Page) {
  return page.locator('section.panel.tasks[aria-label="任务中心"]');
}

function sourceCard(page: Page, label: string) {
  return page.locator("article.tasks-source").filter({ hasText: label });
}

function item(page: Page, title: string) {
  return page.locator("article.tasks-item").filter({ hasText: title });
}

/** 两个下拉在同一个筛选区里：0 是状态，1 是来源。 */
function filter(page: Page, name: "状态" | "来源") {
  return page.locator(".tasks-filters select").nth(name === "状态" ? 0 : 1);
}

/** 真的发布一个版本：任务中心只能投影已经落库的权威版本。 */
async function publishVersion(page: Page) {
  await page.goto("/#/settings/2");
  await page.getByLabel("管理员密码").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "解锁模型管理" }).click();
  await expect(page.getByText("管理已解锁", { exact: true })).toBeVisible();
  await page.getByLabel("配置模板").selectOption("chat-local-text");
  await page.getByRole("button", { name: "预览" }).click();
  await expect(page.locator(".models-preview")).toBeVisible();
  await page.getByRole("button", { name: /^发布版本 \d+$/ }).click();
  await expect(page.locator(".models-notice")).toContainText("已发布版本");
}

/** 真的执行一次设备控制：回执只是受理，任务中心要如实分开显示。 */
async function controlLight(page: Page) {
  await page.goto("/#/home");
  await page.getByLabel("管理员密码").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "解锁设备控制" }).click();
  await page.getByRole("button", { name: "打开书房灯" }).click();
  await expect(page.locator(".home-notice")).toContainText("已受理");
}

test("任务中心：真实空状态、未接入来源、键盘、移动端与减少动效", async ({
  page,
}, testInfo) => {
  await login(page);
  // 全新后台确实没有任何操作：这里必须是真空状态，而不是占位记录。
  await expect(page.getByText("还没有任何平台操作记录")).toBeVisible();
  await expect(page.locator("article.tasks-item")).toHaveCount(0);
  // 两个已接入来源各自报告自己的台账状态，未接入的产品明确写出来由。
  await expect(sourceCard(page, "家庭设备控制")).toContainText("已记录");
  await expect(sourceCard(page, "模型配置发布")).toContainText("暂无记录");
  await expect(
    page.locator("article.tasks-source").filter({ hasText: "Core 对话与写作" }),
  ).toContainText("未接入");
  const sources = page.getByLabel("操作来源");
  await expect(sources).toContainText("还没有正式的任务合同");
  await expect(sources).toContainText("没有可投影的操作台账");
  // 空状态下的筛选是可用的，并且如实说明筛不掉任何已记录的操作。
  await filter(page, "来源").selectOption("companion.core");
  await expect(page.getByText("这个来源还没有接入")).toBeVisible();
  await expect(
    page.locator(".state-copy p").filter({ hasText: "任务合同" }),
  ).toHaveCount(1);
  // 已接入但没有记录的来源 + 一个没有记录的状态：这是筛选空了，不是平台没有操作。
  await filter(page, "来源").selectOption("platform.models");
  await filter(page, "状态").selectOption("observed");
  await expect(page.getByText("当前筛选没有记录")).toBeVisible();
  await page.keyboard.press("Tab");
  await expect(filter(page, "状态")).toBeVisible();
  await page.getByRole("button", { name: "清除筛选" }).click();
  await expect(page.getByText("还没有任何平台操作记录")).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("task-center-empty.png"),
    fullPage: true,
  });
  // 减少动效下所有内容仍然可达可读。
  await page.emulateMedia({ reducedMotion: "reduce" });
  await expect(panel(page)).toBeVisible();
  await expect(page.getByLabel("操作来源")).toBeVisible();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  // 移动端不横向溢出，筛选和来源块仍然可用。
  await page.setViewportSize({ width: 390, height: 844 });
  await filter(page, "状态").selectOption("");
  await expect(page.getByLabel("操作来源")).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("task-center-mobile.png"),
    fullPage: true,
  });
});

test("任务中心：真实发布与真实控制被如实投影，详情可键盘开合", async ({
  page,
  request,
}, testInfo) => {
  await resetHome(request);
  await login(page);
  await publishVersion(page);
  await controlLight(page);
  await page.goto("/#/settings/0");
  await expect(page.getByRole("button", { name: "重新读取" })).toBeVisible();
  await expect(sourceCard(page, "模型配置发布")).toContainText("已记录");
  // 两个来源各一条真实记录，状态、阶段与依据都不合并成同一个绿点。
  const published = item(page, "Chat 兼容配置");
  await expect(published).toHaveCount(1);
  await expect(published.locator(".rail-label")).toHaveText("已观测");
  await expect(published).toContainText("权威版本已核对");
  await expect(published).toContainText("有后续观测或权威记录作为依据。");
  await expect(published).toContainText("模型发布");
  const controlled = item(page, "打开书房灯");
  await expect(controlled).toHaveCount(1);
  await expect(controlled.locator(".rail-label")).toHaveText("已受理");
  await expect(controlled).toContainText(
    "只表示平台已被受理，执行结果还没有依据。",
  );
  await expect(controlled).toContainText("不可取消");
  // 设备控制真的发生过一次，而不是网页上的一个假状态。
  expect(await homeServices(request)).toHaveLength(1);
  // 键盘可达：Tab 到“查看详情”并按 Enter 展开。
  const toggle = page.locator("[data-task-toggle]").first();
  await toggle.focus();
  await expect(toggle).toBeFocused();
  await toggle.press("Enter");
  await expect(toggle).toHaveAttribute("aria-expanded", "true");
  const detail = page.locator(".tasks-detail");
  await expect(detail).toContainText("详情是这一刻的重新读取");
  // 执行方没有声明取消能力：这里只说明，不提供取消按钮，也不自动重发。
  await expect(detail).toContainText("取消：不可用");
  await expect(
    page.getByRole("button", { name: /取消(操作|任务)/ }),
  ).toHaveCount(0);
  await expect(detail).toContainText("时间线");
  await expect(detail).toContainText("结果依据");
  await expect(detail).toContainText("请求内容");
  await expect(detail).toContainText("原始责任模块");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("task-center-records.png"),
    fullPage: true,
  });
  // Esc 收起并把焦点还给触发它的按钮。
  await page.keyboard.press("Escape");
  await expect(toggle).toHaveAttribute("aria-expanded", "false");
  await expect(toggle).toBeFocused();
  // 状态筛选是精确的：只筛“已受理”时模型发布记录不出现。
  await filter(page, "状态").selectOption("accepted");
  await expect(item(page, "打开书房灯")).toHaveCount(1);
  await expect(item(page, "Chat 兼容配置")).toHaveCount(0);
  // 来源筛选同理。
  await filter(page, "状态").selectOption("");
  await filter(page, "来源").selectOption("platform.models");
  await expect(item(page, "Chat 兼容配置")).toHaveCount(1);
  await expect(item(page, "打开书房灯")).toHaveCount(0);
  // 深色外观沿用既有令牌，不改动样式基线。
  await page.getByRole("button", { name: "外观设置" }).click();
  await page.getByLabel("深色", { exact: true }).check();
  await page.keyboard.press("Escape");
  await page.screenshot({
    path: testInfo.outputPath("task-center-records-dark.png"),
    fullPage: true,
  });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("任务中心：退出登录后不再显示任何操作记录", async ({ page }, testInfo) => {
  await login(page);
  await expect(page.getByLabel("操作来源")).toBeVisible();
  await page.goto("/#/companion");
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByLabel("管理员账号")).toBeVisible();
  await page.goto("/#/settings/0");
  await expect(page.getByLabel("管理员账号")).toBeVisible();
  await expect(page.getByLabel("操作来源")).toHaveCount(0);
  await expect(page.locator("article.tasks-item")).toHaveCount(0);
  await page.reload();
  await expect(page.getByLabel("管理员账号")).toBeEnabled();
  await expect(page.getByLabel("操作来源")).toHaveCount(0);
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("task-center-logged-out.png"),
    fullPage: true,
  });
});
