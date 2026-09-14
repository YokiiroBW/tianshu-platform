import { chromium, expect } from "@playwright/test";
import { existsSync } from "node:fs";

const url = process.env.TS014_WEB_URL;
if (!url?.startsWith("https://127.0.0.1:"))
  throw new Error("Isolated loopback only");
const phase = process.env.TS014_WEB_PHASE;
const browser = await chromium.launch();
try {
  // Temporary test CA only. Service-to-service clients verify the fixture CA;
  // no OS/browser trust-store installation or non-loopback navigation occurs.
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    locale: "zh-CN",
    timezoneId: "Asia/Shanghai",
    reducedMotion: "reduce",
    viewport: { width: 1440, height: 1000 },
    storageState:
      phase === "reconnect" && existsSync(process.env.TS014_BROWSER_STATE)
        ? process.env.TS014_BROWSER_STATE
        : undefined,
  });
  const page = await context.newPage();
  const leaks = [];
  page.on("response", async (response) => {
    if (response.url().startsWith(url + "/api/web/")) {
      const text = await response.text().catch(() => "");
      if (/assertion_ref|accepted_origin|credential_ref|Bearer /.test(text))
        leaks.push(response.url());
    }
  });
  await page.goto(url + "/#/companion");
  await expect(page.getByLabel("管理员账号")).toBeEnabled();
  await page.getByLabel("管理员账号").fill("integration-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill(process.env.TS014_WEB_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  const session = await (
    await context.request.get(url + "/api/web/session")
  ).json();
  const selection = {
    conversation: "self_private",
    actor: "actor:a",
    before: null,
  };
  const missingCsrf = await context.request.post(url + "/api/web/snapshot", {
    data: selection,
    headers: { Origin: url },
  });
  expect(missingCsrf.status()).toBe(403);
  const otherActor = await context.request.post(url + "/api/web/snapshot", {
    data: { ...selection, actor: "actor:other" },
    headers: { Origin: url, "X-CSRF-Token": session.csrf },
  });
  expect(otherActor.status()).toBe(403);
  const forged = await context.request.post(url + "/api/web/snapshot", {
    data: { ...selection, scope: { person_id: "forged" } },
    headers: { Origin: url, "X-CSRF-Token": session.csrf },
  });
  expect(forged.status()).toBe(400);
  if (phase === "chat") {
    for (const text of ["合成网页第一条", "合成网页续句"]) {
      await page.getByLabel("想说些什么").fill(text);
      await page.getByRole("button", { name: "发送", exact: true }).click();
      await expect(
        page.getByRole("button", { name: "发送", exact: true }),
      ).toBeVisible();
      await expect(
        page.getByText("入站已接收，等待后台状态；尚不代表已回复。"),
      ).toBeVisible();
    }
    await expect(
      page.getByRole("heading", { name: "正在合并续句" }),
    ).toBeVisible({ timeout: 10000 });
    await expect(
      page.getByText("合成网页第一条", { exact: true }),
    ).toBeVisible();
    await expect(page.getByText("合成网页续句", { exact: true })).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "第 1 轮 · 已送达" }),
    ).toBeVisible({ timeout: 30000 });
    await expect(page.getByText(/actor:a recorded reply/)).toBeVisible();
    await page.getByLabel("想说些什么").fill("合成可取消的第二轮");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByRole("button", { name: "取消第 2 轮" })).toBeVisible(
      { timeout: 20000 },
    );
    await page.getByRole("button", { name: "取消第 2 轮" }).click();
    await expect(page.getByText(/取消结果：cancelled/)).toBeVisible({
      timeout: 15000,
    });
    await expect(
      page.getByRole("heading", { name: "第 2 轮 · 已取消" }),
    ).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/actor:a recorded reply/)).toBeVisible();
    await page.getByLabel("想说些什么").fill("合成响应丢失的第三轮");
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(
      page.getByRole("heading", { name: "第 3 轮 · 发送结果未知" }),
    ).toBeVisible({ timeout: 30000 });
    await expect(page.getByText("正文不可展示 · unavailable")).toBeVisible();
    await context.storageState({ path: process.env.TS014_BROWSER_STATE });
  } else {
    await expect(
      page.getByRole("heading", { name: "第 1 轮 · 已送达" }),
    ).toBeVisible({ timeout: 15000 });
    await expect(page.getByText(/actor:a recorded reply/)).toBeVisible();
    await expect(
      page.getByRole("heading", { name: "第 3 轮 · 发送结果未知" }),
    ).toBeVisible({ timeout: 15000 });
    await page.getByRole("button", { name: "退出登录" }).click();
    await expect(page.getByLabel("管理员账号")).toBeEnabled();
  }
  expect(leaks).toEqual([]);
  await page.screenshot({
    path: `${process.env.TS014_BROWSER_OUTPUT}/${phase}.png`,
    fullPage: true,
  });
  console.log(
    JSON.stringify({
      phase,
      login: "real",
      snapshots: "real Core",
      sender: "real Platform persistent",
      external_model: "recorded synthetic",
      bearer_or_origin_leaks: leaks.length,
      lost_sender_receipt: "Core closed_unknown; body hidden; no retry",
    }),
  );
  await context.close();
} finally {
  await browser.close();
}
