import { test, expect } from "@playwright/test";

test("memory retrieval saves independent models and all six mode combinations", async ({
  page,
}, info) => {
  await page.goto("/#/settings/10");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "记忆检索", exact: true }),
  ).toBeVisible();
  for (const name of ["嵌入", "重排序"]) {
    const legacy = page
      .getByRole("region", { name: `${name}模型连接` })
      .locator("details");
    if ((await legacy.getAttribute("open")) === null)
      await legacy.locator("summary").click();
    await page
      .getByLabel(`${name} API 基础地址`)
      .fill("https://models.example/v1");
    await page
      .getByLabel(`${name}模型名称`)
      .fill(name === "嵌入" ? "embedding-fixture" : "rerank-fixture");
    await page.getByLabel(`${name} API Key`).fill("synthetic-secret-not-real");
    await page
      .getByRole("button", { name: `保存${name}连接`, exact: true })
      .click();
    await expect(page.getByRole("status")).toContainText("已保存");
    await expect(page.getByLabel(`${name} API Key`)).toHaveValue("");
    await page
      .getByRole("button", { name: `测试${name}连接`, exact: true })
      .click();
    await expect(page.getByRole("status")).toContainText("测试通过");
  }
  for (const mode of ["keyword", "vector", "hybrid"]) {
    for (const rerank of [false, true]) {
      await page.getByLabel("检索模式", { exact: true }).selectOption(mode);
      await page.getByLabel("启用重排序").setChecked(rerank);
      await page
        .getByRole("button", { name: "保存检索模式", exact: true })
        .click();
      await expect(page.getByRole("status")).toContainText("已保存");
      await page.reload();
      await expect(page.getByLabel("检索模式", { exact: true })).toHaveValue(
        mode,
      );
      await expect(page.getByLabel("启用重排序")).toBeChecked({
        checked: rerank,
      });
    }
  }
  await expect(page.getByLabel("嵌入模型名称")).toHaveValue(
    "embedding-fixture",
  );
  await expect(page.getByLabel("重排序模型名称")).toHaveValue("rerank-fixture");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: info.outputPath("retrieval.png"),
    fullPage: true,
  });
});
