import { test, expect, type Page } from "@playwright/test";

// Presentation and retry fixture only. Actual owner/bytes/permissions are checked
// separately by runtime-joint.spec against the five independently served owners.
async function runtimeFixture(page: Page) {
  const writes: Record<string, any>[] = [];
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  const activities = [
    { id: "activity:a", title: "续读上册", next_due_at: 1791104400 },
    { id: "activity:b", title: "整理书架", next_due_at: null },
  ].map((row) => ({
    ...row,
    actor_id: "actor:fixture",
    version: 3,
    state: "planned",
    checkpoint: { note: "原进度", step: 1, position: 2, unit: "step" },
    resume_condition: null,
    sources: [],
    result_refs: [],
    scope: null,
    started_at: null,
    updated_at: null,
  }));
  await page.addInitScript(() =>
    Object.defineProperty(Crypto.prototype, "randomUUID", { value: undefined }),
  );
  await page.route("**/api/web/**", async (route) => {
    const path = new URL(route.request().url()).pathname.replace(
      "/api/web/",
      "",
    );
    const body = route.request().postDataJSON() ?? {};
    let value: unknown;
    if (path === "session")
      value = {
        authenticated: true,
        csrf: "runtime-fixture",
        username: "preview",
        conversations: [],
        dialogue: { available: false, code: "not_configured", model: "" },
      };
    else if (path === "life/state")
      value = {
        available: true,
        code: "ready",
        peer: { configured: true, code: "ok", verified_at: null },
      };
    else if (path === "life/actors")
      value = {
        schema_version: 1,
        fictional: true,
        items: [
          {
            actor_id: "actor:fixture",
            label: "橙汐",
            actor_version: 1,
            world_id: "world:fixture",
            room_id: "room:fixture",
          },
        ],
        next_after_actor_id: null,
      };
    else if (path === "life/snapshot")
      value = {
        schema_version: 1,
        fictional: true,
        actor_id: "actor:fixture",
        actor_version: 1,
        world_id: "world:fixture",
        room_id: "room:fixture",
        timezone: "Asia/Shanghai",
        activity: "正在阅读",
        mood: "calm",
        outfit_ref: null,
        changed_at: 1791100000,
        observed_at: 1791104400,
        state_basis: "last_persisted",
      };
    else if (path === "life/diaries")
      value = { items: [], next_after: null, fictional: true };
    else if (path === "life/runtime/read") {
      if (body.after)
        return route.fulfill({ status: 403, json: { code: "scope_changed" } });
      value = {
        schema_version: 2,
        request_id: "read:fixture",
        actor_id: body.actor_id,
        resource: body.resource,
        items:
          body.resource === "activities"
            ? activities
            : body.resource === "state"
              ? [
                  {
                    actor_id: body.actor_id,
                    activity: "正在阅读",
                    mood: "calm",
                    timezone: "Asia/Shanghai",
                    outfit_ref: null,
                    changed_at: 1791104400,
                  },
                ]
              : [],
        next_cursor: body.resource === "activities" ? "page:2" : null,
        fictional: true,
      };
    } else if (path === "life/runtime/manage") {
      writes.push(body);
      if (writes.length === 1)
        return route.fulfill({
          status: 503,
          json: { code: "dependency_unavailable", execution_state: "unknown" },
        });
      value = {
        schema_version: 2,
        request_id: body.client_id,
        actor_id: body.actor_id,
        operation: body.operation,
        result: {
          object_id: body.value.id,
          version: 1,
          state: "planned",
          replayed: true,
        },
      };
    } else
      return route.fulfill({ status: 503, json: { code: "not_configured" } });
    return route.fulfill({ json: value });
  });
  await page.goto("/#/companion/1");
  await expect(
    page.getByRole("heading", { name: "活动与进度", exact: true }),
  ).toBeVisible();
  return { writes, errors };
}

