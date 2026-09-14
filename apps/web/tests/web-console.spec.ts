import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

test("synthetic UI state fixture: receipt, collector, processing, segments, unknown and cancel", async ({
  page,
}) => {
  await page.goto("/#/companion");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  await page.route("**/api/web/session", async (route) => {
    const response = await route.fetch();
    const session = await response.json();
    await route.fulfill({
      response,
      json: {
        ...session,
        dialogue: { available: true, code: "ready", model: "unverified" },
      },
    });
  });
  let phase = "collector";
  await page.route("**/api/web/snapshot", async (route) => {
    const turn = {
      turn_id: "turn:fixture",
      turn_sequence: 1,
      version: 3,
      phase: phase === "unknown" ? "closed_unknown" : "generating",
      delivery_state: phase === "unknown" ? "unknown" : "not_started",
      unresolved_delivery: phase === "unknown",
    };
    const view = {
      turn,
      messages: [
        {
          message_id: "message:fixture",
          revision: 1,
          parts: [{ kind: "text", text: "合成原文" }],
          state: "active",
          sent_at: "2026-09-14T06:00:00Z",
        },
      ],
      replies:
        phase === "unknown"
          ? [
              {
                reply_id: "reply:1",
                segment_sequence: 1,
                segment_count: 2,
                state: "sent",
                content_state: "available",
                text: "合成已送达片段",
              },
              {
                reply_id: "reply:2",
                segment_sequence: 2,
                segment_count: 2,
                state: "unknown",
                content_state: "unavailable",
                text: null,
              },
            ]
          : [],
    };
    await route.fulfill({
      json: {
        state: "current",
        submissions: [],
        snapshot: {
          observed_at: "2026-09-14T06:00:00Z",
          collectors:
            phase === "collector"
              ? [
                  {
                    collection_id: "collection:fixture",
                    revision: 1,
                    deadline_at: "2026-09-14T06:00:05Z",
                    messages: view.messages,
                  },
                ]
              : [],
          active_turns: phase === "generating" ? [view] : [],
          history: phase === "unknown" ? [view] : [],
          next_before_turn_sequence: null,
        },
      },
    });
  });
  await page.route("**/api/web/messages", async (route) => {
    expect(Object.keys(route.request().postDataJSON()).sort()).toEqual([
      "actor",
      "client_id",
      "conversation",
      "text",
    ]);
    phase = "generating";
    await route.fulfill({
      json: { message_id: "message:fixture", state: "accepted", result: null },
    });
  });
  await page.route("**/api/web/cancel", async (route) => {
    expect(route.request().postDataJSON().expected_version).toBe(3);
    phase = "unknown";
    await route.fulfill({ json: { state: "unknown", version: 4 } });
  });
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "正在合并续句" }),
  ).toBeVisible();
  await page.getByLabel("想说些什么").fill("合成原文");
  await page.getByRole("button", { name: "发送", exact: true }).click();
  await expect(
    page.getByText("入站已接收，等待后台状态；尚不代表已回复。"),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "第 1 轮 · 后台处理中" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "取消第 1 轮" }).click();
  await expect(
    page.getByRole("heading", { name: "第 1 轮 · 发送结果未知" }),
  ).toBeVisible();
  await expect(page.getByText("合成已送达片段", { exact: true })).toBeVisible();
  await expect(page.getByText("正文暂不可展示")).toBeVisible();
  const diagnostics = page.getByText("版本 3 · closed_unknown / unknown", {
    exact: true,
  });
  await expect(diagnostics).toBeHidden();
  await page.getByText("状态详情", { exact: true }).focus();
  await page.keyboard.press("Enter");
  await expect(diagnostics).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(diagnostics).toBeHidden();
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
});

test("real local login, keyboard, unavailable dialogue, reload and logout", async ({
  page,
}, testInfo) => {
  await page.goto("/#/companion");
  await expect(page.getByLabel("管理员账号")).toBeEnabled();
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByLabel("密码", { exact: true }).press("Tab");
  await expect(
    page.getByRole("button", { name: "登录", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  await expect(
    page.getByRole("combobox", { name: "角色", exact: true }),
  ).toHaveValue("actor:a");
  await page
    .getByRole("combobox", { name: "角色", exact: true })
    .selectOption("actor:b");
  await expect(
    page.getByRole("heading", { name: "对话通道尚未接通" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "发送", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("想说些什么").fill("合成草稿，不发送");
  const violations = (await new AxeBuilder({ page }).analyze()).violations;
  expect(violations).toEqual([]);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("web-console-light.png"),
    fullPage: true,
  });
  await page.reload();
  await expect(page.getByRole("button", { name: "退出登录" })).toBeVisible();
  await expect(page.getByLabel("想说些什么")).toHaveValue("");
  await page.getByRole("button", { name: "外观设置" }).click();
  await page.getByLabel("深色", { exact: true }).check();
  await page.keyboard.press("Escape");
  await page.screenshot({
    path: testInfo.outputPath("web-console-dark.png"),
    fullPage: true,
  });
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(page.getByLabel("管理员账号")).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("管理员账号")).toBeEnabled();
});

test("wrong password and network interruption never show a conversation", async ({
  page,
}) => {
  await page.goto("/#/companion");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-wrong-password");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("账号或密码不正确");
  await expect(page.getByLabel("密码", { exact: true })).toHaveValue("");
  await page.context().setOffline(true);
  await page.getByRole("button", { name: "重新连接" }).click();
  await expect(page.getByLabel("管理员账号")).toBeDisabled();
  await page.context().setOffline(false);
  await expect(page.getByLabel("管理员账号")).toBeEnabled();
  await expect(page.getByRole("button", { name: "退出登录" })).toHaveCount(0);
});
