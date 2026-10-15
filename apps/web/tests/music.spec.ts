import { test, expect, type Page } from "@playwright/test";
import { fixtureJob, fixtureView, installMediaFixture } from "./media.fixtures";
import type {
  MusicView,
  MusicPlaylist,
} from "../src/features/media/music/types";

async function musicFixture(page: Page, connected = true) {
  await installMediaFixture(page);
  const account = {
    account_id: "netease:123",
    provider: "netease" as const,
    user_id: "123",
    label: "合成网易云账号",
    state: "ready" as const,
    revision: 1,
    checked_at: 1792000000,
  };
  const source = {
    source_id: "900",
    name: "夜晚的收藏",
    description: "合成歌单资料",
    cover_url: null,
    track_count: 4,
    kind: "created" as const,
  };
  const state = {
    qrState: "waiting",
    failScan: false,
    calls: [] as { operation: string; body: Record<string, unknown> }[],
    view: {
      configured: true,
      audio_download_available: true,
      accounts: connected ? [account] : [],
      playlists: [],
      settings: {
        revision: 0,
        directory: "",
        layout: "artist_album",
        filename: "artist_title",
      },
    } as MusicView,
  };
  await page.route("**/api/web/media/music/**", async (route) => {
    const operation = new URL(route.request().url()).pathname.split(
      "/music/",
    )[1];
    const body = route.request().postDataJSON() as Record<string, unknown>;
    state.calls.push({ operation, body });
    const reply = (value: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(value),
      });
    if (operation === "view") return reply(state.view);
    if (operation === "accounts/qr/start")
      return reply({
        qr_id: "synthetic-qr",
        url: "https://music.163.com/login?codekey=synthetic-key",
        expires_at: Date.now() / 1000 + 180,
      });
    if (operation === "accounts/qr/poll") {
      if (state.qrState === "ready") state.view.accounts = [account];
      return reply({
        state: state.qrState,
        account: state.qrState === "ready" ? account : null,
      });
    }
    if (operation === "accounts/revoke") {
      state.view.accounts[0] = { ...account, state: "revoked", revision: 2 };
      return reply({ account: state.view.accounts[0] });
    }
    if (operation === "playlists/list") return reply({ playlists: [source] });
    if (operation === "playlists/subscribe") {
      const playlist = {
        ...source,
        playlist_id: "netease:123:playlist:900",
        account_id: "netease:123",
        revision: 1,
        member_count: 4,
        interval_seconds: Number(body.interval_seconds),
        state: "active",
        synced_at: 1792000000,
        code: "ready",
      } as MusicPlaylist;
      state.view.playlists.push(playlist);
      return reply({ playlist });
    }
    if (operation === "playlists/control") {
      const playlist = state.view.playlists[0];
      expect(body.expected_revision).toBe(playlist.revision);
      playlist.state = body.action === "pause" ? "paused" : "active";
      playlist.revision++;
      return reply({ playlist });
    }
    if (operation === "playlists/downloads") {
      const playlist = state.view.playlists[0];
      expect(body.expected_revision).toBe(playlist.revision);
      playlist.quality = body.quality as MusicPlaylist["quality"];
      playlist.auto_download = Boolean(body.auto_download);
      playlist.revision++;
      return reply({ playlist });
    }
    if (operation === "playlists/download") {
      expect(body.expected_revision).toBe(state.view.playlists[0].revision);
      return reply({ queued: 3, unavailable: 1 });
    }
    if (operation === "playlists/export")
      return reply({
        directory: "/synthetic/music/歌单/example",
        exported: 3,
        total: 4,
      });
    if (operation === "playlists/scan") {
      if (state.failScan) return reply({ code: "music_scan_incomplete" }, 502);
      return reply({ playlist: state.view.playlists[0] });
    }
    if (operation === "playlists/detail")
      return reply({
        playlist: state.view.playlists[0],
        offset: 0,
        more: false,
        tracks: ["清晨", "夜色", "清晨", ""].map((title, index) => ({
          source_id: String(100 + index),
          position: index + 1,
          title,
          artists: ["合成艺术家"],
          album: "合成专辑",
          availability: title ? "known" : "unavailable",
          audio_state: "not_downloaded",
        })),
      });
    if (operation === "settings/inspect")
      return reply({
        state: "readable",
        samples: [
          {
            artist: "合成艺术家",
            album: "合成专辑",
            files: ["合成艺术家 - 清晨.flac", "合成艺术家 - 清晨.lrc"],
          },
        ],
      });
    if (operation === "settings/save") {
      state.view.settings = {
        directory: String(body.directory),
        layout: body.layout as "artist_album",
        filename: "artist_title",
        revision: 1,
      };
      return reply({ settings: state.view.settings });
    }
    return reply({ code: "not_found" }, 404);
  });
  return state;
}

