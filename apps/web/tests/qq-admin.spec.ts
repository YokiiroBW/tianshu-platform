import { expect, test } from "@playwright/test";

test("HTTPS QQ profiles and administrator grant then revoke", async ({
  page,
}, testInfo) => {
  await page.goto("/#/memory/0");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  const directory = page.getByRole("complementary", { name: "用户目录" });
  await expect(directory).toBeVisible();
  await expect(directory.getByText("QQ 1001", { exact: true })).toBeVisible();
  await expect(directory.getByText("QQ 1002", { exact: true })).toBeVisible();
  // The real Memory fixture has identities but no portrait or observation setup.
  // Both same-named people remain visible, and a group alias finds the right one.
  await page
    .getByRole("textbox", { name: "搜索已加载用户" })
    .fill("群里的小雨");
  await expect(directory.getByText("QQ 1002", { exact: true })).toBeVisible();
  await expect(directory.getByText("QQ 1001", { exact: true })).toHaveCount(0);
  await page.getByRole("textbox", { name: "搜索已加载用户" }).clear();
  await expect(directory.getByText(/person:/)).toHaveCount(0);
  await directory.getByRole("button").filter({ hasText: "QQ 1002" }).click();
  await expect(page.getByText("群里的小雨", { exact: false })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("profiles.png"),
    fullPage: true,
  });
  await page.goto("/#/settings/7");
  await expect(
    page.getByRole("heading", { name: "QQ 管理身份", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("link", { name: "前往用户档案" }),
  ).toHaveAttribute("href", "#/memory/0");
  await page.getByLabel("QQ 号", { exact: true }).fill("1001");
  await page.getByLabel("备注称呼").fill("合成管理员");
  await page.getByText("高级设置：手动指定角色范围").click();
  await page.getByLabel(/角色标识/).fill("actor:a");
  await page.getByLabel(/适用会话/).fill("group:123");
  await page.getByRole("button", { name: "保存管理身份" }).click();
  await expect(page.getByText("合成管理员", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText("合成管理员", { exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("granted.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "撤销", exact: true }).click();
  await expect(page.getByText("合成管理员", { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText("合成管理员", { exact: true })).toHaveCount(0);
  await page.screenshot({
    path: testInfo.outputPath("revoked.png"),
    fullPage: true,
  });
});
