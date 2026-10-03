import { test, expect, type Page } from "@playwright/test";

// Synthetic persisted projections: these tests exercise presentation, not the
// scheduler or model. In particular no weather is invented for absent data.
const day = "2026-10-03";
const now = new Date(`${day}T11:35:00Z`);
const at = now.getTime() / 1000;
const fullEvening =
  "晚饭后把碗碟收好，回到书桌旁，打开收藏夹里积攒的短视频。看见一段猫咪在窗边打盹的画面，不自觉地笑起来。中途起身给杯子续了热茶，又翻了几页白天没有读完的书。窗外的路灯慢慢亮起，房间安静下来，暂时没有特别想做的事，只想让这一小段时间过得缓慢一些。最后把喜欢的句子记在便签上，准备明天继续读。";
const futureDetail =
  "准备沿小屋附近走一圈，回家后整理书桌。这是稍后的安排，还没有发生。";

async function lifeFixture(page: Page, configureWeather?: () => Promise<void>) {
  await page.clock.setFixedTime(now);
  const requests: Record<string, unknown>[] = [];
  let todayReads = 0;
  const errors: string[] = [];
  page.on("pageerror", (cause) => errors.push(cause.message));
  await page.route("**/api/web/weather/current", (route) =>
    route.fulfill({
      json: {
        revision: 0,
        can_manage: true,
        configured: false,
        credential_configured: false,
        host: null,
        location: null,
        server_time: at,
        weather: null,
        fetched_at: null,
        stale: false,
        code: "weather_not_configured",
      },
    }),
  );
  await page.route("**/api/web/session", (route) =>
    route.fulfill({
      json: {
        authenticated: true,
        csrf: "life-visual-fixture",
        username: "YokiiroBW",
        conversations: [],
        dialogue: { available: false, code: "not_configured", model: "" },
      },
    }),
  );
  await page.route("**/api/web/life/*", (route) => {
    const name = route.request().url().split("/").at(-1);
    const body = route.request().postDataJSON();
    const actor = body.actor_id ?? "actor:chengxi";
    const common = { schema_version: 1, fictional: true, actor_id: actor };
    let result: unknown;
    if (name === "state")
      result = {
        available: true,
        code: "ready",
        can_retry: false,
        peer: { configured: true, code: "ok", verified_at: null },
      };
    else if (name === "actors")
      result = {
        schema_version: 1,
        fictional: true,
        items: [
          {
            actor_id: "actor:chengxi",
            label: "橙汐",
            actor_version: 1,
            world_id: "world",
            room_id: "room",
          },
          {
            actor_id: "actor:xuese",
            label: "雪色",
            actor_version: 1,
            world_id: "world",
            room_id: "room",
          },
        ],
        next_after_actor_id: null,
      };
    else if (name === "snapshot")
      result = {
        ...common,
        actor_version: 1,
        world_id: "world",
        room_id: "room",
        timezone: "Asia/Shanghai",
        activity: "晚间放松",
        mood: "放松 · 微微愉悦",
        outfit_ref: null,
        changed_at: at - 2100,
        observed_at: at,
        state_basis: "last_persisted",
      };
    else if (name === "today") {
      todayReads++;
      result = {
        ...common,
        day,
        timezone: "Asia/Shanghai",
        enabled: true,
        observed_at: at,
        state_basis: "last_persisted",
        plan: {
          plan_id: `plan:${actor}`,
          version: 3,
          state: "active",
          generated_by: "gateway",
          generation_state: "completed",
          current_phase_id: "phase:5",
          entries: [
            [450, "晨间整理", "拉开窗帘，整理房间，给自己准备一份简单的早餐。"],
            [
              540,
              "阅读与记录",
              "在书桌旁读了几章书，把感兴趣的段落摘在笔记里。",
            ],
            [750, "午餐与休息", "做了一份清淡的午餐，收拾好厨房后稍作休息。"],
            [
              900,
              "窗边小憩",
              "在窗边坐了一会儿，听着远处的声音，慢慢喝完一杯茶。",
            ],
            [1050, "准备晚餐", "把冰箱里的蔬菜洗净，煮了一碗热汤面。"],
            [
              1140,
              actor === "actor:chengxi" ? "晚间放松" : "雪色的夜间阅读",
              actor === "actor:chengxi"
                ? fullEvening
                : "雪色翻开旅行笔记，重新整理上周记录的片段。",
            ],
            [1230, "夜间散步", futureDetail],
          ].map(([minute, activity, detail], index) => ({
            phase_id: `phase:${index}`,
            minute,
            activity,
            detail,
            state: index === 5 ? "current" : index < 5 ? "elapsed" : "planned",
            generation_state: index > 5 ? "queued" : "completed",
          })),
        },
      };
    } else if (name === "timeline") {
      requests.push(body);
      result = {
        ...common,
        day: body.day,
        state_basis: "last_persisted",
        items: [
          {
            event_id: `event:${body.after ? "early" : "evening"}`,
            known_id: `${actor}:${body.after ? "early" : "evening"}`,
            position: body.after ? at - 3600 : at,
            kind: "activity",
            summary: body.after
              ? "清晨在厨房准备了早餐，慢慢开始新的一天。"
              : actor === "actor:chengxi"
                ? fullEvening
                : "雪色翻开旅行笔记，重新整理上周记录的片段。",
            occurred_at: body.after ? at - 3600 : at,
            learned_at: at,
            via: "self",
            phase_id: body.after ? "phase:0" : "phase:5",
            plan_id: `plan:${actor}`,
            generated_by: "gateway",
          },
        ],
        next_after: body.after
          ? null
          : { position: at, known_id: `${actor}:evening` },
      };
    } else if (name === "diaries")
      result = { ...common, items: [], next_after: null };
    else throw new Error(`Unexpected life fixture request: ${name}`);
    return route.fulfill({ json: result });
  });
  await configureWeather?.();
  await page.goto("/#/companion/1");
  await expect(page.locator(".life-plan > li")).toHaveCount(7);
  return { requests, errors, todayReads: () => todayReads };
}

