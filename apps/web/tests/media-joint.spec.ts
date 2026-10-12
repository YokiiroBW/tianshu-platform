/** Actual platform HTTP and browser; recorded Bili and publication upstreams are synthetic. */
import {
  test,
  expect,
  type Page,
  type APIRequestContext,
} from "@playwright/test";
import { randomUUID } from "node:crypto";
import { selectMediaSection, expectMediaSection } from "./media.fixtures";
import type { MediaView } from "../src/features/media/types";

const BV = "BV1xx411c7mD";
const OTHER = "BV1Ab4y1z7Qs";
const controlUrl = process.env.MEDIA_FIXTURE_CONTROL;
const localServer = process.env.MEDIA_FIXTURE_LOCAL_SERVER === "1";
const finishedLabel = localServer ? "核对完成" : "发布完成";
async function control(request: APIRequestContext, body: object) {
  expect(
    controlUrl,
    "set MEDIA_FIXTURE_CONTROL to the isolated upstream fixture control URL",
  ).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/__fixture\/control$/);
  expect((await request.post(controlUrl!, { data: body })).ok()).toBe(true);
}
async function login(page: Page) {
  await page.goto("/#/subscriptions/1");
  await page.getByLabel("管理员账号").fill("synthetic-admin");
  await page
    .getByLabel("密码", { exact: true })
    .fill("synthetic-local-password-014");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expectMediaSection(page, "链接下载");
}
async function parse(page: Page, bvid: string, account?: string) {
  await selectMediaSection(page, "链接下载");
  if (account)
    await page
      .getByLabel("B 站账号")
      .selectOption({ label: `${account} · 可用` });
  await page
    .getByLabel("B 站视频链接")
    .fill(`https://www.bilibili.com/video/${bvid}`);
  await page.getByRole("button", { name: "解析链接", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: `合成投稿 ${bvid}`, exact: true }),
  ).toBeVisible();
  await page.getByLabel("目标媒体库").selectOption("synthetic-network-video");
}
async function waitForState(page: Page, state: string, jobId: string) {
  await expect
    .poll(
      async () => {
        await page
          .getByRole("button", { name: "读取状态", exact: true })
          .click();
        return page
          .locator(`article.media-job[data-job-id="${jobId}"] .status-rail`)
          .allTextContents();
      },
      { timeout: 30_000, intervals: [300, 500, 1000] },
    )
    .toContain(state);
}

test.beforeEach(async ({ request }) => {
  await control(request, {
    members: [BV],
    parts: { [BV]: ["111111111", "222222222"], [OTHER]: ["333333333"] },
    description: "完整合成简介",
    cover_enabled: true,
    qr_state: "waiting",
  });
});

