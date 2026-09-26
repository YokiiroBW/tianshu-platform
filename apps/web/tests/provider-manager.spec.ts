import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("synthetic component flow keeps save, enumeration, test and default distinct", async ({
  page,
}, testInfo) => {
  await page.goto("/tests/provider-manager.fixture.html");
  await expect(
    page.getByRole("heading", { name: "还没有供应商" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "添加供应商" }).click();
  await page.getByLabel("服务预设").selectOption("deepseek");
  await expect(page.getByLabel("API 基础地址")).toHaveValue(
    "https://api.deepseek.com",
  );
  await page.getByLabel("API Key").fill("synthetic-key-never-persisted");
  await page.getByRole("button", { name: "保存供应商" }).click();
  await expect(page.getByRole("heading", { name: "DeepSeek" })).toBeVisible();
  expect(await page.evaluate(() => window.providerEvents)).toEqual(["save"]);
  await expect(page.getByRole("button", { name: "设为默认" })).toBeDisabled();
  await page.getByRole("button", { name: "获取模型" }).click();
  await expect(
    page.getByText("已读取模型列表；这不代表模型能生成回复。"),
  ).toBeVisible();
  await page.getByRole("button", { name: "synthetic-small" }).click();
  await expect(page.getByLabel("模型 ID（可稍后选择）")).toHaveValue(
    "synthetic-small",
  );
  await expect(page.getByLabel("API Key")).toHaveValue("");
  await page.getByRole("button", { name: "保存供应商" }).click();
  await expect(page.getByText("尚未测试")).toBeVisible();
  await page.getByRole("button", { name: "测试回复" }).click();
  await expect(
    page.locator(".provider-card dd").filter({ hasText: /^测试通过/ }),
  ).toBeVisible();
  await page.getByRole("button", { name: "设为默认" }).click();
  await expect(page.getByText("默认对话模型", { exact: true })).toBeVisible();
  expect(await page.evaluate(() => window.providerEvents)).toEqual([
    "save",
    "models:1",
    "save",
    "test:2",
    "default:2",
  ]);
  await expect(page.getByRole("button", { name: "删除" })).toBeDisabled();
  await page.screenshot({
    path: testInfo.outputPath("provider-manager-synthetic.png"),
    fullPage: true,
  });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.getByRole("button", { name: "编辑" }).click();
  await expect(
    page.getByText(/编辑当前默认供应商会使旧测试失效/),
  ).toBeVisible();
  await expect(page.getByLabel("API Key")).toHaveValue("");
  await page.getByRole("button", { name: "保存供应商" }).click();
  await expect(page.getByText("尚未测试")).toBeVisible();
  await expect(page.getByRole("button", { name: "设为默认" })).toBeDisabled();
  expect(await page.evaluate(() => window.providerEvents)).toEqual([
    "save",
    "models:1",
    "save",
    "test:2",
    "default:2",
    "save",
  ]);
});

test("synthetic component rejects upstream HTTP while allowing the LAN HTTP page", async ({
  page,
}) => {
  await page.goto("/tests/provider-manager.fixture.html");
  await expect(page.getByText(/当前使用局域网 HTTP/)).toBeVisible();
  await page.getByRole("button", { name: "添加供应商" }).click();
  await page.getByLabel("名称").fill("本地服务");
  await page.getByLabel("API 基础地址").fill("http://127.0.0.1:8000/v1");
  await page.getByRole("button", { name: "保存供应商" }).click();
  await expect(page.getByText(/当前模型供应商接入要求 HTTPS/)).toBeVisible();
  expect(await page.evaluate(() => window.providerEvents)).toEqual([]);
});