test("activity editing clears dates, uncertain retries reuse identity, and revoked pagination hides records", async ({
  page,
}) => {
  const fixture = await runtimeFixture(page);
  await expect(page.locator(".life-runtime-grid")).toContainText("心情：平静");
  const first = page.locator(".life-record").filter({ hasText: "续读上册" });
  const second = page.locator(".life-record").filter({ hasText: "整理书架" });
  await first.getByRole("button", { name: "编辑进度", exact: true }).click();
  await expect(page.getByLabel("下一步时间", { exact: true })).not.toHaveValue(
    "",
  );
  await second.getByRole("button", { name: "编辑进度", exact: true }).click();
  await expect(page.getByLabel("下一步时间", { exact: true })).toHaveValue("");
  await page.getByRole("button", { name: "取消编辑", exact: true }).click();
  await expect(page.getByLabel("活动名称", { exact: true })).toHaveValue("");
  await expect(page.getByLabel("当前进度", { exact: true })).toHaveValue("");
  await page.getByLabel("活动名称", { exact: true }).fill("晚上继续");
  await page
    .getByLabel("当前进度", { exact: true })
    .fill("保留原操作，查询后继续。");
  await page.getByRole("button", { name: "保存活动", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "写入结果尚未确认" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "保存活动", exact: true }).click();
  await expect(page.getByLabel("活动名称", { exact: true })).toHaveValue("");
  expect(fixture.writes).toHaveLength(2);
  expect(fixture.writes[1]).toEqual(fixture.writes[0]);
  expect(fixture.writes[1].value.next_due_at).toBeNull();
  await page.getByRole("button", { name: "继续读取活动", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "scope_changed" }),
  ).toBeVisible();
  await expect(first).toHaveCount(0);
  await expect(second).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "继续读取活动", exact: true }),
  ).toHaveCount(0);
  expect(fixture.errors).toEqual([]);
});

test("incremental replies retain sent segments after uncertain cancellation and hide revoked history", async ({
  page,
}) => {
  const fixture = await runtimeFixture(page);
  let phase = "generating",
    revoked = false,
    sends = 0;
  await page.route("**/api/web/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf: "runtime-fixture",
        username: "preview",
        conversations: [
          { id: "entry:fixture", label: "私人会话", actors: ["actor:fixture"] },
        ],
        dialogue: { available: true, code: "ready", model: "unverified" },
      },
    }),
  );
  await page.route("**/api/web/snapshot", (route) => {
    if (revoked)
      return route.fulfill({ status: 403, json: { code: "scope_changed" } });
    const view = {
      turn: {
        turn_id: "turn:fixture",
        turn_sequence: 1,
        version: 3,
        phase: phase === "unknown" ? "closed_unknown" : "generating",
        delivery_state: phase === "unknown" ? "unknown" : "partial",
        unresolved_delivery: phase === "unknown",
      },
      messages: [],
      replies: [
        {
          reply_id: "reply:1",
          segment_sequence: 1,
          segment_count: 2,
          state: "sent",
          content_state: "available",
          text: "已经实际投影的第一段",
        },
        {
          reply_id: "reply:2",
          segment_sequence: 2,
          segment_count: 2,
          state: phase === "unknown" ? "unknown" : "pending",
          content_state: "unavailable",
          text: null,
        },
      ],
    };
    return route.fulfill({
      json: {
        state: "current",
        submissions: [],
        snapshot: {
          observed_at: "2026-10-04T06:00:00Z",
          collectors: [],
          active_turns: phase === "generating" ? [view] : [],
          history: phase === "unknown" ? [view] : [],
          next_before_turn_sequence: null,
        },
      },
    });
  });
  await page.route("**/api/web/cancel", (route) => {
    expect(route.request().postDataJSON()).toMatchObject({
      turn_id: "turn:fixture",
      expected_version: 3,
      actor: "actor:fixture",
      conversation: "entry:fixture",
    });
    phase = "unknown";
    return route.fulfill({ json: { state: "unknown", version: 4 } });
  });
  await page.route("**/api/web/messages", (route) => {
    sends++;
    return route.fulfill({
      status: 500,
      json: { code: "dependency_unavailable" },
    });
  });
  await page.goto("/#/companion");
  await expect(
    page.getByText("已经实际投影的第一段", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "取消第 1 轮", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "第 1 轮 · 发送结果未知", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("已经实际投影的第一段", { exact: true }),
  ).toBeVisible();
  expect(sends).toBe(0);
  revoked = true;
  await page.getByRole("button", { name: "回到最新", exact: true }).click();
  await expect(
    page.getByRole("alert").filter({ hasText: "scope_changed" }),
  ).toBeVisible();
  await expect(
    page.getByText("已经实际投影的第一段", { exact: true }),
  ).toHaveCount(0);
  expect(sends).toBe(0);
  expect(fixture.errors).toEqual([]);
});