async function subscribe(page: Page) {
  await page.getByRole("button", { name: "读取账号歌单", exact: true }).click();
  await page.getByRole("button", { name: "订阅歌单", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "本地歌单", exact: true })
      .getByText("夜晚的收藏", { exact: true }),
  ).toBeVisible();
}

test("music entry offers account login and audio archiving", async ({
  page,
}) => {
  const state = await musicFixture(page, false);
  await page.goto("/#/subscriptions/5");
  await expect(page.getByRole("heading", { level: 1 })).toHaveText("音乐订阅");
  await expect(
    page.getByText("尚未连接音乐账号。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(/自动写入歌曲标签、封面和歌词/)).toBeVisible();
  await expect(
    page.getByRole("button", { name: "读取账号歌单", exact: true }),
  ).toBeDisabled();
  expect(
    state.calls.some((call) => call.operation.startsWith("accounts/qr")),
  ).toBe(false);
});

test("music download policy, manual queue and ordered exports", async ({
  page,
}, info) => {
  const state = await musicFixture(page);
  state.view.settings.directory = "/synthetic/music";
  await page.goto("/#/subscriptions/5");
  await subscribe(page);
  await page.getByText("下载设置", { exact: true }).click();
  await page
    .getByRole("combobox", { name: "期望音质", exact: true })
    .selectOption("hires");
  await page.getByLabel("允许使用当前账号可下载的较低音质").uncheck();
  await page.getByLabel("同步后自动下载新增歌曲", { exact: true }).check();
  await page.getByRole("button", { name: "保存下载设置", exact: true }).click();
  await expect(
    page.getByText("下载设置已保存，歌单已加入队列。", { exact: true }),
  ).toBeVisible();
  expect(state.view.playlists[0].quality).toEqual({
    level: "hires",
    allow_fallback: false,
  });
  await page.getByRole("button", { name: "下载歌单", exact: true }).click();
  await expect(page.getByText(/已加入下载队列 3 首/)).toBeVisible();
  await page
    .getByRole("button", { name: "导出 M3U8 / JSON", exact: true })
    .click();
  await expect(page.getByText(/已导出 3\/4 首/)).toBeVisible();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("music-download-settings.png"),
    fullPage: true,
  });
});

