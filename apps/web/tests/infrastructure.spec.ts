import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { randomUUID } from "node:crypto";

async function login(page: Page) {
  await page.goto("/#/settings/11");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByLabel("已配置的 SSH 节点")).toBeVisible();
}

async function register(page: Page, id: string, label: string) {
  await page.goto("/#/settings/11");
  await page.getByLabel("已配置的 SSH 节点").selectOption(id);
  await page.getByLabel("显示名称").fill(label);
  await page.getByLabel("启用后台采集").check();
  await page.getByRole("button", { name: "保存节点", exact: true }).click();
  await expect(
    page.getByRole("status").filter({ hasText: "节点已保存" }),
  ).toBeVisible();
}

test("real platform HTTP: node registration, 130 containers, separate health, details, logs and OpenWrt", async ({
  page,
}, testInfo) => {
  test.setTimeout(60000);
  await login(page);
  await expect(page.locator('option[value="fixture-nas"]')).toContainText(
    "容器清单",
  );
  await register(page, "fixture-nas", "合成 NAS");
  await page.getByRole("button", { name: "测试连接", exact: true }).click();
  await expect(
    page.getByText("已安排连接测试", { exact: false }),
  ).toBeVisible();
  await page.goto("/#/home/1");
  await page.getByRole("button", { name: /^合成 NAS/ }).click();
  const list = page.getByRole("region", { name: "容器列表", exact: true });
  await expect(
    list.getByRole("heading", { name: "容器清单 130 个" }),
  ).toBeVisible();
  await expect(list.locator(".infra-container-row")).toHaveCount(100);
  await expect(
    list
      .locator(".infra-container-row .infra-health")
      .filter({ hasText: "未配置检查" })
      .first(),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "查看容器 synthetic-app-003", exact: true })
    .click();
  const detail = page.getByRole("complementary", {
    name: "容器详情",
    exact: true,
  });
  await expect(
    detail.getByRole("heading", { name: "synthetic-app-003", exact: true }),
  ).toBeVisible();
  await expect(detail.locator(".status-rail.tone-red")).toContainText("运行中");
  await expect(
    detail.getByText("健康检查 · 检查异常", { exact: true }),
  ).toBeVisible();
  await expect(
    detail.getByText("挂载与持久数据", { exact: true }),
  ).toBeVisible();
  await expect(detail).toContainText("/srv/synthetic-stacks/synthetic/data");
  await page.getByRole("button", { name: "读取最近日志", exact: true }).click();
  await expect(page.getByLabel("容器日志预览")).toContainText("合成容器日志");
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(list.locator(".infra-container-row")).toHaveCount(30);
  const last = page.getByRole("button", {
    name: "查看容器 synthetic-app-130",
    exact: true,
  });
  await last.focus();
  await page.keyboard.press("Enter");
  await expect(
    detail.getByRole("heading", { name: "synthetic-app-130", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: /utilities.*65 个容器/ }).click();
  await expect(
    list.getByRole("heading", { name: "容器清单 65 个" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "全部容器", exact: true }).click();
  await page.getByLabel("搜索容器", { exact: true }).fill("synthetic-app-003");
  await expect(list.locator(".infra-container-row")).toHaveCount(1);
  await page
    .getByRole("combobox", { name: "健康检查", exact: true })
    .selectOption("unhealthy");
  await expect(
    list.getByRole("heading", { name: "容器清单 1 个" }),
  ).toBeVisible();
  await expect(page.locator(".infra-workspace")).toBeVisible();
  const violations = await new AxeBuilder({ page })
    .include(".infra-workspace")
    .analyze();
  expect(violations.violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth + 1,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: testInfo.outputPath("docker-light.png"),
    fullPage: true,
  });
  await page.evaluate(() =>
    document.documentElement.setAttribute("data-theme", "dark"),
  );
  await page.screenshot({
    path: testInfo.outputPath("docker-dark.png"),
    fullPage: true,
  });
  await register(page, "fixture-router", "合成路由器");
  await page.goto("/#/home/2");
  await page.getByRole("button", { name: /^合成路由器/ }).click();
  await expect(
    page.getByRole("heading", { name: "OpenWrt 与网络", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("Synthetic Router", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText("192.0.2.10/24", { exact: true })).toBeVisible();
  await expect(
    page.getByText("逐终端流量尚未接入", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "游戏服", exact: true }),
  ).toBeVisible();
  await page.evaluate(() =>
    document.documentElement.setAttribute("data-theme", "dark"),
  );
  expect(
    (await new AxeBuilder({ page }).include(".infra-workspace").analyze())
      .violations,
  ).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("openwrt-dark.png"),
    fullPage: true,
  });
});

