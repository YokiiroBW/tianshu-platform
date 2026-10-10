import { test, expect } from "@playwright/test";

test("function assignments save, reload, inherit and keep chat independent", async ({
  page,
}, info) => {
  await page.goto("/#/settings/2");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("管理员密码").fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "解锁模型管理" }).click();
  const panel = page.getByRole("region", { name: "模型功能分工" });
  await expect(panel.getByRole("combobox")).toHaveCount(6);
  const code = panel
    .locator("article")
    .filter({ has: page.getByRole("heading", { name: "代码", exact: true }) });
  await page
    .getByLabel("代码模型", { exact: true })
    .selectOption({ label: "辅助模型 · fixture-specialist" });
  await page.getByRole("button", { name: "保存代码模型", exact: true }).click();
  await expect(code.getByRole("status")).toHaveText("已保存");
  await expect(code).toContainText("当前：辅助模型 · fixture-specialist");
  await expect(code).toContainText("自动调用尚未接入");
  await page.reload();
  await expect(
    page.getByLabel("代码模型", { exact: true }).locator("option:checked"),
  ).toHaveText("辅助模型 · fixture-specialist");
  const chat = panel.locator("article").filter({
    has: page.getByRole("heading", { name: "主对话", exact: true }),
  });
  await expect(chat).toContainText("当前：聊天模型 · fixture-chat");
  await page
    .getByLabel("日常与日记模型", { exact: true })
    .selectOption({ label: "辅助模型 · fixture-specialist" });
  await page
    .getByRole("button", { name: "保存日常与日记模型", exact: true })
    .click();
  await expect(
    panel
      .locator("article")
      .filter({
        has: page.getByRole("heading", { name: "日常与日记", exact: true }),
      })
      .getByRole("status"),
  ).toHaveText("已保存");
  await page.screenshot({
    path: info.outputPath("model-functions.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page
    .getByLabel("主对话模型", { exact: true })
    .selectOption({ label: "辅助模型 · fixture-specialist" });
  await page
    .getByRole("button", { name: "保存主对话模型", exact: true })
    .click();
  await expect(chat).toContainText("当前：辅助模型 · fixture-specialist");
  await expect(
    page
      .locator(".provider-card")
      .filter({
        has: page.getByRole("heading", { name: "辅助模型", exact: true }),
      }),
  ).toContainText("默认对话模型");
  await page
    .getByLabel("主对话模型", { exact: true })
    .selectOption({ label: "聊天模型 · fixture-chat" });
  await page
    .getByRole("button", { name: "保存主对话模型", exact: true })
    .click();
  await expect(chat).toContainText("当前：聊天模型 · fixture-chat");
  for (const label of ["代码", "日常与日记"]) {
    await page.getByLabel(`${label}模型`, { exact: true }).selectOption("");
    await page
      .getByRole("button", { name: `保存${label}模型`, exact: true })
      .click();
    await expect(
      panel.locator("article").filter({
        has: page.getByRole("heading", { name: label, exact: true }),
      }),
    ).toContainText("当前：聊天模型 · fixture-chat");
  }
});
