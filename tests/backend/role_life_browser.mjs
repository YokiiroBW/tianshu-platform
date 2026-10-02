import { chromium, expect } from "@playwright/test";

const url = process.env.TS_ROLE_WEB_URL;
if (!url?.startsWith("https://127.0.0.1:"))
  throw new Error("loopback TLS required");
const providers = JSON.parse(process.env.TS_ROLE_PROVIDERS);
const browser = await chromium.launch();
try {
  const context = await browser.newContext({
    ignoreHTTPSErrors: true,
    locale: "zh-CN",
    viewport: { width: 1440, height: 1000 },
    reducedMotion: "reduce",
  });
  const page = await context.newPage();
  await page.goto(url + "/#/companion/3");
  await page.getByLabel("管理员账号").fill("integration-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill(process.env.TS_ROLE_WEB_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByRole("button", { name: "新建", exact: true }).click();
  const form = page.locator(".role-form");
  await form.getByLabel("名称", { exact: true }).fill("独立生活角色");
  await form.getByLabel("模型").selectOption(`${providers[1]}@1`);
  await form.getByLabel("启用角色", { exact: true }).check();
  await form.getByLabel("允许对话", { exact: true }).uncheck();
  await form.getByLabel("使用本角色记忆").uncheck();
  await form.getByLabel("记住新的经历").uncheck();
  await form.getByRole("button", { name: "应用设置" }).click();
  await expect(page.getByText("角色设置已生效。", { exact: true })).toBeVisible(
    { timeout: 20000 },
  );
  const session = await (
    await context.request.get(url + "/api/web/session")
  ).json();
  const headers = { Origin: url, "X-CSRF-Token": session.csrf };
  const roles = await (
    await context.request.post(url + "/api/web/roles/view", {
      headers,
      data: {},
    })
  ).json();
  const role = roles.roles.find((row) => row.name === "独立生活角色");
  expect(role.capabilities).toEqual([]);
  expect(session.conversations.flatMap((row) => row.actors)).not.toContain(
    role.actor_id,
  );
  await page.goto(url + "/#/companion/1");
  await expect(page.getByLabel("选择角色")).toHaveValue(role.actor_id);
  await expect(
    page.getByLabel("选择角色").locator("option:checked"),
  ).toHaveText("独立生活角色");
  const daily = page.getByRole("region", { name: "角色日常", exact: true });
  let lastLife = "";
  await expect
    .poll(
      async () => {
        const refresh = daily.getByRole("button", {
          name: "刷新日常",
          exact: true,
        });
        await refresh.click();
        await expect(refresh).toBeEnabled();
        lastLife = await daily.innerText();
        return daily
          .getByRole("button", { name: "重试今日安排内容", exact: true })
          .isVisible();
      },
      { timeout: 45000, intervals: [300, 500, 1000] },
    )
    .toBe(true)
    .catch(() => {
      throw new Error("Generation retry is not visible: " + lastLife);
    });
  await daily
    .getByRole("button", { name: "重试今日安排内容", exact: true })
    .click();
  await expect(daily.locator(".life-notice")).toContainText("重试已受理");
  await expect
    .poll(
      async () => {
        const refresh = daily.getByRole("button", {
          name: "刷新日常",
          exact: true,
        });
        await refresh.click();
        await expect(refresh).toBeEnabled();
        const text = await daily.innerText();
        return (
          text.includes("我在书桌前整理今天的想法") &&
          text.includes("当天计划已由模型生成")
        );
      },
      { timeout: 45000, intervals: [300, 500, 1000] },
    )
    .toBe(true);
  await expect(daily).toContainText("当天计划已由模型生成");
  await expect(daily.locator(".life-plan li[data-current]")).toHaveCount(1);
  await expect(daily).toContainText("尚未发生");
  await page.screenshot({
    path: `${process.env.TS_ROLE_OUTPUT}/role-life-desktop.png`,
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: `${process.env.TS_ROLE_OUTPUT}/role-life-mobile.png`,
    fullPage: true,
  });
  await page.reload();
  await expect(daily).toContainText("我在书桌前整理今天的想法");
  await page.goto(url + "/#/companion/3");
  await page.getByRole("button", { name: /独立生活角色 已启用/ }).click();
  await page.getByRole("button", { name: "停用角色", exact: true }).click();
  await expect(
    page.getByText("角色已停用，历史记录保留。", { exact: true }),
  ).toBeVisible();
  const stopped = await context.request.post(url + "/api/web/life/today", {
    headers,
    data: { actor_id: role.actor_id },
  });
  expect(stopped.status()).toBe(200);
  const today = await stopped.json();
  expect(today.enabled).toBe(false);
  expect(today.plan.state).toBe("paused");
  console.log(
    JSON.stringify({
      actor: role.actor_id,
      generated: true,
      no_dialogue: true,
      paused: true,
    }),
  );
  await context.close();
} finally {
  await browser.close();
}