test("actual media HTTP supports QR transitions, all source URLs, new members and full publication", async ({
  page,
  request,
}, info) => {
  await login(page);
  const account = `隔离扫码 ${randomUUID().slice(0, 8)}`;
  const newMember = `BV1${randomUUID().replaceAll("-", "").slice(0, 9)}`;
  await selectMediaSection(page, "账号与媒体库");
  await page.getByLabel("账号显示名称").fill(account);
  await page
    .getByRole("button", { name: "生成登录二维码", exact: true })
    .click();
  await expect(page.getByAltText("B 站登录二维码")).toHaveAttribute(
    "src",
    /^data:image\/gif;base64,/,
  );
  await expect(page.getByText("等待扫码", { exact: true })).toBeVisible();
  await control(request, { qr_state: "scanned" });
  await expect(
    page.getByText("已扫码，请在手机确认", { exact: true }),
  ).toBeVisible({ timeout: 10_000 });
  await control(request, { qr_state: "ready" });
  await expect(
    page.getByRole("heading", { name: account, exact: true }),
  ).toBeVisible({ timeout: 10_000 });
  await page
    .getByRole("button", { name: "校验会话", exact: true })
    .last()
    .click();
  await expect(
    page.getByText("已重新校验 B 站会话，请查看账号最新状态。"),
  ).toBeVisible();

  await parse(page, BV, account);
  await page.getByLabel("质量策略").selectOption("80");
  const created = page.waitForResponse((response) =>
    response.url().endsWith("/api/web/media/enqueue"),
  );
  await page
    .getByRole("button", { name: "创建下载任务（2 个分 P）", exact: true })
    .click();
  const createdJob = (await (await created).json()).jobs[0];
  await waitForState(page, finishedLabel, createdJob.job_id);
  await page
    .locator(`article.media-job[data-job-id="${createdJob.job_id}"]`)
    .getByRole("button", { name: "查看任务详情", exact: true })
    .click();
  const detail = page.getByRole("complementary", { name: "任务详情" });
  await expect(detail.getByText("1080P", { exact: true })).toBeVisible();
  if (localServer) {
    await expect(detail.getByText("已核对", { exact: true })).toBeVisible();
  } else {
    await expect(
      detail.getByText(
        "目标库未配置媒体服务器。发布完成不代表服务器核对完成。",
      ),
    ).toBeVisible();
  }
  await expect(detail.getByText("尚无已确认的资产发布回执。")).toHaveCount(0);
  await page.screenshot({
    path: info.outputPath(`media-http-${info.project.name}.png`),
    fullPage: true,
  });
  await page.getByRole("button", { name: "返回任务列表", exact: true }).click();
  await page.goto("/#/settings/0");
  await expect(
    page.locator("article.tasks-item").filter({ hasText: BV }).first(),
  ).toContainText(localServer ? "核对完成" : "发布完成（未配置媒体服务器）");
  await page.goto("/#/subscriptions/1");
  await selectMediaSection(page, "视频订阅");
  const sources = {
    favorite: "https://space.bilibili.com/946974/favlist?fid=123",
    collection: "https://space.bilibili.com/946974/lists/456?type=season",
    series: "https://space.bilibili.com/946974/lists/789?type=series",
    uploader: "https://space.bilibili.com/946974/video",
  };
  const labels: Record<string, string> = {};
  for (const [kind, url] of Object.entries(sources)) {
    const label = (labels[kind] =
      `${info.project.name} ${kind} ${randomUUID().slice(0, 6)}`);
    await page.getByRole("button", { name: "新建订阅", exact: true }).click();
    await page.getByLabel("订阅名称").fill(label);
    await page
      .getByLabel("B 站账号")
      .selectOption({ label: `${account} · 可用` });
    await page.getByLabel("来源类型").selectOption(kind);
    await page.getByLabel("来源链接或 ID", { exact: true }).fill(url);
    await page.getByLabel("扫描间隔（分钟）").fill("60");
    if (kind === "collection")
      await page.getByText("导入来源中的历史视频", { exact: true }).click();
    await page.getByRole("button", { name: "下一步", exact: true }).click();
    if (kind === "favorite") {
      await page
        .getByRole("button", { name: "添加白名单组", exact: true })
        .click();
      await page.getByLabel("匹配值", { exact: true }).fill("合成投稿");
      await page
        .getByLabel("试算视频链接")
        .fill(`https://www.bilibili.com/video/${BV}`);
      await page.getByRole("button", { name: "试算规则", exact: true }).click();
      await expect(page.getByText("符合规则，可以下载").first()).toBeVisible();
      await page.getByText("查看逐条规则解释", { exact: true }).first().click();
      await expect(
        page.getByText("白名单组 1", { exact: true }).first(),
      ).toBeVisible();
    }
    await page.getByRole("button", { name: "下一步", exact: true }).click();
    await page.getByLabel("目标媒体库").selectOption("synthetic-network-video");
    if (localServer)
      await page
        .getByLabel("整理子目录（可选）")
        .fill(`joint/${info.project.name}/${kind}/${randomUUID().slice(0, 6)}`);
    await page.getByRole("button", { name: "保存订阅", exact: true }).click();
    const card = page
      .locator("article.media-card")
      .filter({ has: page.getByRole("heading", { name: label, exact: true }) });
    await expect(card).toBeVisible();
    await card.getByRole("button", { name: "立即扫描", exact: true }).click();
    await expect(card).toContainText("完整 · 1 个成员");
  }
  await control(request, {
    members: [BV, newMember],
    parts: { [newMember]: ["666666666"] },
  });
  const favorite = page.locator("article.media-card").filter({
    has: page.getByRole("heading", { name: labels.favorite, exact: true }),
  });
  await favorite.getByRole("button", { name: "立即扫描", exact: true }).click();
  await expect(favorite).toContainText("完整 · 2 个成员");
  await expect(favorite).toContainText("1 个任务");
  await favorite.getByRole("button", { name: "暂停订阅", exact: true }).click();
  await expect(
    favorite.getByRole("button", { name: "恢复订阅", exact: true }),
  ).toBeVisible();
  await selectMediaSection(page, "下载中心");
  await expect(
    page
      .getByRole("heading", { name: `合成投稿 ${newMember}`, exact: true })
      .first(),
  ).toBeVisible();
  await page.getByLabel("筛选任务").selectOption("finished");
  await expect(
    page.locator("article.media-job").filter({ hasText: newMember }).first(),
  ).toContainText(finishedLabel, { timeout: 30_000 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
});

test("actual media HTTP restores a waiting metadata job with an explicit override", async ({
  page,
  request,
}, info) => {
  const metadataBV =
    info.project.name === "desktop" ? "BV1Q541167Qg" : "BV1fK4y1t7NK";
  await control(request, {
    description: "",
    parts: { [metadataBV]: ["555555555"] },
  });
  await login(page);
  await parse(page, metadataBV);
  const created = page.waitForResponse((response) =>
    response.url().endsWith("/api/web/media/enqueue"),
  );
  await page
    .getByRole("button", { name: "创建下载任务（1 个分 P）", exact: true })
    .click();
  const createdJob = (await (await created).json()).jobs[0];
  await waitForState(page, "等待补全元数据", createdJob.job_id);
  const card = page.locator(
    `article.media-job[data-job-id="${createdJob.job_id}"]`,
  );
  await card.getByRole("button", { name: "查看任务详情", exact: true }).click();
  await expect(page.getByLabel("展示简介")).toHaveValue("");
  await page.getByLabel("展示简介").fill("浏览器明确补充的合成简介");
  await page.getByRole("button", { name: "保存元数据", exact: true }).click();
  await expect(
    page.getByText(
      "元数据已保存，任务将从对应阶段继续。源视频的原始资料会保留。",
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "返回任务列表", exact: true }).click();
  await waitForState(page, finishedLabel, createdJob.job_id);
});

test("actual overview HTTP displays server totals and media storage disk facts", async ({
  page,
}, info) => {
  await login(page);
  await selectMediaSection(page, "概览");
  await expectMediaSection(page, "概览");
  const refresh = page.getByRole("button", { name: "读取状态", exact: true });
  await expect(refresh).toBeEnabled();
  const reading = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/web/media/view") &&
      response.request().method() === "POST",
  );
  await refresh.click();
  const response = await reading;
  expect(response.ok()).toBe(true);
  let view = (await response.json()) as MediaView;
  if (localServer) {
    await expect
      .poll(
        async () => {
          const updated = page.waitForResponse((result) =>
            result.url().endsWith("/api/web/media/view"),
          );
          await refresh.click();
          view = (await (await updated).json()) as MediaView;
          return (
            !!view.overview?.storage.volumes.length &&
            view.overview.storage.volumes.every(
              (volume) => volume.breakdown?.state === "ready",
            )
          );
        },
        { timeout: 15_000, intervals: [200, 500, 1000] },
      )
      .toBe(true);
  }
  expect(view.configured).toBe(true);
  expect(view.overview).toBeTruthy();
  const { jobs, storage } = view.overview!;
  expect(jobs.total).toBeGreaterThanOrEqual(view.jobs.length);
  await expect(page.locator(".overview-kpi-value")).toHaveText(
    [jobs.published, jobs.processing, jobs.queued, jobs.attention].map(
      (count) => count.toLocaleString("zh-CN"),
    ),
  );
  await expect(page.locator(".overview-total")).toHaveText(
    `累计 ${jobs.total.toLocaleString("zh-CN")} 个任务 · 其中 ${jobs.cancelled.toLocaleString("zh-CN")} 个已取消`,
  );
  await expect(page.locator(".overview-recent-row")).toHaveCount(
    Math.min(5, view.jobs.length),
  );
  const disk = page.getByRole("region", { name: "存储磁盘", exact: true });
  await expect(disk).toContainText(
    "媒体目录所在的存储磁盘，容量包含同盘其他文件。同盘目录合并显示。",
  );
  expect(Array.isArray(storage.volumes)).toBe(true);
  if (localServer) {
    expect(storage.state).toBe("available");
    expect(storage.volumes.length).toBeGreaterThan(0);
  }
  await expect(disk.locator(".overview-storage-volume")).toHaveCount(
    storage.volumes.length,
  );
  for (const volume of storage.volumes) {
    const displayed = disk.getByRole("region", {
      name: volume.label,
      exact: true,
    });
    for (const directory of volume.directories)
      await expect(displayed).toContainText(directory);
    if (volume.state !== "available") {
      expect([
        volume.total_bytes,
        volume.used_bytes,
        volume.free_bytes,
      ]).toEqual([null, null, null]);
      await expect(displayed).toContainText("暂未取得磁盘容量");
      await expect(displayed.getByRole("progressbar")).toHaveCount(0);
      continue;
    }
    for (const value of [
      volume.total_bytes,
      volume.used_bytes,
      volume.free_bytes,
    ]) {
      expect(typeof value).toBe("number");
      expect(value).toBeGreaterThanOrEqual(0);
    }
    const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    const unit =
      volume.total_bytes! > 0
        ? Math.min(
            Math.floor(Math.log(volume.total_bytes!) / Math.log(1024)),
            5,
          )
        : 0;
    await expect(
      displayed.locator(".overview-storage-capacity strong"),
    ).toHaveText(
      `${(volume.total_bytes! / 1024 ** unit).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${units[unit]}`,
    );
    if (localServer) {
      const breakdown = volume.breakdown!;
      const values = [
        breakdown.subscription_bytes!,
        breakdown.emby_bytes!,
        breakdown.other_bytes!,
        breakdown.unused_bytes!,
      ];
      expect(values.reduce((total, value) => total + value, 0)).toBe(
        volume.total_bytes,
      );
      expect(breakdown.unused_bytes).toBe(volume.free_bytes);
      await expect(displayed.getByLabel("四类容量").locator("dt")).toHaveText([
        "订阅占用",
        "Emby占用",
        "其他占用",
        "未使用",
      ]);
      const formatted = values.map((value) => {
        const index =
          value > 0
            ? Math.min(Math.floor(Math.log(value) / Math.log(1024)), 5)
            : 0;
        return `${(value / 1024 ** index).toLocaleString("zh-CN", { maximumFractionDigits: 1 })} ${units[index]}`;
      });
      await expect(displayed.getByLabel("四类容量").locator("dd")).toHaveText(
        formatted,
      );
      await expect(displayed.locator(".overview-storage-segment")).toHaveCount(
        values.filter((value) => value > 0).length,
      );
      await expect(displayed).toContainText("统计时间：");
      if (breakdown.basis === "logical")
        await expect(displayed).toContainText("按文件逻辑大小估算");
    }
  }
  if (!storage.volumes.length) {
    await expect(disk).toContainText(
      storage.state === "not_configured"
        ? "尚未配置媒体存储目录"
        : "暂未取得磁盘容量",
    );
    await expect(disk.getByRole("progressbar")).toHaveCount(0);
  }
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath(`overview-http-${info.project.name}.png`),
    fullPage: true,
  });
});

