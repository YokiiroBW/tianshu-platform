import { expect, test, type Page } from "@playwright/test";

const admin = "synthetic-admin";
const password = "synthetic-local-password-014";

async function api(page: Page, path: string, body: object) {
  const origin = new URL(page.url()).origin;
  const session = await page.request.get(`${origin}/api/web/session`);
  const { csrf } = (await session.json()) as { csrf: string };
  return page.request.post(`${origin}/api/web/personas/${path}`, {
    headers: { "X-CSRF-Token": csrf, Origin: origin },
    data: body,
  });
}

test("real HTTPS persona authoring, conflict and role readback", async ({
  page,
}) => {
  await page.goto("/#/companion/2");
  await page.getByLabel("管理员账号").fill(admin);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  await page.getByRole("button", { name: "创建与编辑" }).click();
  await expect(page.locator(".persona-read-details")).not.toHaveAttribute(
    "open",
    "",
  );
  const editor = page.locator(".persona-author");
  await expect(editor.getByText("创建与编辑")).toBeVisible();
  await editor.locator(".persona-author-field input").fill("温和伙伴");
  const boxes = editor.locator(".persona-author-field textarea");
  await boxes.nth(0).fill("隔离演练档案");
  await boxes.nth(1).fill("你是温和、可信的陪伴者。\n保持真诚。");
  await boxes.nth(2).fill("平静而亲切");
  await boxes.nth(3).fill("用简短的自然语言回答");
  await boxes.nth(4).fill("朋友");
  await editor.getByRole("button", { name: "保存草稿" }).click();
  await expect(
    editor.getByText("草稿已保存，运行中的人格没有改变。"),
  ).toBeVisible();
  await expect(
    editor.locator(".persona-author-list").getByText("温和伙伴"),
  ).toBeVisible();
  await page.screenshot({
    path: "apps/web/test-results/persona-author-desktop.png",
    fullPage: true,
  });

  await boxes.nth(1).fill("你是温和、可信且耐心的陪伴者。\n保持真诚。");
  await editor.getByRole("button", { name: "保存草稿" }).click();
  await expect(
    editor.getByText("草稿已保存，运行中的人格没有改变。"),
  ).toBeVisible();
  await editor
    .locator(".persona-target-options button")
    .filter({ hasText: "actor:a" })
    .click();
  await expect(editor.getByText(/目标 actor:a · 读取版本/)).toBeVisible();
  await editor.getByRole("button", { name: "保存并应用" }).click();
  await expect(editor.getByText(/已应用到 actor:a/)).toBeVisible();

  const role = await api(page, "view", { id: "actor:a" });
  expect(role.ok()).toBeTruthy();
  const body = (await role.json()) as {
    item: {
      content: { persona: string };
      published_revision: string | null;
      draft_revision: string | null;
      version: number;
    };
  };
  expect(body.item.content.persona).toBe(
    "你是温和、可信且耐心的陪伴者。\n保持真诚。",
  );
  expect(body.item.published_revision).not.toBeNull();
  expect(body.item.draft_revision).toBeNull();

  const stale = await api(page, "save", {
    id: "actor:a",
    name: "旧表单",
    description: "",
    expected: body.item.version - 1,
    client_id: "stale-role-form-0001",
    content: { persona: "不能覆盖", tone: "", style: "", address: "" },
  });
  expect(stale.status()).toBe(409);
  expect((await stale.json()).code).toBe("version_conflict");
  const oversized = await api(page, "save", {
    id: "actor:a",
    name: "过长正文",
    description: "",
    expected: body.item.version,
    client_id: "oversized-role-form-0001",
    content: { persona: "字".repeat(20001), tone: "", style: "", address: "" },
  });
  expect(oversized.status()).toBe(400);
  expect((await oversized.json()).code).toBe("invalid_input");
  const outside = await api(page, "view", { id: "actor:outside" });
  expect(outside.status()).toBe(403);

  await editor
    .locator(".persona-author-list .persona-subject")
    .filter({ hasText: "actor:b" })
    .click();
  await expect(editor.locator(".persona-author-field input")).toHaveValue(
    "actor:b",
  );
  await editor.locator(".persona-author-field input").fill("乙的角色");
  await boxes.nth(1).fill("乙的新版人格\n保留上下文。");
  await editor.getByRole("button", { name: "保存并应用" }).click();
  await expect(editor.getByText(/已应用到 actor:b/)).toBeVisible();
  const directRole = await api(page, "view", { id: "actor:b" });
  expect(directRole.ok()).toBeTruthy();
  const roleB = (await directRole.json()) as {
    item: { version: number; content: { persona: string } };
  };
  expect(roleB.item.content.persona).toBe(
    "乙的新版人格\n保留上下文。",
  );
  await expect(
    editor.locator(".persona-author-list").getByText("乙的角色"),
  ).toBeVisible();

  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: "apps/web/test-results/persona-author-mobile.png",
    fullPage: true,
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await boxes.nth(1).fill("本地尚未保存的编辑");
  const concurrent = await api(page, "save", {
    id: "actor:b",
    name: "乙的角色",
    description: "",
    expected: roleB.item.version,
    client_id: "concurrent-role-save-0001",
    content: { persona: "另一个编辑", tone: "", style: "", address: "" },
  });
  expect(concurrent.ok()).toBeTruthy();
  await editor.getByRole("button", { name: "保存草稿" }).click();
  await expect(editor.getByRole("alert")).toContainText("version_conflict");
  await expect(boxes.nth(1)).toHaveValue("本地尚未保存的编辑");
  await page.screenshot({
    path: "apps/web/test-results/persona-author-conflict.png",
    fullPage: true,
  });
  page.once("dialog", (dialog) => void dialog.accept());
  await editor
    .locator(".persona-author-list .persona-subject")
    .filter({ hasText: "actor:b" })
    .click();
  await expect(boxes.nth(1)).toHaveValue("另一个编辑");
  await editor.getByRole("button", { name: "复制为新档案" }).click();
  await expect(editor.locator(".persona-author-field input")).toHaveValue(
    "乙的角色 副本",
  );
  await editor.getByRole("button", { name: "保存草稿" }).click();
  await expect(
    editor.getByText("草稿已保存，运行中的人格没有改变。"),
  ).toBeVisible();
  await page.getByRole("button", { name: "退出登录" }).click();
  const afterLogout = await api(page, "profiles", {});
  expect(afterLogout.status()).toBe(401);
});

test("a reader can view personas but cannot create or apply", async ({
  page,
}) => {
  await page.goto("https://127.0.0.1:4841/#/companion/2");
  await page.getByLabel("管理员账号").fill(admin);
  await page.getByLabel("密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.locator(".persona-state")).toContainText("已授权角色 2 个");
  await page.getByRole("button", { name: "创建与编辑" }).click();
  const editor = page.locator(".persona-author");
  await expect(editor.getByText("创建与编辑")).toBeVisible();
  await expect(editor.getByText(/创建、编辑和应用权限未开放/)).toBeVisible();
  await expect(editor.getByRole("button", { name: "新建档案" })).toBeDisabled();
  const refused = await api(page, "create", {
    name: "只读账号不能创建",
    description: "",
    client_id: "read-only-create-0001",
    content: { persona: "不可保存的人格", tone: "", style: "", address: "" },
  });
  expect(refused.status()).toBe(403);
  expect((await refused.json()).code).toBe("persona_write_required");
});
