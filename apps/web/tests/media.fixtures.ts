import type { Page, Route } from "@playwright/test";
import {
  bestQuality,
  emptyRules,
  type Job,
  type JobDetail,
  type MediaView,
  type Preview,
  type Subscription,
} from "../src/features/media/types";

export const fixtureJob = (overrides: Partial<Job> = {}): Job => ({
  job_id: "media-job-one",
  subscription_id: null,
  media_key: "bilibili:video:BV1xx411c7mD",
  bvid: "BV1xx411c7mD",
  cid: "100",
  selected_cids: ["100", "200"],
  title: "隔离样例 · 城市与山海",
  creator: "合成 UP 主",
  state: "waiting_metadata",
  stage: "metadata_ready",
  code: "missing_description",
  progress: null,
  created_at: 1791475200,
  updated_at: 1791475210,
  target_id: "test-library",
  quality: bestQuality(),
  actual_quality: "80",
  cancel_requested: false,
  can_retry: false,
  can_cancel: true,
  revision: 3,
  asset_receipt: null,
  library_results: [],
  ...overrides,
});
export const fixturePreview = (): Preview => ({
  preview_id: "preview-test",
  expires_at: Math.floor(Date.now() / 1000) + 300,
  video: {
    bvid: "BV1xx411c7mD",
    title: "隔离样例 · 城市与山海",
    description: "明确的合成视频资料，不连接真实账号。",
    creator: { mid: "123", name: "合成 UP 主" },
    cover_url: null,
    parts: [
      {
        cid: "100",
        index: 1,
        title: "城市",
        duration_seconds: 128,
        formats: [
          { quality_id: "80", label: "1080P" },
          { quality_id: "64", label: "720P" },
        ],
      },
      {
        cid: "200",
        index: 2,
        title: "山海",
        duration_seconds: 98,
        formats: [{ quality_id: "64", label: "720P" }],
      },
    ],
  },
});
export const fixtureView = (): MediaView => ({
  configured: true,
  capabilities: {
    provider: "bilibili",
    source_kinds: ["favorite", "collection", "series", "uploader"],
  },
  engine: { state: "ready", version: "synthetic", code: "" },
  accounts: [
    {
      account_id: "test-account",
      label: "隔离 B 站账号",
      state: "ready",
      revision: 1,
      checked_at: 1791475200,
      code: "",
    },
  ],
  targets: [
    {
      target_id: "test-library",
      label: "隔离网络视频库",
      state: "ready",
      servers: [
        {
          server_id: "test-jellyfin",
          label: "隔离 Jellyfin",
          kind: "jellyfin",
        },
        { server_id: "test-emby", label: "隔离 Emby", kind: "emby" },
      ],
    },
  ],
  subscriptions: [],
  jobs: [fixtureJob()],
});
export function fixtureDetail(job: Job): JobDetail {
  return {
    job: structuredClone(job),
    events: [
      { state: "queued", code: "", at: job.created_at },
      { state: job.state, code: job.code, at: job.updated_at },
    ],
    metadata: {
      title: job.title,
      description: "",
      original_title: "来源原标题",
      original_description: null,
      title_overridden: true,
      description_overridden: false,
      issues: ["missing_description"],
      cover_available: true,
    },
  };
}
export async function installMediaFixture(page: Page, initial = fixtureView()) {
  const state = {
    view: initial,
    authenticated: true,
    unavailable: false,
    ruleError: false,
    calls: [] as { operation: string; body: Record<string, unknown> }[],
    resolveHandler: null as ((route: Route) => Promise<void>) | null,
    detailHandler: null as ((route: Route) => Promise<void>) | null,
    listHandler: null as ((route: Route) => Promise<void>) | null,
  };
  await page.route("**/api/web/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    const reply = (body: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(body),
      });
    if (path === "/api/web/session")
      return reply({
        authenticated: state.authenticated,
        csrf: "synthetic-media-csrf",
        username: "隔离网页登录",
        onboarding: { state: "ready" },
      });
    if (path === "/api/web/logout") {
      state.authenticated = false;
      return reply({ authenticated: false });
    }
    const operation = path.replace("/api/web/media/", "");
    const body = route.request().postDataJSON() ?? {};
    state.calls.push({ operation, body });
    if (!state.authenticated) return reply({ code: "session_expired" }, 401);
    if (operation === "view")
      return state.unavailable
        ? reply({ code: "dependency_unavailable" }, 503)
        : reply(state.view);
    if (operation === "jobs/list") {
      if (state.listHandler) return state.listHandler(route);
      const states = body.states as string[] | null;
      const jobs = state.view.jobs.filter(
        (job) => !states || states.includes(job.state),
      );
      const offset = body.cursor ? Number(body.cursor) : 0;
      const size = body.page_size as number;
      return reply({
        jobs: jobs.slice(offset, offset + size),
        page: {
          has_more: offset + size < jobs.length,
          next_cursor:
            offset + size < jobs.length ? String(offset + size) : null,
        },
      });
    }
    if (operation === "jobs/refresh_metadata") {
      const job = state.view.jobs.find((item) => item.job_id === body.job_id)!;
      job.revision += 1;
      return reply({ job, replayed: false });
    }
    if (operation === "resolve")
      return state.resolveHandler
        ? state.resolveHandler(route)
        : reply(fixturePreview());
    if (operation === "enqueue") {
      const job = fixtureJob({
        state: "queued",
        stage: "queued",
        code: "",
        selected_cids: body.part_cids as string[],
      });
      state.view.jobs = [job];
      return reply({ jobs: [job], replayed: false });
    }
    if (operation === "rules/preview")
      return reply({
        results: [
          {
            cid: "100",
            decision: state.ruleError ? "rule_error" : "download",
            reason: state.ruleError ? "regex_timeout" : "eligible",
            automatic_enqueue_allowed: !state.ruleError,
            requires_rule_attention: state.ruleError,
            trace: { item_key: "bilibili:video:BV1xx411c7mD", trace: [] },
          },
        ],
      });
    if (operation === "subscriptions/save") {
      const subscription: Subscription = {
        ...(body as unknown as Subscription),
        subscription_id:
          (body.subscription_id as string) ||
          `subscription-${state.view.subscriptions.length}`,
        revision: (body.expected_revision as number) + 1,
        state: "active",
        baseline_ready: false,
        last_scan_at: null,
        next_scan_at: 1791475300,
        code: "",
        counts: { members: 0, queued: 0 },
      };
      state.view.subscriptions = [
        ...state.view.subscriptions.filter(
          (item) => item.subscription_id !== subscription.subscription_id,
        ),
        subscription,
      ];
      return reply({ subscription, replayed: false });
    }
    if (
      operation === "subscriptions/control" ||
      operation === "subscriptions/scan"
    ) {
      const subscription = state.view.subscriptions.find(
        (item) => item.subscription_id === body.subscription_id,
      )!;
      if (operation === "subscriptions/control") {
        subscription.state = body.action === "pause" ? "paused" : "active";
        subscription.revision += 1;
      } else {
        subscription.baseline_ready = true;
        subscription.last_scan_at = 1791475200;
        subscription.counts = { members: 3, queued: 1 };
      }
      return reply({ subscription, queued: 1, replayed: false });
    }
    if (operation === "jobs/detail")
      return state.detailHandler
        ? state.detailHandler(route)
        : reply(
            fixtureDetail(
              state.view.jobs.find((item) => item.job_id === body.job_id)!,
            ),
          );
    if (operation === "jobs/control") {
      const job = state.view.jobs.find((item) => item.job_id === body.job_id)!;
      job.state = body.action === "cancel" ? "cancelled" : "queued";
      job.stage = job.state;
      job.can_cancel = body.action !== "cancel";
      job.can_retry = false;
      job.updated_at += 1;
      job.revision += 1;
      return reply({ job, replayed: false });
    }
    if (operation === "jobs/metadata") {
      const job = state.view.jobs.find((item) => item.job_id === body.job_id)!;
      if (body.expected_revision !== job.revision)
        return reply({ code: "version_conflict" }, 409);
      job.title = body.title as string;
      job.state = "queued";
      job.revision += 1;
      return reply({ job, replayed: false });
    }
    if (operation === "accounts/qr/start")
      return reply({
        qr_id: "synthetic-qr",
        url: "https://passport.bilibili.com/h5-app/passport/login/scan?synthetic=1",
        expires_at: Math.floor(Date.now() / 1000) + 120,
      });
    if (operation === "accounts/qr/poll")
      return reply({ state: "waiting", account: null });
    if (operation === "accounts/import") {
      const account = {
        account_id: "imported-account",
        label: body.label as string,
        state: "ready" as const,
        revision: 1,
        checked_at: 1791475200,
        code: "",
      };
      state.view.accounts.push(account);
      return reply({ account, replayed: false });
    }
    if (operation === "accounts/check" || operation === "accounts/revoke") {
      const account = state.view.accounts.find(
        (item) => item.account_id === body.account_id,
      )!;
      if (operation === "accounts/revoke") {
        account.state = "revoked";
        account.revision += 1;
      }
      return reply({ account, replayed: false });
    }
    return reply({ code: "not_found" }, 404);
  });
  return state;
}
export const fixtureSubscription = (): Subscription => ({
  subscription_id: "existing-subscription",
  label: "已有收藏夹订阅",
  revision: 2,
  source: { kind: "favorite", id: "123456" },
  account_id: "test-account",
  target_id: "test-library",
  initial_sync: "future_only",
  quality: bestQuality(),
  rules: emptyRules(),
  state: "active",
  baseline_ready: false,
  last_scan_at: null,
  next_scan_at: 1791475300,
  interval_seconds: 1800,
  code: "",
  counts: { members: 0, queued: 0 },
});