test("life overview and timeline remain readable on desktop and touch screens", async ({
  page,
}, testInfo) => {
  const { errors } = await lifeFixture(page);
  await expect(page.getByText("放松 · 微微愉悦").first()).toBeVisible();
  await expect(page.getByText("天气未连接", { exact: true })).toBeVisible();
  const future = page
    .locator(".life-plan > li")
    .filter({ hasText: "夜间散步" });
  await expect(future).toContainText(/尚未发生|待发生/);
  await expect(page.locator(".life-timeline")).not.toContainText("夜间散步");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("life-overview.png"),
    fullPage: true,
  });
  expect(errors).toEqual([]);
});

test("completed daily plans refresh in the background without unmounting weather settings", async ({
  page,
}) => {
  await page.clock.install({ time: now });
  const { todayReads, errors } = await lifeFixture(page);
  await page.getByRole("button", { name: "设置天气", exact: true }).click();
  const settings = page.getByRole("dialog", { name: "位置与天气" });
  await settings
    .getByLabel("和风天气 API Host")
    .fill("fixture.qweatherapi.com");
  await settings
    .getByLabel("API Key", { exact: true })
    .fill("unsaved-key-during-background-refresh");
  const before = todayReads();
  await page.clock.runFor(31_000);
  await expect.poll(todayReads).toBeGreaterThan(before);
  await expect(settings).toBeVisible();
  await expect(settings.getByLabel("和风天气 API Host")).toHaveValue(
    "fixture.qweatherapi.com",
  );
  await expect(settings.getByLabel("API Key", { exact: true })).toHaveValue(
    "unsaved-key-during-background-refresh",
  );
  expect(errors).toEqual([]);
});