test("sampled media keeps coverage gaps visible and original images never imply completed generation", async ({
  page,
}) => {
  const fixture = await runtimeFixture(page);
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=",
    "base64",
  );
  const ref = {
    owner: "memory",
    object_id: "content:fixture",
    version: 1,
    sha256: "a".repeat(64),
    kind: "video",
    sources: [],
    coverage: { unit: "seconds", start: 0, end: 20, total: 20 },
  };
  await page.route("**/api/web/life/runtime/read", (route) => {
    const body = route.request().postDataJSON();
    let items: unknown[] = [];
    if (body.resource === "reading")
      items = [
        {
          id: "reading:fixture",
          version: 2,
          actor_id: body.actor_id,
          state: "reading",
          mode: "together",
          participants: [],
          scope: {},
          content_ref: ref,
          position: { unit: "seconds", start: 0, end: 5 },
          coverage: [{ unit: "seconds", start: 0, end: 5 }],
          updated_at: 1791104400,
        },
      ];
    else if (body.resource === "image_jobs")
      items = [
        {
          id: "image:fixture",
          version: 3,
          actor_id: body.actor_id,
          state: "unknown",
          error_code: null,
          created_at: 1791104400,
          completed_at: null,
          artifacts: [
            {
              media_id: "media:fixture",
              content_ref: {
                ...ref,
                kind: "image",
                coverage: { unit: "bytes", start: 0, end: 68, total: 68 },
              },
            },
          ],
        },
      ];
    return route.fulfill({
      json: {
        schema_version: 2,
        request_id: "read:fixture",
        actor_id: body.actor_id,
        resource: body.resource,
        items,
        next_cursor: null,
      },
    });
  });
  await page.route("**/api/web/life/runtime/content", (route) => {
    expect(route.request().postDataJSON().reading_id).toBe("reading:fixture");
    return route.fulfill({
      json: {
        schema_version: 2,
        request_id: "media-read",
        content_ref: ref,
        coverage: { unit: "seconds", start: 5, end: 10 },
        complete: false,
        text: null,
        gaps: ["frames_sampled", "audio_not_transcribed"],
        representations: [
          {
            kind: "video_frame",
            media_type: "image/png",
            sha256: "b".repeat(64),
            source_sha256: ref.sha256,
            data_base64: png.toString("base64"),
            at_seconds: 7.5,
            coverage: { unit: "seconds", start: 7.5, end: 7.5 },
          },
        ],
      },
    });
  });
  await page.route("**/api/web/life/runtime/media", (route) =>
    route.fulfill({ contentType: "image/png", body: png }),
  );
  await page.getByRole("tab", { name: "原文与共读", exact: true }).click();
  await page.getByRole("button", { name: "从断点继续", exact: true }).click();
  await page.getByRole("button", { name: "读取此范围", exact: true }).click();
  await expect(page.getByText(/本次范围有缺口/)).toBeVisible();
  await expect(page.getByText("画面为抽样帧", { exact: true })).toBeVisible();
  await expect(
    page.getByText("音轨尚未转成文字", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("img", { name: "实际画面 7.5 秒", exact: true }),
  ).toBeVisible();
  await page.getByRole("tab", { name: "衣柜与相册", exact: true }).click();
  const job = page
    .getByRole("heading", { name: "原任务", exact: true })
    .locator("..");
  await expect(job).toContainText("结果未知");
  await job.getByRole("button", { name: "打开原图", exact: true }).click();
  await expect(
    job.getByRole("img", { name: "本次创作原图", exact: true }),
  ).toBeVisible();
  await expect(
    job.getByRole("link", { name: "保存原图", exact: true }),
  ).toHaveAttribute("href", /^blob:/);
  expect(fixture.writes).toHaveLength(0);
  expect(fixture.errors).toEqual([]);
});