test("mixed download list renders audio evidence without video editing controls", async ({
  page,
}, info) => {
  const view = fixtureView();
  const audio = fixtureJob({
    job_id: "audio-one",
    media_kind: "audio",
    provider: "netease",
    source_id: "101",
    title: "合成歌曲",
    creator: "合成艺术家",
    state: "completed",
    stage: "completed",
    code: "music_archived",
    target_id: "music-library",
    actual_quality: "lossless",
    destination_directory: "/synthetic/music",
    can_cancel: false,
    can_retry: false,
    can_delete: true,
    audio_quality: {
      level: "lossless",
      codec: "flac",
      sample_rate: 44100,
      channels: 2,
      bits_per_sample: 16,
      bit_rate: 900000,
      fallback: false,
    },
    audio_receipt: {
      files: [{ path: "合成艺术家/合成专辑/合成歌曲.flac", kind: "audio" }],
      warnings: ["music_lyrics_unavailable"],
    },
    audio_reused: true,
  });
  // Real audio API documents have no BVID or selected CIDs.
  Object.assign(audio, { bvid: null, selected_cids: null, cid: null });
  view.jobs.unshift(audio);
  const state = await installMediaFixture(page, view);
  state.detailHandler = async (route) =>
    route.fulfill({
      contentType: "application/json",
      body: JSON.stringify({ job: audio, events: [], metadata: null }),
    });
  await page.goto("/#/subscriptions/2");
  await expect(page.getByText("合成歌曲", { exact: true })).toBeVisible();
  await expect(page.getByText("网易云 101", { exact: false })).toBeVisible();
  await page
    .locator('[data-job-id="audio-one"]')
    .getByRole("button", { name: "查看任务详情", exact: true })
    .click();
  const detail = page.getByRole("complementary", {
    name: "任务详情",
    exact: true,
  });
  await expect(
    detail.getByRole("heading", { name: "音乐归档", exact: true }),
  ).toBeVisible();
  await expect(
    detail.getByText("合成艺术家/合成专辑/合成歌曲.flac", { exact: true }),
  ).toBeVisible();
  await expect(
    detail.getByText("歌词未取得，音频已保留。", { exact: true }),
  ).toBeVisible();
  await expect(detail.getByText("媒体服务器核对", { exact: true })).toHaveCount(
    0,
  );
  await expect(
    detail.getByRole("button", { name: "重新解析来源", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath("music-job.png"),
    fullPage: true,
  });
});

test("NetEase QR shows waiting then connected and survives reload", async ({
  page,
}) => {
  const state = await musicFixture(page, false);
  await page.goto("/#/subscriptions/5");
  await page
    .getByRole("button", { name: "网易云扫码登录", exact: true })
    .click();
  await expect(page.getByAltText("网易云音乐登录二维码")).toBeVisible();
  await expect(page.getByText("等待扫码", { exact: true })).toBeVisible();
  state.qrState = "ready";
  await expect(
    page
      .getByRole("region", { name: "网易云账号", exact: true })
      .getByText("合成网易云账号", { exact: true }),
  ).toBeVisible({ timeout: 10000 });
  await page.reload();
  await expect(
    page
      .getByRole("region", { name: "网易云账号", exact: true })
      .getByText("合成网易云账号", { exact: true }),
  ).toBeVisible();
});

test("expired NetEase QR can be regenerated", async ({ page }) => {
  const state = await musicFixture(page, false);
  state.qrState = "expired";
  await page.goto("/#/subscriptions/5");
  await page
    .getByRole("button", { name: "网易云扫码登录", exact: true })
    .click();
  await expect(page.getByText("二维码已过期", { exact: true })).toBeVisible();
  await expect(page.getByAltText("网易云音乐登录二维码")).toHaveCount(0);
  state.qrState = "waiting";
  await page
    .getByRole("button", { name: "重新生成二维码", exact: true })
    .click();
  await expect(page.getByAltText("网易云音乐登录二维码")).toBeVisible();
});

test("playlist selection keeps source order and supports pause and resume", async ({
  page,
}, info) => {
  const state = await musicFixture(page);
  await page.goto("/#/subscriptions/5");
  await page
    .getByRole("combobox", { name: "同步周期", exact: true })
    .selectOption("21600");
  await subscribe(page);
  expect(
    state.calls.find((call) => call.operation === "playlists/subscribe")?.body
      .interval_seconds,
  ).toBe(21600);
  await page.getByRole("button", { name: "查看歌曲", exact: true }).click();
  const table = page.getByRole("region", { name: "歌单歌曲", exact: true });
  await expect(
    table.getByRole("cell", { name: "清晨", exact: true }),
  ).toHaveCount(2);
  await expect(
    table.getByRole("cell", { name: /暂不可用的歌曲/ }),
  ).toBeVisible();
  await expect(
    table.getByRole("cell", { name: "未下载", exact: true }),
  ).toHaveCount(4);
  await page.getByRole("button", { name: "暂停同步", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "立即同步", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "恢复同步", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "立即同步", exact: true }),
  ).toBeEnabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: info.outputPath(`music-${info.project.name}.png`),
    fullPage: true,
  });
});

test("music directory settings inspect a sample and preserve configured layout", async ({
  page,
}) => {
  const state = await musicFixture(page);
  await page.goto("/#/subscriptions/5");
  await page.getByLabel("音乐目录", { exact: true }).fill("/synthetic/music");
  await page.getByRole("button", { name: "查看目录结构", exact: true }).click();
  await expect(
    page.getByText("目录可读取，以下为抽样结构。", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "保存存储设置", exact: true }).click();
  await expect(
    page.getByText("音乐目录和整理方式已保存。", { exact: true }),
  ).toBeVisible();
  expect(state.view.settings.directory).toBe("/synthetic/music");
  await page.reload();
  await expect(page.getByLabel("音乐目录", { exact: true })).toHaveValue(
    "/synthetic/music",
  );
});

test("incomplete sync and account disconnect retain the local playlist", async ({
  page,
}) => {
  const state = await musicFixture(page);
  await page.goto("/#/subscriptions/5");
  await subscribe(page);
  state.failScan = true;
  await page.getByRole("button", { name: "立即同步", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("已保留上次同步结果");
  await page.getByRole("button", { name: "断开账号", exact: true }).click();
  await page.getByRole("button", { name: "查看歌曲", exact: true }).click();
  await expect(
    page
      .getByRole("region", { name: "歌单歌曲", exact: true })
      .getByRole("cell", { name: "清晨", exact: true }),
  ).toHaveCount(2);
});
