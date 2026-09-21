/**
 * Response shapes of the verified persona read contract, projected by this console.
 *
 * These types describe what the page may receive and nothing more: no credential, no endpoint, no
 * local path and no operator identity is part of any of them. Words the producer may use are
 * mapped through the label tables below, and a word this page does not know is displayed as
 * unknown rather than guessed at.
 */

/** One directory row. A refusal is a stated row, never a missing one and never a blank one. */
export type CatalogEntry = {
  subject: string;
  state: string | null;
  version: number | null;
  published_revision: string | null;
  draft_revision: string | null;
  retired: boolean | null;
  updated_at: number | null;
  error: string | null;
};

export type CatalogPage = {
  code: string;
  page: number;
  subjects: number;
  count: number;
  entries: CatalogEntry[];
  has_more: boolean;
  next_cursor: string | null;
};

/** One history row. The four kinds carry different fields; an absent one stays absent. */
export type HistoryRow = {
  revision_id?: string | null;
  fingerprint?: string | null;
  parent?: string | null;
  source?: string | null;
  operator?: string | null;
  note?: string | null;
  created_at?: number | null;
  decision?: string | null;
  decided_by?: string | null;
  decided_at?: number | null;
  publication_id?: string | null;
  supersedes?: string | null;
  generation?: number | null;
  kind?: string | null;
  state?: string | null;
  reason?: string | null;
  approval_id?: string | null;
  rollback_id?: string | null;
  restored_revision?: string | null;
  target_revision?: string | null;
  superseded_revision?: string | null;
};

export type HistoryPage = {
  subject: string;
  kind: string;
  persona_version: number;
  consistency: string;
  limit: number;
  count: number;
  entries: HistoryRow[];
  has_more: boolean;
  next_cursor: string | null;
};

export type RevisionBody = {
  revision_id: string;
  fingerprint: string | null;
  content: Record<string, string | null>;
  present: string[];
  additional_fields: string[];
  parent: string | null;
  source: string | null;
  note: string | null;
  created_at: number | null;
};

export type RevisionView = {
  subject: string;
  revision: RevisionBody;
  is_published: boolean;
  is_draft: boolean;
  persona_version: number;
  state: string;
  published_revision: string | null;
  draft_revision: string | null;
};

export type Reference = {
  revision_id: string;
  fingerprint: string | null;
  parent: string | null;
  source: string | null;
  note: string | null;
  created_at: number | null;
};

export type Change = "added" | "removed" | "modified" | "unchanged";

export type CompareField = {
  change: Change;
  presence: { left: boolean; right: boolean };
  left: string | null;
  right: string | null;
};

export type CompareView = {
  subject: string;
  left: Reference;
  right: Reference;
  fields: Record<string, CompareField>;
  persona_version: number;
  state: string;
  published_revision: string | null;
  draft_revision: string | null;
  additional_fields_present: boolean;
  additional_fields_changed: boolean;
  additional_field_names: string[];
  identical_revision: boolean;
  content_identical: boolean;
};

export const HISTORY_KINDS = [
  "revisions",
  "publications",
  "approvals",
  "rollbacks",
] as const;

export type HistoryKind = (typeof HISTORY_KINDS)[number];

export const kindLabels: Record<HistoryKind, string> = {
  revisions: "修订",
  publications: "发布",
  approvals: "批准",
  rollbacks: "回退",
};

/** The four fields this page interprets. Everything else is reported by name only. */
export const COMPARE_FIELDS = ["persona", "tone", "style", "address"] as const;

export const fieldLabels: Record<string, string> = {
  persona: "人格",
  tone: "语气",
  style: "表达",
  address: "称呼",
};

export const changeLabels: Record<Change, string> = {
  added: "新增",
  removed: "移除",
  modified: "改动",
  unchanged: "相同",
};

/** One word per persona state the producer may report. */
export const stateLabels: Record<string, string> = {
  published: "已发布",
  draft: "草稿",
  approved: "已批准",
  unpublished: "未发布",
  retired: "已停用",
};

/** Why one directory row is not readable: the page repeats the code it actually received. */
export const rowErrorLabels: Record<string, string> = {
  not_found: "角色服务里没有这个角色",
  invalid_upstream: "回答无法使用",
  dependency_unavailable: "暂时读不到",
  timeout: "没有按时回答",
  forbidden: "没有读取权限",
  unauthorized: "连接凭据未被接受",
  invalid_input: "请求被拒绝",
};

export function stateLabel(state: string | null) {
  if (!state) return "未知";
  return stateLabels[state] ?? `未知状态（${state}）`;
}

export function rowErrorLabel(code: string) {
  return rowErrorLabels[code] ?? `无法读取（${code}）`;
}

/** A short, unambiguous rendering of a revision digest; never a URL and never a full id. */
export function shortDigest(value: string | null | undefined) {
  return value ? value.slice(0, 8) : "—";
}

/** Seconds since the epoch to a local, text-only timestamp; an absent time stays absent. */
export function shortTime(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value))
    return "没有记录时间";
  return new Date(value * 1000).toLocaleString();
}

/**
 * The one field a history kind is about. A row is labelled by what it actually moved or decided,
 * so a publication is never displayed as a revision and a rejection is never displayed as an
 * approval.
 */
export function rowTitle(kind: HistoryKind, row: HistoryRow) {
  if (kind === "revisions") return shortDigest(row.revision_id);
  if (kind === "publications") return `第 ${row.generation ?? "?"} 次发布`;
  if (kind === "approvals")
    return row.decision === "rejected" ? "驳回" : "批准";
  return "回退";
}
