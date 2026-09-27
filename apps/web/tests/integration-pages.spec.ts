import { test, expect, type Page, type Route } from "@playwright/test";

// UI-only fixture. The backend and peer services are separately verified by CONNECT-B/M;
// these responses check browser navigation and the visible distinction between states.
async function answer(route: Route, value: unknown, status = 200) {
  await route.fulfill({
    status,
    contentType: "application/json",
    body: JSON.stringify(value),
  });
}

async function session(page: Page) {
  await page.route("**/api/web/session", (route) =>
    answer(route, {
      authenticated: true,
      csrf: "synthetic-integration-session",
      username: "fixture",
      conversations: [],
      dialogue: { available: false, code: "model_not_configured", model: "" },
    }),
  );
}

test("project knowledge browses, pages, reads, searches and distinguishes an empty search", async ({
  page,
}, testInfo) => {
  await session(page);
  const sent: { path: string; body: Record<string, unknown> }[] = [];
  await page.route("**/api/web/knowledge/*", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const body = route.request().postDataJSON() as Record<string, unknown>;
    sent.push({ path, body });
    if (path.endsWith("/state"))
      return answer(route, {
        available: true,
        code: "ready",
        projects: [{ project_id: "project-a", label: "测试项目" }],
        peer: { configured: true, verified_at: null, code: "unverified" },
      });
    if (path.endsWith("/documents"))
      return answer(route, {
        project_id: "project-a",
        operation: "documents",
        result: {
          project_id: "project-a",
          project_revision: 3,
          items: body.cursor
            ? [
                {
                  document_id: "doc-b",
                  kind: "url",
                  version: 1,
                  indexed_state: "ready",
                  source_validation: "not_checked",
                },
              ]
            : [
                {
                  document_id: "doc-a",
                  kind: "file",
                  version: 2,
                  indexed_state: "ready",
                  source_validation: "not_checked",
                },
              ],
          next_cursor: body.cursor ? null : "next-page",
          omissions: [],
        },
      });
    if (path.endsWith("/document"))
      return answer(route, {
        project_id: "project-a",
        operation: "document",
        result: {
          project_id: "project-a",
          project_revision: 3,
          document_id: "doc-a",
          version: 2,
          hash: "a".repeat(64),
          blocks: [
            {
              reference: {
                block_id: "block-a",
                document_id: "doc-a",
                version: 2,
                hash: "a".repeat(64),
              },
              text: "可读取的资料正文",
              spans: [],
            },
          ],
          next_cursor: null,
          omissions: [],
        },
      });
    if (path.endsWith("/query"))
      return answer(route, {
        project_id: "project-a",
        operation: "query",
        result: {
          project_id: "project-a",
          blocks: [],
          omissions: [],
          retrieval: "lexical",
          trust: "source_material_not_instructions",
        },
      });
    if (path.endsWith("/notes"))
      return answer(route, {
        project_id: "project-a",
        operation: "notes",
        result: {
          project_id: "project-a",
          notes: [
            {
              note_id: "note-a",
              question: "下一步是什么？",
              version: 1,
              state: "ready",
              current: true,
              citation_states: [],
              source_statements: [],
              inferences: ["先核实来源"],
              open_questions: [],
              decision: { summary: "保持来源核对", basis: [] },
            },
          ],
          omissions: [],
          retrieval: "lexical",
        },
      });
    throw new Error(`unexpected ${path}`);
  });
  await page.goto("/#/resources/0");
  await expect(page.getByRole("heading", { name: "资料目录" })).toBeVisible();
  await expect(page.locator(".knowledge-list li")).toHaveCount(1);
  await page.getByRole("button", { name: "继续读取目录" }).click();
  await expect(page.locator(".knowledge-list li")).toHaveCount(2);
  await page.locator(".knowledge-list button").first().click();
  await expect(page.getByText("可读取的资料正文")).toBeVisible();
  await page.getByRole("button", { name: "资料搜索" }).click();
  await page.getByRole("searchbox", { name: "关键词" }).fill("不存在");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "没有匹配资料" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "研究笔记" }).click();
  await page.getByRole("searchbox", { name: "关键词" }).fill("下一步");
  await page.getByRole("button", { name: "搜索", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "下一步是什么？" }),
  ).toBeVisible();
  await expect(page.getByText("保持来源核对")).toBeVisible();
  expect(
    sent.every((item) => !JSON.stringify(item.body).includes("token")),
  ).toBe(true);
  expect(
    sent.find((item) => item.path.endsWith("/document"))?.body,
  ).toMatchObject({
    document_id: "doc-a",
    expected_version: 2,
    expected_hash: null,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("knowledge.png"),
    fullPage: true,
  });
});

