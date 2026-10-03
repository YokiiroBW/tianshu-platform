import { test, expect, type Page } from "@playwright/test";

async function fixture(
  page: Page,
  options: { locked?: boolean; memoryFailure?: boolean } = {},
) {
  let unlocked = !options.locked;
  let discoveredReads = 0;
  let memoryRevoked = false;
  let overviewReads = 0;
  const writes: Record<string, unknown>[] = [];
  const account = {
    id: "bot:one",
    name: "家里的机器人",
    account_id: "90001",
    revision: 1,
    read_enabled: true,
    enabled: true,
    state: "ready",
    private_policy: {
      observe: true,
      mode: "observe_only",
      list: [] as string[],
      actor_id: null as string | null,
    },
    group_policy: {
      observe: true,
      mode: "blacklist",
      list: ["88888"],
      actor_id: "role:a",
    },
  };
  const roles = [
    { id: "role:a", label: "橙汐", version: 1, available: true, reason: null },
    { id: "role:b", label: "雪色", version: 2, available: true, reason: null },
  ];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.route("**/api/web/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(
      "/api/web/",
      "",
    );
    const body =
      route.request().method() === "POST" ? route.request().postDataJSON() : {};
    let result: unknown;
    if (path === "session")
      result = {
        authenticated: true,
        csrf: "people-fixture",
        username: "YokiiroBW",
        conversations: [],
        dialogue: { available: false, code: "not_configured", model: "" },
      };
    else if (path === "memory/state") {
      if (options.memoryFailure)
        return route.fulfill({
          status: 503,
          json: { code: "memory_not_configured" },
        });
      result = { available: true, code: "ready", actor_id: "role:a", roles };
    } else if (path === "qq-admin/profiles")
      result = {
        items: body.after
          ? [
              {
                qq_id: "10003",
                person_id: "person:three",
                display_name: "林间风",
                aliases: [],
              },
            ]
          : [
              {
                qq_id: "10001",
                person_id: "person:one",
                display_name: "小雨",
                aliases: [
                  {
                    kind: "group_card",
                    bot_id: "90001",
                    group_id: "80001",
                    value: "群里的小雨",
                    observed_at: "2026-10-04T10:00:00Z",
                  },
                ],
              },
            ],
        next_cursor: body.after ? null : "profiles-page-2",
      };
    else if (path === "bot-observation/view")
      result = { available: true, unlocked, connections: [account] };
    else if (path === "bots/unlock") {
      unlocked = true;
      result = { unlocked: true };
    } else if (path === "bot-observation/discovered") {
      discoveredReads++;
      if (!unlocked)
        return route.fulfill({
          status: 403,
          json: { code: "management_required" },
        });
      result = {
        items: [
          {
            conversation: "private:10002",
            author: "10002",
            count: 1,
            last_at: 1791104400,
            decision: {
              observe: true,
              reply_permitted: false,
              reply_triggered: false,
            },
          },
          {
            conversation: "group:80001",
            author: "10001",
            count: 4,
            last_at: 1791104300,
            decision: {
              observe: true,
              reply_permitted: false,
              reply_triggered: false,
            },
          },
        ],
        next_cursor: null,
      };
    } else if (path === "bot-observation/archive")
      result = {
        memory_state: "available",
        backlog: {},
        items: [],
        archive_items: [
          {
            source_ref: "message:one",
            author: "10002",
            text: "hi",
            content_state: "available",
            sent_at: "2026-10-04T10:00:00Z",
          },
        ],
        archive_next_cursor: null,
      };
    else if (path === "memory/overview") {
      overviewReads++;
      if (memoryRevoked)
        return route.fulfill({
          status: 403,
          json: { code: "upstream_forbidden" },
        });
      result = { scope_version: 1 };
    } else if (path === "memory/records")
      result = {
        items:
          body.role_id === "role:b"
            ? [
                {
                  semantic_group_id: "portrait:one",
                  category: "preference",
                  field_key: "reading",
                  item_key: "books",
                  units: [
                    {
                      record_id: "r1",
                      record_version: 1,
                      statement: "小雨喜欢在傍晚读书。",
                      uncertainty: null,
                      conditions: null,
                      negations: null,
                      valid_time: null,
                      reality: null,
                    },
                  ],
                },
              ]
            : [],
        next_cursor: null,
        scope_version: 1,
        verified_at: "2026-10-04T10:00:00Z",
      };
    else if (path === "bot-observation/private-access") {
      writes.push(body);
      account.revision++;
      account.private_policy = {
        observe: true,
        mode: "whitelist",
        list: body.enabled ? [body.qq_id] : [],
        actor_id: body.actor_id || account.private_policy.actor_id,
      };
      result = { connection: account };
    } else
      return route.fulfill({ status: 404, json: { code: "not_configured" } });
    return route.fulfill({ json: result });
  });
  await page.goto("/#/memory/0");
  return {
    writes,
    errors,
    discoveredReads: () => discoveredReads,
    setMemoryRevoked: (value: boolean) => {
      memoryRevoked = value;
    },
    overviewReads: () => overviewReads,
  };
}

