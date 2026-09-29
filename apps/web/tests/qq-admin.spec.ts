import { expect, test } from "@playwright/test";

test("HTTPS QQ profiles and administrator grant then revoke", async ({
  page,
}, testInfo) => {
  await page.goto("/#/settings/7");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("QQ 用户档案", { exact: true })).toBeVisible();
  await expect(page.getByText("QQ 1001", { exact: true })).toBeVisible();
  await expect(page.getByText("QQ 1002", { exact: true })).toBeVisible();
  await expect(page.getByText("群里的小雨", { exact: false })).toBeVisible();
  await expect(page.getByText(/person:/)).toHaveCount(0);
  await page.screenshot({
    path: testInfo.outputPath("profiles.png"),
    fullPage: true,
  });
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