test("QWeather setup keeps credentials private, respects location time and marks stale readings", async ({
  page,
}, testInfo) => {
  const location = {
    id: "1E98F",
    name: "纽约",
    adm1: "纽约州",
    adm2: "纽约",
    country: "美国",
    tz: "America/New_York",
    utc_offset: "-04:00",
    lat: "40.71",
    lon: "-74.01",
  };
  const calls: { name: string; body: Record<string, unknown> }[] = [];
  let revision = 0;
  let selected = false;
  let failCurrent = false;
  const view = (actor: unknown, withWeather = false) => ({
    revision,
    can_manage: true,
    configured: revision > 0,
    credential_configured: revision > 0,
    host: revision > 0 ? "fixture.qweatherapi.com" : null,
    location: selected && actor === "actor:chengxi" ? location : null,
    server_time: at,
    code:
      revision === 0
        ? "weather_not_configured"
        : selected
          ? "ok"
          : "weather_location_required",
    weather:
      withWeather && selected && actor === "actor:chengxi"
        ? {
            temp: "22",
            feels_like: "20",
            text: "多云",
            icon: "101",
            wind_scale: "2",
            observed_at: null,
            attributions: ["QWeather"],
          }
        : null,
    fetched_at: withWeather && selected ? now.toISOString() : null,
    stale: false,
  });
  const { errors } = await lifeFixture(page, async () => {
    await page.route("**/api/web/weather/*", (route) => {
      const name = route.request().url().split("/").at(-1)!;
      const body = route.request().postDataJSON();
      calls.push({ name, body });
      if (
        name === "current" &&
        failCurrent &&
        body.actor_id === "actor:chengxi"
      )
        return route.fulfill({
          status: 503,
          json: { code: "weather_upstream_unavailable" },
        });
      if (name === "configure") revision = 1;
      if (name === "location") {
        revision = 2;
        selected = true;
      }
      if (name === "locations")
        return route.fulfill({ json: { items: [location] } });
      return route.fulfill({ json: view(body.actor_id, name === "current") });
    });
  });
  await page.getByRole("button", { name: "设置天气", exact: true }).click();
  const settings = page.getByRole("dialog", { name: "位置与天气" });
  await settings
    .getByLabel("和风天气 API Host")
    .fill("fixture.qweatherapi.com");
  await settings
    .getByLabel("API Key", { exact: true })
    .fill("synthetic-key-only-for-ui-test");
  await settings.getByRole("button", { name: "保存连接" }).click();
  await expect(
    settings.getByText("连接配置已保存。选择位置后会读取实际天气。"),
  ).toBeVisible();
  const configured = calls.filter((call) => call.name === "configure");
  expect(configured).toHaveLength(1);
  expect(configured[0].body).toMatchObject({
    actor_id: "actor:chengxi",
    host: "fixture.qweatherapi.com",
    expected_revision: 0,
    client_id: expect.any(String),
    credential: { action: "replace", value: "synthetic-key-only-for-ui-test" },
  });
  await expect(settings.getByLabel("API Key", { exact: true })).toHaveValue("");
  await settings.getByLabel("搜索城市或区县").fill("纽约");
  await settings.getByRole("button", { name: "搜索位置" }).click();
  await settings
    .getByRole("button", { name: /纽约.*美国.*America\/New_York/ })
    .click();
  await expect(settings).not.toBeVisible();
  expect(calls.find((call) => call.name === "locations")?.body).toEqual({
    actor_id: "actor:chengxi",
    query: "纽约",
  });
  expect(calls.find((call) => call.name === "location")?.body).toMatchObject({
    actor_id: "actor:chengxi",
    location_id: location.id,
    expected_revision: 1,
    client_id: expect.any(String),
  });
  const weather = page.locator(".life-weather");
  await expect(weather.locator(".life-weather-reading")).toContainText("22°C");
  await expect(weather).toContainText("体感 20°C · 风力 2 级");
  await expect(weather).toContainText("多云");
  await expect(weather.locator(".life-weather-place")).toContainText(
    "10/03 07:35",
  );
  await expect(page.locator(".life-clock > strong")).toHaveText("07:35");
  await expect(page.locator(".life-clock-caption")).toHaveText(
    "America/New_York",
  );
  await expect(page.locator(".life-schedule-heading")).toContainText(
    "日程时区 Asia/Shanghai",
  );
  await expect(
    page.locator('.life-plan > li[data-phase="current"] time'),
  ).toHaveText("19:00");
  await page.screenshot({
    path: testInfo.outputPath("life-weather-configured.png"),
    fullPage: true,
  });

  // Closing an unsaved replacement must clear it; persisted keys are never read back.
  await page.getByRole("button", { name: "位置与天气", exact: true }).click();
  await settings
    .getByLabel("API Key", { exact: true })
    .fill("discard-this-unsaved-key");
  await settings.getByRole("button", { name: "关闭天气设置" }).click();
  await page.getByRole("button", { name: "位置与天气", exact: true }).click();
  await expect(settings.getByLabel("API Key", { exact: true })).toHaveValue("");
  await settings.getByRole("button", { name: "关闭天气设置" }).click();
  expect(calls.filter((call) => call.name === "configure")).toHaveLength(1);

  failCurrent = true;
  // Native dialog.close fires its close event asynchronously. Allow the React
  // effect to resume background reads before simulating a later tab resume.
  await expect(settings).not.toBeVisible();
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  await page.evaluate(() =>
    document.dispatchEvent(new Event("visibilitychange")),
  );
  await expect(weather.locator(".life-weather-place")).toContainText(
    "上次读数",
  );
  await expect(weather.locator(".life-weather-reading")).toContainText("22°C");
  await expect(weather.getByRole("status")).toContainText(
    "天气服务暂时无法连接",
  );
  await page.getByLabel("选择角色").selectOption("actor:xuese");
  await expect(weather).toContainText("选择天气位置");
  await expect(weather).not.toContainText("22°C");
  await expect(page.locator(".life-clock > strong")).toHaveText("19:35");
  await expect(page.locator(".life-clock-caption")).toHaveText("Asia/Shanghai");
  expect(errors).toEqual([]);
});

