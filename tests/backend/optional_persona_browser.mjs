import { chromium, expect } from "@playwright/test";
const url = process.env.TS_ROLE_WEB_URL;
if (!url?.startsWith("https://127.0.0.1:")) throw new Error("loopback TLS required");
const profiles = JSON.parse(process.env.TS_ROLE_PROFILES);
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true, locale: "zh-CN" });
  const page = await context.newPage();
  await page.goto(url + "/#/companion/3");
  await page.getByLabel("管理员账号").fill("integration-admin");
  await page.getByLabel("密码", { exact: true }).fill(process.env.TS_ROLE_WEB_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "角色管理" })).toBeVisible();
  await page.getByRole("button", { name: "新建", exact: true }).click();
  const form = page.locator(".role-form");
  await expect(form.getByLabel("人格档案")).toHaveValue("");
  await expect(form.getByLabel("启用对话")).not.toBeChecked();
  await expect(form.getByRole("button", { name: "应用设置" })).toBeDisabled();
  const name = profiles.length ? "Optional with profiles" : "Optional without profiles";
  await form.getByLabel("名称").fill(name);
  await form.getByRole("button", { name: "应用设置" }).click();
  await expect(page.getByText("角色已停用，历史记录保留。", { exact: true })).toBeVisible();
  await page.reload();
  await page.getByRole("button", { name: new RegExp(name) }).click();
  await expect(form.getByLabel("人格档案")).toHaveValue("");
  await expect(form.getByLabel("启用对话")).not.toBeChecked();
  if (profiles.length) {
    await form.getByLabel("人格档案").selectOption(profiles[0]);
    await form.getByRole("button", { name: "应用设置" }).click();
    await expect(page.getByText("角色已停用，历史记录保留。", { exact: true })).toBeVisible();
    await page.reload();
    await page.getByRole("button", { name: new RegExp(name) }).click();
    await expect(form.getByLabel("人格档案")).toHaveValue(profiles[0]);
    await form.getByLabel("人格档案").selectOption("");
    await form.getByRole("button", { name: "应用设置" }).click();
    await expect(page.getByText("角色已停用，历史记录保留。", { exact: true })).toBeVisible();
  }
  console.log(JSON.stringify({ flow: "optional-persona", profiles: profiles.length, disabled: true }));
  await context.close();
} finally { await browser.close(); }
