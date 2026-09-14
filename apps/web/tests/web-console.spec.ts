import { test, expect, type Page } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const ADMIN = "synthetic-admin";
const ADMIN_PASSWORD = "synthetic-local-password-014";

async function loginModels(page: Page) {
  await page.goto("/#/settings/2");
  await page.getByLabel("管理员账号").fill(ADMIN);
  await page.getByLabel("密码", { exact: true }).fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  // The management state itself is asserted by each test; being logged in is not authority.
  await expect(page.getByRole("button", { name: "重新读取" })).toBeVisible();
}

async function unlockModels(page: Page) {
  await page.getByLabel("管理员密码").fill(ADMIN_PASSWORD);
  await page.getByRole("button", { name: "解锁模型管理" }).click();
  await expect(page.getByText("管理已解锁", { exact: true })).toBeVisible();
}

function managementState(page: Page, label: string) {
  return page.locator(".rail-label").filter({ hasText: label });
}

function target(page: Page, label: string) {
  return page.locator("article.models-target").filter({ hasText: label });
}

function currentVersion(page: Page, label: string) {
  return target(page, label).locator(".models-facts dd").first();
}

/** Version lists are newest first and numbers repeat as substrings: match the label exactly. */
function versionItem(page: Page, label: string, version: number) {
  return target(page, label)
    .locator("li")
    .filter({
      has: page.locator(".models-version", {
        hasText: new RegExp(`^版本 ${version}$`),
      }),
    });
}

async function plannedVersion(page: Page) {
  const button = page.getByRole("button", { name: /^发布版本 \d+$/ });
  return Number((await button.innerText()).replace(/\D+/g, ""));
}

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

test("real console model management: gated unlock, preview, publish and revoke", async ({
  page,
}, testInfo) => {
  await loginModels(page);
  // An ordinary chat login never carries the management write port.
  await expect(page.getByRole("button", { name: "预览" })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /^撤销版本 \d+$/ }),
  ).toHaveCount(0);
  const nativeBefore = await currentVersion(
    page,
    "原生 Responses 配置",
  ).innerText();
  await unlockModels(page);
  await page.getByLabel("配置模板").selectOption("chat-local-text");
  await page.getByRole("button", { name: "预览" }).click();
  await expect(page.locator(".models-preview")).toBeVisible();
  const version = await plannedVersion(page);
  await page.getByRole("button", { name: `发布版本 ${version}` }).click();
  await expect(page.locator(".models-notice")).toContainText(
    `已发布版本 ${version}，可用至`,
  );
  await expect(currentVersion(page, "Chat 兼容配置")).toHaveText(
    String(version),
  );
  // Chat and native keep separate version sequences: one never moves the other.
  await expect(currentVersion(page, "原生 Responses 配置")).toHaveText(
    nativeBefore,
  );
  const chatVersion = versionItem(page, "Chat 兼容配置", version);
  await expect(chatVersion.locator(".rail-label")).toHaveText("可用");
  expect((await new AxeBuilder({ page }).analyze()).violations).toEqual([]);
  await page.screenshot({
    path: testInfo.outputPath("models-published.png"),
    fullPage: true,
  });
  await chatVersion
    .getByRole("button", { name: `撤销版本 ${version}` })
    .click();
  await expect(page.locator(".models-notice")).toContainText(
    `已撤销版本 ${version}`,
  );
  await expect(
    versionItem(page, "Chat 兼容配置", version).locator(".rail-label"),
  ).toHaveText("已撤销");
  await expect(managementState(page, "管理已解锁")).toBeVisible();
  await page.getByRole("button", { name: "锁定管理" }).click();
  await expect(managementState(page, "管理待验证")).toBeVisible();
  await expect(page.getByRole("button", { name: "预览" })).toHaveCount(0);
});

test("real console: a second browser with a stale version conflicts instead of overwriting", async ({
  page,
  browser,
}) => {
  const second = await browser.newContext();
  const other = await second.newPage();
  try {
    await loginModels(page);
    await unlockModels(page);
    await loginModels(other);
    await unlockModels(other);
    for (const view of [page, other]) {
      await view.getByLabel("配置模板").selectOption("chat-local-text");
      await view.getByRole("button", { name: "预览" }).click();
      await expect(view.locator(".models-preview")).toBeVisible();
    }
    const version = await plannedVersion(page);
    await page.getByRole("button", { name: `发布版本 ${version}` }).click();
    await expect(page.locator(".models-notice")).toContainText(
      `已发布版本 ${version}`,
    );
    // The second browser still holds the version it read before the first publish.
    await other.getByRole("button", { name: /^发布版本 \d+$/ }).click();
    await expect(other.locator(".models-error")).toContainText(
      "后台版本已变化",
    );
    await expect(other.locator(".models-notice")).toContainText(
      "已按后台当前版本重新读取；请重新预览后再发布。",
    );
    await expect(
      other.getByRole("button", { name: /^发布版本 \d+$/ }),
    ).toHaveCount(0);
    await expect(currentVersion(other, "Chat 兼容配置")).toHaveText(
      String(version),
    );
  } finally {
    await second.close();
  }
});