test("compact phase opens full source text by keyboard or touch and restores focus", async ({
  page,
  isMobile,
}, testInfo) => {
  const { errors } = await lifeFixture(page);
  const current = page
    .locator(".life-plan > li")
    .filter({ hasText: "晚间放松" });
  const trigger = current.getByRole("button");
  if (!isMobile) {
    await trigger.hover();
    await expect(current.getByRole("tooltip")).toBeVisible();
    await page.mouse.move(0, 0);
    await trigger.focus();
    await expect(current.getByRole("tooltip")).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(current.getByRole("tooltip")).not.toBeVisible();
    await page.keyboard.press("Enter");
  } else await trigger.tap();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await expect(dialog.locator(".life-dialog-body")).toHaveText(fullEvening);
  expect(
    await dialog.evaluate((node) => node.scrollWidth <= node.clientWidth),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("life-detail.png"),
    fullPage: true,
  });
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
  expect(errors).toEqual([]);
});

test("history pagination stays available and switching role does not retain old details", async ({
  page,
}) => {
  const { requests, errors } = await lifeFixture(page);
  await page.getByRole("button", { name: "继续读取经历" }).click();
  await expect(page.locator(".life-timeline")).toContainText(
    "清晨在厨房准备了早餐",
  );
  expect(requests.at(-1)).toMatchObject({
    actor_id: "actor:chengxi",
    day,
    after: { position: at, known_id: "actor:chengxi:evening" },
  });
  await page
    .locator(".life-plan > li")
    .filter({ hasText: "晚间放松" })
    .getByRole("button")
    .click();
  await expect(page.getByRole("dialog")).toBeVisible();
  // A modal intentionally makes the role picker inert. Dispatch a role update to
  // exercise the same unmount path as an external change while details are open.
  await page.getByLabel("选择角色").evaluate((node) => {
    (node as HTMLSelectElement).value = "actor:xuese";
    node.dispatchEvent(new Event("change", { bubbles: true }));
  });
  await expect(page.getByRole("dialog")).not.toBeVisible();
  const next = page
    .locator(".life-plan > li")
    .filter({ hasText: "雪色的夜间阅读" });
  await expect(next).toBeVisible();
  await next.getByRole("button").click();
  await expect(page.getByRole("dialog")).toContainText("雪色翻开旅行笔记");
  await expect(page.getByRole("dialog")).not.toContainText(fullEvening);
  expect(errors).toEqual([]);
});