test("switching nodes discards a late real HTTP response and stale projections label old values", async ({
  page,
}) => {
  test.setTimeout(45000);
  await login(page);
  await register(page, "fixture-nas", "合成 NAS");
  await register(page, "fixture-router", "合成路由器");
  await page.route("**/api/web/infrastructure/node", async (route) => {
    const request = route.request().postDataJSON();
    const response = await route.fetch();
    if (request.node_id === "fixture-nas")
      await new Promise((resolve) => setTimeout(resolve, 1200));
    await route.fulfill({ response }).catch(() => {});
  });
  await page.goto("/#/home/2");
  await page.getByRole("button", { name: /^合成路由器/ }).click();
  await expect(
    page.getByRole("heading", { name: "OpenWrt 与网络", exact: true }),
  ).toBeVisible();
  await page.waitForTimeout(1500);
  await expect(
    page.getByRole("heading", { name: "合成 NAS", exact: true }),
  ).toHaveCount(0);
  await page.unroute("**/api/web/infrastructure/node");
  await page.route("**/api/web/infrastructure/node", async (route) => {
    const response = await route.fetch();
    const value = await response.json();
    value.connection_state = "offline";
    value.snapshot.freshness = "stale";
    value.snapshot.reason_codes = ["ssh_unavailable"];
    await route.fulfill({ json: value });
  });
  await page.getByRole("button", { name: /^合成 NAS/ }).click();
  await expect(
    page.getByText("上次读数", { exact: false }).first(),
  ).toBeVisible();
  await expect(
    page.getByText("SSH 连接不可用", { exact: false }),
  ).toBeVisible();
  await expect(page.getByText("12 GiB", { exact: true })).toBeVisible();
});

test("polling keeps a draft bound to its read revision and a concurrent save conflicts", async ({
  page,
}) => {
  await login(page);
  await register(page, "fixture-nas", "合成 NAS");
  await page.getByLabel("显示名称").fill("未保存的名称");
  const session = await (await page.request.get("/api/web/session")).json();
  const headers = {
    Origin: "http://127.0.0.1:4827",
    "X-CSRF-Token": session.csrf,
  };
  const view = await (
    await page.request.post("/api/web/infrastructure/view", {
      data: {},
      headers,
    })
  ).json();
  const response = await page.request.post("/api/web/infrastructure/save", {
    data: {
      node_id: "fixture-nas",
      label: "其他页面的名称",
      enabled: true,
      expected_revision: view.revision,
      client_id: randomUUID(),
    },
    headers,
  });
  expect(response.status()).toBe(200);
  await expect(
    page.getByText("连接设置已在其他页面改变", { exact: false }),
  ).toBeVisible({ timeout: 8000 });
  await page.getByRole("button", { name: "保存节点", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("revision_conflict");
  const saved = await (
    await page.request.post("/api/web/infrastructure/view", {
      data: {},
      headers,
    })
  ).json();
  expect(
    saved.nodes.find((node: { id: string }) => node.id === "fixture-nas").label,
  ).toBe("其他页面的名称");
  await page.getByRole("button", { name: "重新读取设置", exact: true }).click();
  await expect(page.getByLabel("显示名称")).toHaveValue("其他页面的名称");
});
