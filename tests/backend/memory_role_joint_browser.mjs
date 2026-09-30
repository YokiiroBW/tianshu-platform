// Actual Chromium against the live Platform and Memory processes from test_memory_joint.py.
// No network interception or browser-side service substitutes are used.
import { chromium } from "../../node_modules/@playwright/test/index.mjs";
import { mkdir } from "node:fs/promises";
import { join } from "node:path";

const [origin, screenshots] = process.argv.slice(2);
if (!origin || !screenshots) throw new Error("origin and screenshot directory are required");
await mkdir(screenshots, { recursive: true });
const browser = await chromium.launch({ headless: true });
try {
  for (const [name, options] of [
    ["desktop", { viewport: { width: 1440, height: 1000 } }],
    ["mobile", { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true }],
  ]) {
    const context = await browser.newContext({ ...options, locale: "zh-CN", timezoneId: "Asia/Shanghai" });
    const page = await context.newPage();
    await page.goto(origin + "/#/memory/0");
    await page.getByLabel("管理员账号").fill("synthetic-admin");
    await page.getByLabel("密码", { exact: true }).fill("synthetic-local-password-014");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    const select = page.getByLabel("查看哪位角色的记忆");
    await select.waitFor();
    if (await select.inputValue() !== "actor:a") throw new Error(`${name}: default actor mismatch`);
    await page.locator(".memory-counts dd").waitFor();
    if ((await page.locator(".memory-counts dd").innerText()).trim() !== "2") throw new Error(`${name}: default count mismatch`);
    await select.selectOption("actor:b");
    await page.waitForFunction(() => document.querySelector(".memory-counts dd")?.textContent?.trim() === "1");
    await page.screenshot({ path: join(screenshots, `memory-role-live-${name}.png`), fullPage: true });
    await page.getByRole("link", { name: "查看人物与群" }).click();
    await page.locator(".memory-list button").first().waitFor();
    await page.locator(".memory-list button").first().click();
    await page.getByText("语义组 y-profile-b").waitFor();
    await page.getByRole("link", { name: "本人记忆", exact: true }).click();
    await page.getByText("语义组 " + process.env.TS_MEMORY_ROLE_B_GROUP).waitFor();
    await page.reload();
    const remembered = page.getByLabel("查看哪位角色的记忆");
    await remembered.waitFor();
    if (await remembered.inputValue() !== "actor:b") throw new Error(`${name}: role was not retained`);
    await context.close();
    console.log(`${name}: real Platform/Memory role switch, projections, own records and refresh passed`);
  }
} finally {
  await browser.close();
}
