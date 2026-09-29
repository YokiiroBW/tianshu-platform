import { chromium, expect } from "@playwright/test";
import { randomUUID } from "node:crypto";

const url = process.env.TS_ROLE_WEB_URL;
if (!url?.startsWith("https://127.0.0.1:")) throw new Error("loopback TLS required");
const profiles = JSON.parse(process.env.TS_ROLE_PROFILES);
const providers = JSON.parse(process.env.TS_ROLE_PROVIDERS);
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ ignoreHTTPSErrors: true, locale: "zh-CN",
    viewport: { width: 1440, height: 960 }, reducedMotion: "reduce" });
  const page = await context.newPage();
  await page.goto(url + "/#/companion/3");
  await page.getByLabel("管理员账号").fill("integration-admin");
  await page.getByLabel("密码", { exact: true }).fill(process.env.TS_ROLE_WEB_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "角色管理" })).toBeVisible();
  const created = [];
  for (let i = 0; i < 2; i++) {
    await page.getByRole("button", { name: "新建" }).click();
    const form = page.locator(".role-form");
    await form.getByLabel("名称").fill(`Role ${i ? "B" : "A"}`);
    await form.getByLabel("人格档案").selectOption(profiles[i]);
    await form.getByLabel("模型").selectOption(`${providers[i]}@1`);
    if (i === 1) await form.getByLabel("记住新的经历").uncheck();
    await form.getByLabel("启用对话").check();
    await form.getByRole("button", { name: "应用设置" }).click();
    await expect(page.getByText("角色设置已生效。")).toBeVisible({ timeout: 20000 });
    const response = await context.request.get(url + "/api/web/session");
    const session = await response.json();
    const role = session.conversations.flatMap((c) => c.actors).find((a) =>
      a.startsWith("actor:role-") && !created.some((x) => x.actor === a));
    expect(role).toBeTruthy();
    const conversation = session.conversations.find((c) => c.actors.includes(role));
    created.push({ actor: role, conversation: conversation.id });
  }
  await page.screenshot({ path: `${process.env.TS_ROLE_OUTPUT}/roles-desktop.png`, fullPage: true });
  const edited = page.locator(".role-form");
  await edited.getByLabel("名称").fill("Role B edited");
  await edited.getByRole("button", { name: "应用设置" }).click();
  await expect(page.getByText("角色设置已生效。")).toBeVisible();
  const mobile = await browser.newContext({ ignoreHTTPSErrors: true, locale: "zh-CN",
    viewport: { width: 390, height: 844 }, storageState: await context.storageState() });
  const mobilePage = await mobile.newPage();
  await mobilePage.goto(url + "/#/companion/3");
  await expect(mobilePage.getByRole("heading", { name: "角色管理" })).toBeVisible();
  await mobilePage.getByRole("button", { name: "Role A 已启用" }).click();
  await mobilePage.screenshot({ path: `${process.env.TS_ROLE_OUTPUT}/roles-mobile.png`, fullPage: true });
  await mobile.close();
  const session = await (await context.request.get(url + "/api/web/session")).json();
  const calls = await Promise.all(created.map(({ actor, conversation }, i) =>
    context.request.post(url + "/api/web/messages", {
      data: { actor, conversation, text: `synthetic parallel ${i}`, client_id: randomUUID() },
      headers: { Origin: url, "X-CSRF-Token": session.csrf },
    })));
  for (const call of calls) expect(call.status()).toBe(200);
  const end = Date.now() + 16000;
  let last = [];
  let replied = false;
  while (Date.now() < end) {
    const checks = await Promise.all(created.map(({ actor, conversation }) =>
      context.request.post(url + "/api/web/snapshot", {
        data: { actor, conversation, before: null },
        headers: { Origin: url, "X-CSRF-Token": session.csrf },
      })));
    const values = await Promise.all(checks.map((r) => r.json()));
    last = values;
    if (values.every((v) => JSON.stringify(v).includes("recorded reply"))) {
      replied = true;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  if (!replied) throw new Error(JSON.stringify({ calls: await Promise.all(calls.map((c) => c.json())), last }).slice(0, 5000));
  const forged = await context.request.post(url + "/api/web/messages", {
    data: { actor: created[1].actor, conversation: created[0].conversation,
      text: "forged source", client_id: randomUUID() },
    headers: { Origin: url, "X-CSRF-Token": session.csrf },
  });
  expect(forged.status()).toBe(403);
  await page.getByRole("button", { name: "Role A 已启用" }).click();
  await page.getByRole("button", { name: "停用角色" }).click();
  await expect(page.getByText("角色已停用，历史记录保留。", { exact: true })).toBeVisible();
  const disabled = await context.request.post(url + "/api/web/messages", {
    data: { actor: created[0].actor, conversation: created[0].conversation,
      text: "after disable", client_id: randomUUID() },
    headers: { Origin: url, "X-CSRF-Token": session.csrf },
  });
  expect([403, 503]).toContain(disabled.status());
  const roleViewResponse = await context.request.post(url + "/api/web/roles/view", {
    data: {}, headers: { Origin: url, "X-CSRF-Token": session.csrf },
  });
  expect(roleViewResponse.status()).toBe(200);
  const roleView = await roleViewResponse.json();
  const existing = roleView.legacy_roles.find((item) => item.id === "actor:a");
  expect(existing).toBeTruthy();
  const existingName = existing.name === existing.id
    ? `原有角色 · ${existing.id.slice(6)}` : existing.name;
  await page.getByRole("button", { name: `${existingName} 待配置` }).click();
  await expect(page.getByText(/保留原角色身份、人格和历史/)).toBeVisible();
  await page.locator(".role-form").getByLabel("模型").selectOption(`${providers[1]}@1`);
  await page.locator(".role-form").getByRole("button", { name: "应用设置" }).click();
  await expect(page.getByText("角色设置已生效。")).toBeVisible({ timeout: 20000 });
  await expect(page.getByText(/原角色人格 · 保留现有版本/)).toBeVisible();
  await page.locator(".role-detail-heading h3").click();
  await page.screenshot({ path: `${process.env.TS_ROLE_OUTPUT}/roles-existing.png`, fullPage: true });
  const adoptedSession = await (await context.request.get(url + "/api/web/session")).json();
  const existingConversation = adoptedSession.conversations.find((item) =>
    item.actors.includes("actor:a"));
  expect(existingConversation).toBeTruthy();
  const existingMessage = await context.request.post(url + "/api/web/messages", {
    data: { actor: "actor:a", conversation: existingConversation.id,
      text: "synthetic adopted role", client_id: randomUUID() },
    headers: { Origin: url, "X-CSRF-Token": adoptedSession.csrf },
  });
  expect(existingMessage.status()).toBe(200);
  let existingReply = false;
  const adoptedEnd = Date.now() + 16000;
  while (Date.now() < adoptedEnd) {
    const response = await context.request.post(url + "/api/web/snapshot", {
      data: { actor: "actor:a", conversation: existingConversation.id, before: null },
      headers: { Origin: url, "X-CSRF-Token": adoptedSession.csrf },
    });
    existingReply = JSON.stringify(await response.json()).includes("recorded reply");
    if (existingReply) break;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  expect(existingReply).toBe(true);
  await page.getByRole("button", { name: "停用角色" }).click();
  await expect(page.getByText("角色已停用，历史记录保留。", { exact: true })).toBeVisible();
  const deniedAdopted = await context.request.post(url + "/api/web/messages", {
    data: { actor: "actor:a", conversation: existingConversation.id,
      text: "after adopted disable", client_id: randomUUID() },
    headers: { Origin: url, "X-CSRF-Token": adoptedSession.csrf },
  });
  expect([403, 503]).toContain(deniedAdopted.status());
  console.log(JSON.stringify({ browser: "Chromium", roles: created,
    screenshots: ["roles-desktop.png", "roles-mobile.png", "roles-existing.png"],
    flow: "create-configure-apply-edit-chat-disable-adopt-existing-chat",
    forged_source: forged.status(), disabled_role: disabled.status(),
    disabled_adopted: deniedAdopted.status() }));
  await context.close();
} finally {
  await browser.close();
}