test("people directory keeps observed hi users and separates shared portrait from identity", async ({
  page,
}, testInfo) => {
  const state = await fixture(page);
  const directory = page.getByRole("complementary", { name: "用户目录" });
  await expect(
    directory.getByText("QQ 10002", { exact: true }).last(),
  ).toBeVisible();
  await directory.getByRole("button").filter({ hasText: "QQ 10002" }).click();
  await expect(
    page.getByRole("heading", { name: "QQ 10002", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "最近互动", exact: true }).click();
  await expect(page.getByText("hi", { exact: true })).toBeVisible();
  await page.getByRole("tab", { name: "画像与记忆", exact: true }).click();
  await expect(
    page.getByText("人物身份尚未确认；收到消息不代表已经建立画像。"),
  ).toBeVisible();
  await directory.getByRole("button").filter({ hasText: "QQ 10001" }).click();
  await expect(page.getByText("群里的小雨", { exact: false })).toBeVisible();
  await expect(
    page.locator(".people-detail").getByText("person:one"),
  ).toHaveCount(0);
  await page.screenshot({
    path: testInfo.outputPath("people-overview.png"),
    fullPage: true,
  });
  await page.evaluate(() => (document.documentElement.dataset.theme = "dark"));
  await page.screenshot({
    path: testInfo.outputPath("people-overview-dark.png"),
    fullPage: true,
  });
  await page.evaluate(() => (document.documentElement.dataset.theme = "light"));
  await page.getByRole("tab", { name: "画像与记忆", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "还没有形成共享画像" }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "查看角色", exact: true })
    .selectOption("role:b");
  await expect(
    page.getByRole("tab", { name: "概览", exact: true }),
  ).toHaveAttribute("aria-selected", "true");
  await page.getByRole("tab", { name: "画像与记忆", exact: true }).click();
  await expect(page.getByText("小雨喜欢在傍晚读书。")).toBeVisible();
  await page.getByRole("button", { name: "加载更多用户", exact: true }).click();
  await expect(directory.getByText("QQ 10003", { exact: true })).toBeVisible();
  await page
    .getByRole("textbox", { name: "搜索已加载用户" })
    .fill("群里的小雨");
  await expect(directory.getByText("QQ 10001", { exact: true })).toBeVisible();
  await expect(directory.getByText("QQ 10002", { exact: true })).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  expect(state.errors).toEqual([]);
});

test("people observation unlock reveals contacts and targets only selected private reply permission", async ({
  page,
}) => {
  const state = await fixture(page, { locked: true });
  await expect(
    page.getByRole("button", { name: "解锁用户会话" }),
  ).toBeVisible();
  expect(state.discoveredReads()).toBe(0);
  await page
    .getByRole("textbox", { name: "管理员密码", exact: true })
    .fill("synthetic-only");
  await page.getByRole("button", { name: "解锁用户会话" }).click();
  const directory = page.getByRole("complementary", { name: "用户目录" });
  await directory.getByRole("button").filter({ hasText: "QQ 10002" }).click();
  await page.getByRole("tab", { name: "回复权限", exact: true }).click();
  await page
    .getByRole("combobox", { name: "家里的机器人默认回复角色", exact: true })
    .selectOption("role:a");
  await page
    .getByRole("button", { name: "允许回复此用户", exact: true })
    .click();
  await expect(page.getByText("这位用户的私聊回复权限已保存。")).toBeVisible();
  expect(state.writes).toHaveLength(1);
  expect(state.writes[0]).toMatchObject({
    id: "bot:one",
    qq_id: "10002",
    expected_revision: 1,
    actor_id: "role:a",
    enabled: true,
  });
  expect(state.writes[0]).not.toHaveProperty("group_policy");
  await page
    .getByRole("button", { name: "停止回复此用户", exact: true })
    .click();
  expect(state.writes[1]).toMatchObject({
    qq_id: "10002",
    expected_revision: 2,
    actor_id: null,
    enabled: false,
  });
  expect(state.errors).toEqual([]);
});

test("QQ identity directory remains readable when memory service fails", async ({
  page,
}) => {
  const state = await fixture(page, { memoryFailure: true });
  const directory = page.getByRole("complementary", { name: "用户目录" });
  await expect(directory.getByText("QQ 10001", { exact: true })).toBeVisible();
  await directory.getByRole("button").filter({ hasText: "QQ 10001" }).click();
  await expect(page.getByText("群里的小雨", { exact: false })).toBeVisible();
  await page.getByRole("tab", { name: "画像与记忆", exact: true }).click();
  await expect(page.getByText("请选择查看角色。")).toBeVisible();
  await expect(page.getByText("还没有形成共享画像")).toHaveCount(0);
  expect(state.errors).toEqual([]);
});

test("portrait scope revalidation hides revoked records and rechecks when visible", async ({
  page,
}) => {
  await page.clock.install({ time: new Date("2026-10-04T10:00:00Z") });
  const state = await fixture(page);
  await page
    .getByRole("complementary", { name: "用户目录" })
    .getByRole("button")
    .filter({ hasText: "QQ 10001" })
    .click();
  await page
    .getByRole("combobox", { name: "查看角色", exact: true })
    .selectOption("role:b");
  await page.getByRole("tab", { name: "画像与记忆", exact: true }).click();
  await expect(page.getByText("小雨喜欢在傍晚读书。")).toBeVisible();
  state.setMemoryRevoked(true);
  await page.clock.fastForward(15001);
  await expect(page.getByText("小雨喜欢在傍晚读书。")).toHaveCount(0);
  await expect(
    page.getByRole("alert").filter({ hasText: "upstream_forbidden" }),
  ).toBeVisible();
  await expect(
    page
      .getByRole("complementary", { name: "用户目录" })
      .getByText("QQ 10001", { exact: true }),
  ).toBeVisible();
  const previous = state.overviewReads();
  state.setMemoryRevoked(false);
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await expect(page.getByText("小雨喜欢在傍晚读书。")).toBeVisible();
  expect(state.overviewReads()).toBeGreaterThan(previous);
  expect(state.errors).toEqual([]);
});