test("actual media HTTP deletes a waiting record with revision checks counters and replay", async ({
  page,
  request,
}, info) => {
  const bvid = `BV1${randomUUID().replaceAll("-", "").slice(0, 9)}`;
  await control(request, { description: "", parts: { [bvid]: ["777777777"] } });
  await login(page);
  await parse(page, bvid);
  const created = page.waitForResponse((response) =>
    response.url().endsWith("/api/web/media/enqueue"),
  );
  await page
    .getByRole("button", { name: "创建下载任务（1 个分 P）", exact: true })
    .click();
  const job = (await (await created).json()).jobs[0];
  await waitForState(page, "等待补全元数据", job.job_id);
  const card = page.locator(`article.media-job[data-job-id="${job.job_id}"]`);
  await card.getByRole("button", { name: "查看任务详情", exact: true }).click();
  const detail = page.getByRole("complementary", { name: "任务详情" });
  await expect(
    detail.getByRole("button", { name: "删除记录", exact: true }),
  ).toBeVisible();
  const session = await (await page.request.get("/api/web/session")).json();
  const headers = {
    "X-CSRF-Token": session.csrf,
    Origin: new URL(page.url()).origin,
  };
  const before = await (
    await page.request.post("/api/web/media/view", { headers, data: {} })
  ).json();
  const conflict = await page.request.post("/api/web/media/jobs/delete", {
    headers,
    data: { job_id: job.job_id, expected_revision: 1, client_id: randomUUID() },
  });
  expect(conflict.status()).toBe(409);
  await expect(card).toHaveCount(1);
  await expect(detail).toBeVisible();
  const deleted = page.waitForResponse(
    (response) =>
      response.url().endsWith("/api/web/media/jobs/delete") &&
      response.request().method() === "POST",
  );
  await detail.getByRole("button", { name: "删除记录", exact: true }).click();
  const response = await deleted;
  expect(response.ok()).toBe(true);
  expect(await response.json()).toMatchObject({
    deleted: true,
    job_id: job.job_id,
  });
  await expect(detail).toHaveCount(0);
  await expect(card).toHaveCount(0);
  const replay = await page.request.post("/api/web/media/jobs/delete", {
    headers,
    data: response.request().postDataJSON(),
  });
  expect(replay.ok()).toBe(true);
  expect(await replay.json()).toMatchObject({
    deleted: true,
    job_id: job.job_id,
    replayed: true,
  });
  const missing = await page.request.post("/api/web/media/jobs/detail", {
    headers,
    data: { job_id: job.job_id },
  });
  expect(missing.status()).toBe(404);
  const after = await (
    await page.request.post("/api/web/media/view", { headers, data: {} })
  ).json();
  expect(after.overview.jobs.total).toBe(before.overview.jobs.total - 1);
  expect(after.overview.jobs.attention).toBe(
    before.overview.jobs.attention - 1,
  );
  expect(
    after.jobs.some((item: { job_id: string }) => item.job_id === job.job_id),
  ).toBe(false);
  await page.screenshot({
    path: info.outputPath(`records-http-deleted-${info.project.name}.png`),
    fullPage: true,
  });
});