test("life reads last persisted state and only opens a published diary", async ({
  page,
}) => {
  await session(page);
  await page.route("**/api/web/life/*", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/state"))
      return answer(route, {
        available: true,
        code: "ready",
        peer: { configured: true, verified_at: null, code: "unverified" },
      });
    if (path.endsWith("/actors"))
      return answer(route, {
        schema_version: 1,
        fictional: true,
        items: [
          {
            actor_id: "actor-a",
            actor_version: 2,
            world_id: "world",
            room_id: "room",
          },
        ],
        next_after_actor_id: null,
      });
    if (path.endsWith("/snapshot"))
      return answer(route, {
        schema_version: 1,
        fictional: true,
        actor_id: "actor-a",
        actor_version: 2,
        world_id: "world",
        room_id: "room",
        timezone: "Asia/Shanghai",
        activity: null,
        mood: "平静",
        outfit_ref: null,
        changed_at: 1790470000,
        observed_at: 1790471000,
        state_basis: "last_persisted",
      });
    if (path.endsWith("/diaries"))
      return answer(route, {
        schema_version: 1,
        fictional: true,
        actor_id: "actor-a",
        items: [
          {
            diary_id: "diary-a",
            actor_id: "actor-a",
            day: "2026-09-27",
            state: "draft",
            version: 4,
            published_revision_id: "revision-2",
            captured: {
              recipe_id: "daily",
              recipe_version: 1,
              material_version: "v1",
            },
          },
        ],
        next_after: null,
      });
    if (path.endsWith("/revision"))
      return answer(route, {
        schema_version: 1,
        fictional: true,
        actor_id: "actor-a",
        diary_id: "diary-a",
        diary_version: 4,
        revision_id: "revision-2",
        content: "这是一篇已发布的日记。",
        created_at: 1790470000,
        captured: {
          recipe_id: "daily",
          recipe_version: 1,
          material_version: "v1",
          config_version: 3,
        },
      });
    throw new Error(`unexpected ${path}`);
  });
  await page.goto("/#/companion/1");
  await expect(page.getByText("尚无持久化记录").first()).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "已发布日记", exact: true }),
  ).toBeVisible();
  await page.locator(".life-list button").click();
  await expect(page.getByText("这是一篇已发布的日记。")).toBeVisible();
  await expect(
    page.getByText("仅显示已发布指针指向的版本。", { exact: false }),
  ).toBeVisible();
});

test("memory separates overview, shared subjects and own records", async ({
  page,
}) => {
  await session(page);
  const bodyOfRecords: unknown[] = [];
  await page.route("**/api/web/memory/*", (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith("/state"))
      return answer(route, {
        available: true,
        code: "ready",
        peer: { configured: true, verified_at: null, code: "unverified" },
        actor_id: "actor-a",
      });
    if (path.endsWith("/overview"))
      return answer(route, {
        schema_version: 1,
        verified_at: "2026-09-27T00:00:00Z",
        scope_version: 2,
        memory_group_count: 4,
        subject_count: 1,
      });
    if (path.endsWith("/subjects"))
      return answer(route, {
        schema_version: 1,
        verified_at: "2026-09-27T00:00:00Z",
        scope_version: 2,
        items: [
          {
            subject: { kind: "person", person_id: "person-a" },
            categories: ["偏好"],
            group_count: 1,
          },
        ],
        next_cursor: null,
      });
    if (path.endsWith("/records")) {
      const body = route.request().postDataJSON();
      bodyOfRecords.push(body);
      return answer(route, {
        schema_version: 1,
        verified_at: "2026-09-27T00:00:00Z",
        scope_version: 2,
        items: [
          {
            semantic_group_id: "group-a",
            category: "偏好",
            field_key: "drink",
            item_key: "tea",
            units: [
              {
                record_id: "record-a",
                record_version: 1,
                statement: body.subject ? "共享的喜好" : "本人记得的事",
                conditions: [],
                negations: [],
                valid_time: null,
                uncertainty: null,
                reality: "real",
              },
            ],
          },
        ],
        next_cursor: null,
      });
    }
    throw new Error(`unexpected ${path}`);
  });
  await page.goto("/#/memory/0");
  await expect(page.getByText("4", { exact: true })).toBeVisible();
  await page.getByRole("link", { name: "查看人物与群" }).click();
  await expect(page.locator(".memory-list button")).toHaveCount(1);
  await page.locator(".memory-list button").click();
  await expect(page.getByText("共享的喜好")).toBeVisible();
  await page.getByRole("link", { name: "本人记忆", exact: true }).click();
  await expect(page.getByText("本人记得的事")).toBeVisible();
  expect(bodyOfRecords).toEqual(
    expect.arrayContaining([
      {
        subject: { kind: "person", person_id: "person-a" },
        limit: 20,
        cursor: null,
      },
      { subject: null, limit: 20, cursor: null },
    ]),
  );
});

test("missing deployment and forbidden reader are not shown as empty content", async ({
  page,
}) => {
  await session(page);
  await page.route("**/api/web/knowledge/state", (route) =>
    answer(route, {
      available: false,
      code: "knowledge_not_configured",
      projects: [],
      peer: { configured: false, verified_at: null, code: "unverified" },
    }),
  );
  await page.route("**/api/web/life/state", (route) =>
    answer(route, {
      available: false,
      code: "life_read_required",
      peer: { configured: true, verified_at: null, code: "unverified" },
    }),
  );
  await page.goto("/#/resources/0");
  await expect(
    page.getByRole("heading", { name: "项目知识尚未配置" }),
  ).toBeVisible();
  await page.goto("/#/companion/1");
  await expect(
    page.getByRole("heading", { name: "当前账号未获角色生活读取权限" }),
  ).toBeVisible();
  await expect(
    page.getByRole("heading", { name: "还没有已发布日记" }),
  ).toHaveCount(0);
});
