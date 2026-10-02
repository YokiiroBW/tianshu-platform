import { chromium, expect } from "@playwright/test";

const url = process.env.TS_ROLE_WEB_URL;
if (!url?.startsWith("https://127.0.0.1:")) throw new Error("loopback TLS required");
const [profile] = JSON.parse(process.env.TS_ROLE_PROFILES);
const [provider] = JSON.parse(process.env.TS_ROLE_PROVIDERS);
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true, locale: "zh-CN" });
  const page = await context.newPage();
  await page.goto(url + "/#/companion/3");
  await page.getByLabel("管理员账号").fill("integration-admin");
  await page.getByLabel("密码", { exact: true }).fill(process.env.TS_ROLE_WEB_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "角色管理" })).toBeVisible();
  await page.getByRole("button", { name: "新建" }).click();
  const form = page.locator(".role-form");
  await form.getByLabel("名称").fill("Retry role");
  await form.getByLabel("人格档案").selectOption(profile);
  await form.getByLabel("模型").selectOption(`${provider}@1`);
  await form.getByLabel("启用角色", { exact: true }).check();
  await form.getByRole("button", { name: "应用设置" }).click();
  await expect(page.getByRole("button", { name: "继续配置" })).toBeVisible({ timeout: 20000 });
  await expect(page.getByRole("button", { name: "取消配置并停用" })).toBeVisible();
  await page.getByRole("button", { name: "继续配置" }).click();
  await expect(page.getByText("配置已生效。", { exact: true })).toBeVisible({ timeout: 20000 });
  await form.getByLabel("名称").fill("Retry role edited");
  await form.getByRole("button", { name: "应用设置" }).click();
  await expect(page.getByText("角色设置已生效。", { exact: true })).toBeVisible({ timeout: 20000 });
  const session = await (await context.request.get(url + "/api/web/session")).json();
  const response = await context.request.post(url + "/api/web/roles/view", {
    data: {}, headers: { Origin: url, "X-CSRF-Token": session.csrf },
  });
  expect(response.status()).toBe(200);
  const view = await response.json();
  const role = view.roles.find((item) => item.name === "Retry role edited");
  expect(role?.state).toBe("active");
  expect(role?.version).toBe(2);
  console.log(JSON.stringify({ browser: "Chromium", flow: "pending-retry-edit", actor: role.actor_id }));
  await context.close();
} finally {
  await browser.close();
}
