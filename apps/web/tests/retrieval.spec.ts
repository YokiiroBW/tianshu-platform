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
    await page
      .getByRole("region", { name: `${name}模型连接` })
      .getByRole("button", { name: "原独立连接设置", exact: true })
      .click();
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
    await expect(page.getByRole("dialog").getByRole("status")).toContainText(
      "已保存",
    );
    await expect(page.getByLabel(`${name} API Key`)).toHaveValue("");
    await page
      .getByRole("button", { name: `测试${name}连接`, exact: true })
      .click();
    await expect(page.getByRole("dialog").getByRole("status")).toContainText(
      "测试通过",
    );
    await page.keyboard.press("Escape");
  }
  for (const mode of ["keyword", "vector", "hybrid"]) {
    for (const rerank of [false, true]) {
      await page
        .getByRole("button", { name: "检索模式设置", exact: true })
        .click();
      await page.getByLabel("检索模式", { exact: true }).selectOption(mode);
      await page.getByLabel("启用重排序").setChecked(rerank);
      await page
        .getByRole("button", { name: "保存检索模式", exact: true })
        .click();
      await expect(page.getByRole("dialog").getByRole("status")).toContainText(
        "已保存",
      );
      await page.reload();
      await page
        .getByRole("button", { name: "检索模式设置", exact: true })
        .click();
      await expect(page.getByLabel("检索模式", { exact: true })).toHaveValue(
        mode,
      );
      await expect(page.getByLabel("启用重排序")).toBeChecked({
        checked: rerank,
      });
      await page.keyboard.press("Escape");
    }
  }
  for (const [name, model] of [
    ["嵌入", "embedding-fixture"],
    ["重排序", "rerank-fixture"],
  ]) {
    await page
      .getByRole("region", { name: `${name}模型连接` })
      .getByRole("button", { name: "原独立连接设置", exact: true })
      .click();
    await expect(page.getByLabel(`${name}模型名称`)).toHaveValue(model);
    await page.keyboard.press("Escape");
  }
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