test("real console: a retried publish replays instead of creating another version", async ({
  page,
}) => {
  await loginModels(page);
  await unlockModels(page);
  const chatBefore = await currentVersion(page, "Chat 兼容配置").innerText();
  const result = await page.evaluate(async () => {
    const send = async (path: string, body: object) => {
      const session = await (
        await fetch("/api/web/session", { credentials: "same-origin" })
      ).json();
      const response = await fetch(`/api/web/${path}`, {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": session.csrf,
        },
        body: JSON.stringify(body),
      });
      return { status: response.status, body: await response.json() };
    };
    const view = await send("models/view", {});
    const native = view.body.targets.find(
      (item: { target: string }) => item.target === "native",
    );
    const request = {
      template_id: "native-local-text",
      expected_version: native.current_version,
      client_id: crypto.randomUUID(),
    };
    const first = await send("models/publish", request);
    const second = await send("models/publish", request);
    return { first, second };
  });
  expect(result.first.status).toBe(200);
  expect(result.first.body.state).toBe("published");
  expect(result.second.status).toBe(200);
  expect(result.second.body.state).toBe("replayed");
  expect(result.second.body.version).toBe(result.first.body.version);
  await page.getByRole("button", { name: "重新读取" }).click();
  await expect(currentVersion(page, "原生 Responses 配置")).toHaveText(
    String(result.first.body.version),
  );
  await expect(currentVersion(page, "Chat 兼容配置")).toHaveText(chatBefore);
});

test("real console: losing the session returns to login without management state", async ({
  page,
}) => {
  await loginModels(page);
  await unlockModels(page);
  await page.context().clearCookies();
  await page.getByRole("button", { name: "重新读取" }).click();
  await expect(page.getByLabel("管理员账号")).toBeVisible();
  await expect(managementState(page, "管理已解锁")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "预览" })).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: /^发布版本 \d+$/ }),
  ).toHaveCount(0);
});

test("synthetic UI state fixture: management disabled and unauthorized states", async ({
  page,
}) => {
  await page.route("**/api/web/models/view", async (route) => {
    await route.fulfill({
      json: {
        management: {
          available: false,
          code: "management_disabled",
          unlocked: false,
          unlock_ttl_seconds: 900,
          templates: [],
        },
        targets: [
          {
            target: "chat",
            label: "Chat 兼容配置",
            detail: "text-dialogue/v1 · companion.text",
            protocol: "openai-chat-completions",
            workload: "companion.text",
            current_version: null,
            availability: "unconfigured",
            usable_until: null,
            versions: [],
          },
        ],
      },
    });
  });
  await loginModels(page);
  await expect(
    page.getByRole("heading", { name: "这个部署没有开启网页模型管理" }),
  ).toBeVisible();
  await expect(managementState(page, "管理未开启")).toBeVisible();
  await expect(page.getByRole("button", { name: "预览" })).toHaveCount(0);
  await expect(page.getByLabel("管理员密码")).toHaveCount(0);
  await page.unroute("**/api/web/models/view");
  await page.route("**/api/web/models/view", async (route) => {
    await route.fulfill({
      json: {
        management: {
          available: false,
          code: "operator_not_authorized",
          unlocked: false,
          unlock_ttl_seconds: 900,
          templates: [],
        },
        targets: [],
      },
    });
  });
  await page.getByRole("button", { name: "重新读取" }).click();
  await expect(
    page.getByRole("heading", { name: "当前账号没有模型配置权限" }),
  ).toBeVisible();
  await expect(managementState(page, "无管理权限")).toBeVisible();
});

test("synthetic UI state fixture: an expired session blocks management without false success", async ({
  page,
}) => {
  await page.route("**/api/web/models/view", async (route) => {
    await route.fulfill({ status: 401, json: { code: "session_expired" } });
  });
  await loginModels(page);
  // The management read never becomes a silent empty panel or an unlocked state.
  await expect(page.locator(".models-error")).toContainText("登录已过期");
  await expect(managementState(page, "管理已解锁")).toHaveCount(0);
  await expect(page.getByRole("button", { name: "解锁模型管理" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("button", { name: "预览" })).toHaveCount(0);
  await expect(page.locator(".models-target")).toHaveCount(0);
  await page.unroute("**/api/web/models/view");
  await page.getByRole("button", { name: "重新读取" }).click();
  await expect(currentVersion(page, "Chat 兼容配置")).toBeVisible();
  await expect(page.locator(".models-error")).toHaveCount(0);
});