test("image preview reads its full coverage without exposing byte offsets and preserves owner refusal", async ({
  page,
}) => {
  const fixture = await runtimeFixture(page);
  const png = Buffer.from(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=",
    "base64",
  );
  const ref = {
    owner: "memory",
    object_id: "image:preview",
    version: 1,
    sha256: "6b1048f8a6d40bac0b2954c18fefa40c4ea7a96120fc2e54b7317c0e43c2bbec",
    kind: "image",
    sources: [],
    coverage: { unit: "bytes", start: 0, end: png.length, total: png.length },
  };
  let reads = 0,
    downloads = 0,
    refused = false;
  await page.route("**/api/web/life/runtime/read", (route) => {
    const body = route.request().postDataJSON();
    return route.fulfill({
      json: {
        schema_version: 2,
        request_id: "image-preview",
        actor_id: body.actor_id,
        resource: body.resource,
        items:
          body.resource === "image_reference"
            ? [
                {
                  id: "reference:preview",
                  version: 2,
                  actor_id: body.actor_id,
                  content_ref: ref,
                  scope: {},
                  state: "configured",
                },
              ]
            : [],
        next_cursor: null,
      },
    });
  });
  await page.route("**/api/web/life/runtime/content", (route) => {
    reads++;
    expect(route.request().postDataJSON()).toMatchObject({
      content_ref: ref,
      reading_id: null,
      range: { unit: "bytes", start: 0, end: png.length },
    });
    if (refused)
      return route.fulfill({ status: 403, json: { code: "forbidden" } });
    return route.fulfill({
      json: {
        schema_version: 2,
        request_id: "image-preview",
        content_ref: ref,
        coverage: { unit: "bytes", start: 0, end: png.length },
        complete: false,
        text: null,
        gaps: ["image_resized"],
        representations: [
          {
            kind: "image",
            media_type: "image/png",
            sha256: ref.sha256,
            source_sha256: ref.sha256,
            data_base64: png.toString("base64"),
            at_seconds: null,
            coverage: { unit: "bytes", start: 0, end: png.length },
          },
        ],
      },
    });
  });
  await page.route("**/api/web/content/original", (route) => {
    downloads++;
    expect(route.request().postDataJSON().value).toEqual({
      content_ref: ref,
      range: null,
    });
    return route.fulfill({ contentType: "image/png", body: png });
  });
  await page.getByRole("tab", { name: "衣柜与相册", exact: true }).click();
  const viewer = page
    .getByRole("heading", { name: "角色形象参考", exact: true })
    .locator("..")
    .locator(".life-content-reader");
  await expect(
    viewer.getByRole("button", { name: "查看图片", exact: true }),
  ).toBeVisible();
  await expect(viewer.locator('input[type="number"]')).toHaveCount(0);
  await expect(viewer).not.toContainText("字节");
  expect(reads).toBe(0);
  await viewer.getByRole("button", { name: "查看图片", exact: true }).click();
  await expect(
    viewer.getByRole("img", { name: "原件图片", exact: true }),
  ).toBeVisible();
  await expect(viewer).toContainText("已取得图片预览（预览有缺口）");
  await expect(viewer).toContainText("图片展示已缩放");
  await expect(viewer).not.toContainText("字节");
  const download = page.waitForEvent("download");
  await viewer.getByRole("button", { name: "下载原图", exact: true }).click();
  expect((await download).suggestedFilename()).toContain("原图-版本1");
  expect(downloads).toBe(1);
  refused = true;
  await viewer.getByRole("button", { name: "查看图片", exact: true }).click();
  await expect(viewer.getByRole("alert")).toContainText("forbidden");
  await expect(viewer.getByRole("img")).toHaveCount(0);
  await expect(viewer.getByText(/已取得图片预览/)).toHaveCount(0);
  expect(reads).toBe(2);
  expect(fixture.errors).toEqual([]);
});
