export type Quality = {
  mode: "best" | "exact";
  quality_id: string | null;
  allow_fallback: boolean;
};
export type Rule = {
  id: string;
  field: "title" | "description" | "uploader_id" | "uploader_name" | "tags";
  op: "equals" | "contains" | "prefix" | "suffix" | "regex";
  value: string;
  case_sensitive: boolean;
};
export type Group = { id: string; rules: Rule[] };
export type Rules = {
  schema_version: 1;
  revision: number;
  whitelist: Group[];
  blacklist: Group[];
};
export type Source = {
  kind: "favorite" | "collection" | "series" | "uploader";
  id: string;
};
export type Account = {
  account_id: string;
  label: string;
  state: "ready" | "auth_required" | "revoked";
  revision: number;
  checked_at: number | null;
  code: string;
};
export type Target = {
  target_id: string;
  label: string;
  state: string;
  servers: { server_id: string; label: string; kind: string }[];
};
export type Subscription = {
  subscription_id: string;
  label: string;
  revision: number;
  source: Source;
  account_id: string | null;
  target_id: string;
  initial_sync: "future_only" | "history";
  quality: Quality;
  rules: Rules;
  state: "active" | "paused" | "auth_required" | "rule_error";
  baseline_ready: boolean;
  last_scan_at: number | null;
  next_scan_at: number;
  interval_seconds: number;
  code: string;
  counts: { members: number; queued: number };
};
export type Job = {
  job_id: string;
  subscription_id: string | null;
  media_key: string;
  bvid: string;
  cid: string;
  selected_cids: string[];
  title: string;
  creator: string;
  state: string;
  stage: string;
  code: string;
  progress: number | null;
  created_at: number;
  updated_at: number;
  target_id: string;
  quality: Quality;
  actual_quality: string | null;
  layout?: "single" | "multipart";
  cancel_requested: boolean;
  can_retry: boolean;
  can_cancel: boolean;
  revision: number;
  asset_receipt: Record<string, unknown> | null;
  library_results: {
    server_id: string;
    kind: string;
    state: string;
    code: string;
    item_id: string | null;
  }[];
};
export type MediaView = {
  configured: boolean;
  capabilities: { provider: string; source_kinds: string[] };
  engine: { state: string; version: string | null; code: string };
  accounts: Account[];
  targets: Target[];
  subscriptions: Subscription[];
  jobs: Job[];
};
export type Format = { quality_id: string; label: string };
export type Part = {
  cid: string;
  index: number;
  title: string;
  duration_seconds: number | null;
  formats: Format[];
};
export type Preview = {
  preview_id: string;
  expires_at: number;
  video: {
    bvid: string;
    title: string;
    description: string | null;
    creator: { mid: string; name: string | null };
    cover_url: string | null;
    parts: Part[];
  };
};
export type RuleResult = {
  cid: string;
  decision: string;
  reason: string;
  automatic_enqueue_allowed: boolean;
  requires_rule_attention: boolean;
  trace: unknown;
};
export type JobDetail = {
  job: Job;
  events: { state: string; code: string; at: number }[];
  metadata: {
    title: string;
    description: string;
    issues: string[];
    cover_available: boolean;
    original_title: string;
    original_description: string | null;
    title_overridden: boolean;
    description_overridden: boolean;
  };
};
export type QrSession = { qr_id: string; url: string; expires_at: number };
export const bestQuality = (): Quality => ({
  mode: "best",
  quality_id: null,
  allow_fallback: false,
});
export const emptyRules = (): Rules => ({
  schema_version: 1,
  revision: 1,
  whitelist: [],
  blacklist: [],
});
