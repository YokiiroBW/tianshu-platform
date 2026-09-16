import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ListFilter, LockKeyhole, RefreshCw } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import {
  call,
  session as readSession,
  WebError,
  type SessionState,
  type TaskDetail,
  type TaskDetailView,
  type TaskEvidence,
  type TaskItem,
  type TaskSource,
  type TasksPage,
  type TasksView,
} from "./api";
import "./tasks.css";

const PAGE_SIZE = 10;
const POLL_MS = 15_000;

const statusRail: Record<string, { tone: Tone; label: string }> = {
  accepted: { tone: "yellow", label: "已受理" },
  in_progress: { tone: "yellow", label: "执行中" },
  observed: { tone: "blue", label: "已观测" },
  unknown: { tone: "gray", label: "未知" },
  failed: { tone: "red", label: "失败" },
};
/** Each status says what it actually claims; accepted is never shown as done. */
const statusNote: Record<string, string> = {
  accepted: "只表示平台已被受理，执行结果还没有依据。",
  in_progress: "已经认领或已经发出，结果还没有确定。",
  observed: "有后续观测或权威记录作为依据。",
  unknown: "结果无法确认；平台不会自动重发。",
  failed: "执行方明确拒绝或明确失败。",
};
const stageWording: Record<string, string> = {
  pending: "等待执行",
  observing: "已认领未发出",
  executing: "已发出待回执",
  accepted: "收到回执",
  observed: "观测确认",
  unknown: "结果未知",
  rejected: "被拒绝",
  published: "权威版本已核对",
  unverified: "权威版本未确认",
  corrupt: "权威内容不一致",
};
const sourceWording: Record<string, string> = {
  "platform.home": "家庭设备控制",
  "platform.models": "模型配置发布",
  "companion.core": "Core 对话与写作",
  "assets.remote": "资产远端任务",
  "resources.download": "订阅与下载",
  "platform.dialogue": "网页对话",
};
const sourceDetail: Record<string, string> = {
  "companion.core": "对话与写作任务还没有正式的任务合同，本轮没有接入。",
  "assets.remote": "资产远端任务还没有正式的任务合同，本轮没有接入。",
  "resources.download": "订阅与下载任务还没有正式的任务合同，本轮没有接入。",
  "platform.dialogue":
    "网页对话只有发送回执，没有可投影的操作台账，本轮没有接入。",
};
const kindWording: Record<string, string> = {
  "device.control": "设备控制",
  "model.publish": "模型发布",
};
const sourceState: Record<string, { tone: Tone; label: string; note: string }> =
  {
    online: { tone: "blue", label: "在线", note: "状态读取正常。" },
    offline: {
      tone: "red",
      label: "离线",
      note: "最近一次状态读取失败；已有记录保持原样，不显示成功。",
    },
    never_read: {
      tone: "yellow",
      label: "尚未读取",
      note: "还没有成功读取过状态，因此没有可核对的依据。",
    },
    disabled: {
      tone: "gray",
      label: "未开启",
      note: "设置里没有开启这一块，历史记录仍然保留。",
    },
    credential_missing: {
      tone: "red",
      label: "缺少凭据",
      note: "登记的凭据环境变量没有值。",
    },
    unconfigured: {
      tone: "gray",
      label: "未配置",
      note: "这个部署没有登记该来源。",
    },
    missing: {
      tone: "yellow",
      label: "登记已移除",
      note: "登记被移除，历史记录仍然保留在这里。",
    },
    unreadable: {
      tone: "red",
      label: "无法读取",
      note: "台账暂时读不到，这里的内容可能不是最新。",
    },
    available: {
      tone: "blue",
      label: "可读",
      note: "权威记录与意图台账都能读取。",
    },
    empty: {
      tone: "gray",
      label: "暂无记录",
      note: "还没有发布或撤销过任何版本。",
    },
    not_connected: { tone: "gray", label: "未接入", note: "" },
  };
const originWording: Record<string, { tone: Tone; label: string }> = {
  current: { tone: "blue", label: "登记仍在" },
  origin_missing: { tone: "yellow", label: "登记已移除" },
  source_missing: { tone: "gray", label: "来源已移除" },
};
const cancelWording: Record<string, string> = {
  executor_does_not_cancel:
    "执行方不提供取消：已经发出的设备指令无法撤回，也不会从任务中心自动重发。要改变状态请到家庭设备页重新操作。",
  recorded_fact:
    "这是已经发生并记录的发布事实，不是可以取消的操作。要改变配置请到模型与用量页发布新版本。",
};
const fieldWording: Record<string, string> = {
  acceptance: "受理",
  receipt: "回执中出现目标状态",
  observation: "观测结果",
  observation_state: "观测状态",
  observed_at: "观测时间",
  revision: "读数修订号",
  operation: "操作",
  actor: "操作者",
  integrity: "权威内容",
  availability: "记录可用性",
  revoked: "已撤销",
  claim: "另有未确认回执",
  receipt_recorded: "回执已落库",
  authoritative: "权威版本",
  window: "受理窗口",
  requested_state: "请求状态",
  template_id: "模板",
  entity_id: "实体",
  service: "服务",
  target: "目标",
  version: "版本",
  recorded_at: "记录时间",
  published_at: "发布时间",
  usable_until: "可用至",
  revoked_at: "撤销时间",
  settled_at: "结案时间",
};
const valueWording: Record<string, string> = {
  pending: "等待",
  observing: "已认领",
  sending: "已发出",
  accepted: "已受理",
  observed: "已观测",
  unknown: "未知",
  rejected: "已拒绝",
  confirmed: "与目标一致",
  contradicted: "与目标不一致",
  unreadable: "不可读",
  digest_match: "摘要一致",
  digest_mismatch: "摘要不一致",
  absent: "权威行不存在",
  open: "未到期",
  expired: "已过期",
  published: "已发布并核对",
  unverified: "未确认",
  corrupt: "内容不一致",
  config: "Chat 兼容配置",
  native: "原生配置",
  "config.publish": "发布 Chat 配置",
  "native_config.publish": "发布原生配置",
  "config.revoke": "撤销 Chat 配置",
  "native_config.revoke": "撤销原生配置",
};
const dateOnly = /^\d{4}-\d{2}-\d{2}T/;

type Tone = "blue" | "yellow" | "red" | "gray";

function rail(
  table: Record<string, { tone: Tone; label: string }>,
  key: string,
) {
  return table[key] ?? { tone: "gray" as Tone, label: "未知" };
}

function stamp(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

function reason(cause: unknown) {
  return cause instanceof Error ? cause.message : "连接中断，请重新连接。";
}

/** One evidence value: booleans and known words are read out, timestamps are stamped. */
function show(value: TaskEvidence[string]) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "number") return String(value);
  if (dateOnly.test(value)) return stamp(value);
  return valueWording[value] ?? value;
}

/** A new page never hides a record the page already showed: same id keeps its place. */
function merge(current: TaskItem[], incoming: TaskItem[]) {
  const seen = new Map(
    current.map((item, position) => [item.task_id, position]),
  );
  const next = [...current];
  for (const item of incoming) {
    const position = seen.get(item.task_id);
    if (position === undefined) next.push(item);
    else next[position] = item;
  }
  return next;
}

export function TasksPanel() {
  const [session, setSession] = useState<SessionState | null>(null);
  const [items, setItems] = useState<TaskItem[]>([]);
  const [page, setPage] = useState<TasksPage | null>(null);
  const [sources, setSources] = useState<TaskSource[]>([]);
  const [generatedAt, setGeneratedAt] = useState("");
  const [status, setStatus] = useState("");
  const [source, setSource] = useState("");
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [open, setOpen] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);
  const cursor = useRef<string | null>(null);
  const known = useRef<TaskItem[]>([]);
  const list = useRef<HTMLDivElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const password = useRef<HTMLInputElement>(null);

  const start = useCallback(() => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    return controller;
  }, []);

  useEffect(() => {
    known.current = items;
  }, [items]);

  const body = useCallback(
    (extra: object = {}) => ({
      status: status || null,
      source: source || null,
      page_size: PAGE_SIZE,
      ...extra,
    }),
    [source, status],
  );

  /** The first page of the current filter; the cursor is always re-derived from it. */
  const read = useCallback(
    async (state: SessionState, signal: AbortSignal, quiet = false) => {
      if (!quiet) setBusy(true);
      const result = await call<TasksView>(
        "tasks/view",
        body(),
        state.csrf,
        signal,
      );
      cursor.current = result.page.next_cursor;
      setItems(result.items);
      setPage(result.page);
      setSources(result.sources);
      setGeneratedAt(result.generated_at);
    },
    [body],
  );

  const load = useCallback(
    async (signal: AbortSignal, retry = true) => {
      setBusy(true);
      try {
        const state = await readSession(signal);
        setSession(state);
        if (!state.authenticated) {
          setItems([]);
          setPage(null);
          setDetail(null);
          setOpen("");
          return;
        }
        try {
          await read(state, signal);
        } catch (cause) {
          setItems([]);
          setPage(null);
          // A revoked or expired login is a state, not a stale panel: re-read once, then show it.
          if (
            retry &&
            cause instanceof WebError &&
            ["session_expired", "unauthorized"].includes(cause.code)
          ) {
            await load(signal, false);
            setError(cause.message);
            return;
          }
          throw cause;
        }
      } catch (cause) {
        if (!signal.aborted) setError(reason(cause));
      } finally {
        if (!signal.aborted) setBusy(false);
      }
    },
    [read],
  );

  useEffect(() => {
    const controller = start();
    void load(controller.signal);
    return () => {
      controller.abort();
    };
  }, [load, start]);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      setError("");
      await load(signal ?? start().signal, false);
    },
    [load, start],
  );

  // A filter change is a new question: it starts from the first page, never from a stale cursor.
  const filtered = useRef("");
  useEffect(() => {
    const key = `${status}|${source}`;
    if (!session?.authenticated) {
      filtered.current = key;
      return;
    }
    if (filtered.current === key) return;
    filtered.current = key;
    const controller = start();
    setBusy(true);
    setError("");
    setOpen("");
    setDetail(null);
    void read(session, controller.signal, true)
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) setError(reason(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setBusy(false);
      });
  }, [read, session, source, start, status]);

  /** Polling first: a rejected poll never replaces what the panel already read. */
  useEffect(() => {
    if (!session?.authenticated) return;
    let timer = 0;
    const tick = async () => {
      if (document.visibilityState !== "visible") return;
      const controller = new AbortController();
      try {
        const result = await call<TasksView>(
          "tasks/view",
          body(),
          session.csrf,
          controller.signal,
        );
        const seen = new Set(known.current.map((item) => item.task_id));
        const added = result.items.filter((item) => !seen.has(item.task_id));
        setItems((current) => merge(current, result.items));
        setSources(result.sources);
        setGeneratedAt(result.generated_at);
        if (added.length) setNotice(`有 ${added.length} 条新的操作记录。`);
      } catch {
        // Offline or logged out: the next tick or the next action reports it honestly.
      }
    };
    timer = window.setInterval(() => void tick(), POLL_MS);
    const onVisible = () => {
      if (document.visibilityState === "visible") void tick();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [body, session]);

  const close = useCallback(() => {
    const task = open;
    setOpen("");
    setDetail(null);
    if (task)
      list.current
        ?.querySelector<HTMLButtonElement>(`[data-task-toggle="${task}"]`)
        ?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [close, open]);

  async function openDetail(taskId: string) {
    if (open === taskId) {
      close();
      return;
    }
    setOpen(taskId);
    setDetail(null);
    setNotice("");
    const controller = start();
    try {
      const result = await call<TaskDetailView>(
        "tasks/detail",
        { task_id: taskId },
        session?.csrf ?? "",
        controller.signal,
      );
      setDetail(result.task);
      window.setTimeout(() => panel.current?.focus(), 0);
    } catch (cause) {
      if (controller.signal.aborted) return;
      const message = reason(cause);
      setError(message);
      if (
        cause instanceof WebError &&
        ["session_expired", "unauthorized"].includes(cause.code)
      ) {
        await refresh();
        setError(message);
      }
    }
  }

  async function more() {
    if (!session || !cursor.current) return;
    setBusy(true);
    setError("");
    const controller = start();
    try {
      const result = await call<TasksView>(
        "tasks/view",
        body({ cursor: cursor.current }),
        session.csrf,
        controller.signal,
      );
      cursor.current = result.page.next_cursor;
      setItems((current) => merge(current, result.items));
      setPage(result.page);
      setSources(result.sources);
    } catch (cause) {
      if (controller.signal.aborted) return;
      const message = reason(cause);
      setError(message);
      if (cause instanceof WebError && cause.code === "cursor_conflict") {
        // The list conditions changed under the cursor: read the first page again.
        await refresh(controller.signal);
        setError(message);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function authenticate(form: HTMLFormElement) {
    if (!session) return;
    const data = new FormData(form);
    setBusy(true);
    setError("");
    const controller = start();
    try {
      // The login POST needs the CSRF token of the session cookie it already carries.
      await call(
        "login",
        {
          username: String(data.get("username")),
          password: String(data.get("password")),
        },
        session.csrf,
        controller.signal,
      );
    } catch (cause) {
      if (!controller.signal.aborted) setError(reason(cause));
      return;
    } finally {
      if (password.current) password.current.value = "";
      if (!controller.signal.aborted) setBusy(false);
    }
    await refresh();
  }

  const connected = useMemo(
    () => sources.filter((item) => item.connected),
    [sources],
  );
  const knownRecords = connected.reduce(
    (total, item) => total + (item.records ?? 0),
    0,
  );
  const unreadable = connected.filter((item) => item.state === "unreadable");
  const picked = sources.find((item) => item.id === source);
  const filtering = Boolean(status || source);

  return (
    <section className="panel tasks" aria-label="任务中心" aria-busy={busy}>
      <div className="section-heading">
        <h2>任务</h2>
        {page && (
          <StatusRail
            tone={items.length ? "blue" : "gray"}
            label={`已显示 ${items.length} 条`}
          />
        )}
      </div>
      <p className="muted">
        这里列出本平台已经真实记录的模型发布与设备控制操作，直接读取它们各自的台账，
        不新建一套执行数据库。受理、执行中、已观测、未知与失败分开显示：受理不是执行，
        回执不是结果，无法确认的结果保持未知。
      </p>
      <div className="tasks-status" role="status">
        {busy
          ? "正在读取操作记录…"
          : session?.authenticated
            ? `已登录 · ${session.username} · 最近读取 ${stamp(generatedAt)}`
            : "请登录本机管理员账号"}
      </div>
      {error && (
        <p className="tasks-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="tasks-notice" role="status">
          {notice}
        </p>
      )}
      {!session ? (
        <StatePanel kind="loading" title="正在连接任务中心">
          <p>稍候。</p>
        </StatePanel>
      ) : !session.authenticated ? (
        <div className="tasks-login">
          <LockKeyhole aria-hidden="true" />
          <p className="muted">
            任务中心沿用真实登录会话与撤权：退出、过期或凭据轮换后，这里不再显示任何记录。
          </p>
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void authenticate(event.currentTarget);
            }}
          >
            <label>
              管理员账号
              <input
                name="username"
                autoComplete="username"
                maxLength={128}
                required
                disabled={busy}
              />
            </label>
            <label>
              密码
              <input
                ref={password}
                name="password"
                type="password"
                autoComplete="current-password"
                minLength={12}
                maxLength={256}
                required
                disabled={busy}
              />
            </label>
            <button className="button primary" type="submit" disabled={busy}>
              登录
            </button>
          </form>
        </div>
      ) : (
        <>
          <div className="tasks-actions">
            <button
              className="button"
              onClick={() => void refresh()}
              disabled={busy}
            >
              <RefreshCw aria-hidden="true" />
              重新读取
            </button>
          </div>
          <div className="tasks-sources" aria-label="操作来源">
            {sources.map((item) => {
              const state = sourceState[item.state] ?? {
                tone: "gray" as Tone,
                label: "未知",
                note: "来源没有报告状态。",
              };
              return (
                <article className="tasks-source" key={item.id}>
                  <div className="tasks-source-head">
                    <h3>
                      {sourceWording[item.id] ?? item.id}
                      {item.kind ? (
                        <span className="muted">
                          {" "}
                          · {kindWording[item.kind] ?? item.kind}
                        </span>
                      ) : null}
                    </h3>
                    <StatusRail tone={state.tone} label={state.label} />
                  </div>
                  <p className="muted">
                    {item.connected
                      ? `已记录 ${item.records ?? "未知"} 条 · ${state.note}`
                      : sourceDetail[item.id]}
                  </p>
                </article>
              );
            })}
          </div>
          <div className="tasks-filters">
            <label>
              状态
              <select
                value={status}
                onChange={(event) => setStatus(event.target.value)}
                disabled={busy}
              >
                <option value="">全部状态</option>
                {Object.entries(statusRail).map(([value, item]) => (
                  <option value={value} key={value}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              来源
              <select
                value={source}
                onChange={(event) => setSource(event.target.value)}
                disabled={busy}
              >
                <option value="">全部来源</option>
                {sources.map((item) => (
                  <option value={item.id} key={item.id}>
                    {sourceWording[item.id] ?? item.id}
                    {item.connected ? "" : "（未接入）"}
                  </option>
                ))}
              </select>
            </label>
            {filtering && (
              <button
                className="button"
                onClick={() => {
                  setStatus("");
                  setSource("");
                }}
                disabled={busy}
              >
                <ListFilter aria-hidden="true" />
                清除筛选
              </button>
            )}
          </div>
          {!items.length ? (
            picked && !picked.connected ? (
              <StatePanel kind="unconfigured" title="这个来源还没有接入">
                <p>{sourceDetail[picked.id]}</p>
              </StatePanel>
            ) : unreadable.length === connected.length && connected.length ? (
              <StatePanel kind="error" title="操作台账暂时读不到">
                <p>
                  责任模块自己的记录无法读取，因此这里不显示任何记录，也不把空白当作没有操作。
                </p>
              </StatePanel>
            ) : filtering ? (
              <StatePanel kind="empty" title="当前筛选没有记录">
                <p>
                  已记录的 {knownRecords}{" "}
                  条操作都不符合当前筛选，清除筛选可以看到全部。
                </p>
              </StatePanel>
            ) : (
              <StatePanel kind="empty" title="还没有任何平台操作记录">
                <p>
                  平台还没有发布过模型配置，也没有执行过设备控制。这里只显示真实发生并被记录的操作，
                  不会用占位记录填充。
                </p>
              </StatePanel>
            )
          ) : (
            <div className="tasks-list" ref={list}>
              {items.map((item) => {
                const state = rail(statusRail, item.status);
                const origin = rail(originWording, item.source_state);
                const expanded = open === item.task_id;
                return (
                  <article
                    className={`tasks-item${item.attention ? " attention" : ""}`}
                    key={item.task_id}
                  >
                    <div className="tasks-item-head">
                      <div>
                        <h3>{item.title}</h3>
                        <p className="muted">
                          {sourceWording[item.source] ?? item.source} ·{" "}
                          {kindWording[item.kind] ?? item.kind} · {item.target}
                        </p>
                      </div>
                      <StatusRail tone={state.tone} label={state.label} />
                    </div>
                    <dl className="tasks-facts">
                      <div>
                        <dt>阶段</dt>
                        <dd>{stageWording[item.stage] ?? item.stage}</dd>
                      </div>
                      <div>
                        <dt>记录时间</dt>
                        <dd>{stamp(item.created_at)}</dd>
                      </div>
                      <div>
                        <dt>更新时间</dt>
                        <dd>
                          {stamp(item.updated_at)}
                          {item.pending ? "（受理后尚未结案）" : ""}
                        </dd>
                      </div>
                      <div>
                        <dt>记录来源</dt>
                        <dd>{origin.label}</dd>
                      </div>
                      <div>
                        <dt>结果依据</dt>
                        <dd>
                          {statusNote[item.status] ?? "结果无法确认。"}
                          {item.code ? `（${item.code}）` : ""}
                        </dd>
                      </div>
                    </dl>
                    <div className="tasks-item-actions">
                      <button
                        className="button"
                        data-task-toggle={item.task_id}
                        aria-expanded={expanded}
                        onClick={() => void openDetail(item.task_id)}
                      >
                        {expanded ? "收起详情" : "查看详情"}
                      </button>
                      {!item.cancel.supported && item.pending && (
                        <span className="tasks-chip">不可取消</span>
                      )}
                    </div>
                    {expanded && (
                      <div
                        className="tasks-detail"
                        role="region"
                        aria-label={`${item.title} 的操作详情`}
                        ref={panel}
                        tabIndex={-1}
                      >
                        {detail?.task_id === item.task_id ? (
                          <>
                            <p className="muted">
                              详情是这一刻的重新读取，不是列表里的旧快照。按 Esc
                              收起并把焦点还给列表。
                            </p>
                            <p className="tasks-cancel">
                              取消：不可用 —{" "}
                              {cancelWording[item.cancel.code] ??
                                "执行方没有声明取消能力。"}
                            </p>
                            <h4>时间线</h4>
                            <dl className="tasks-facts">
                              {Object.entries(detail.timeline).map(
                                ([key, value]) => (
                                  <div key={key}>
                                    <dt>{fieldWording[key] ?? key}</dt>
                                    <dd>{show(value)}</dd>
                                  </div>
                                ),
                              )}
                            </dl>
                            <h4>结果依据</h4>
                            <dl className="tasks-facts">
                              {Object.entries(detail.evidence).map(
                                ([key, value]) => (
                                  <div key={key}>
                                    <dt>{fieldWording[key] ?? key}</dt>
                                    <dd>{show(value)}</dd>
                                  </div>
                                ),
                              )}
                            </dl>
                            <h4>请求内容</h4>
                            <dl className="tasks-facts">
                              {Object.entries(detail.request).map(
                                ([key, value]) => (
                                  <div key={key}>
                                    <dt>{fieldWording[key] ?? key}</dt>
                                    <dd>{show(value)}</dd>
                                  </div>
                                ),
                              )}
                            </dl>
                            <p className="muted">
                              标识 {detail.task_id} · 原始责任模块{" "}
                              {sourceWording[detail.source] ?? detail.source}
                            </p>
                            {detail.module.page ? (
                              <p className="tasks-module">
                                <a className="button" href={detail.module.page}>
                                  到责任模块页面查看
                                </a>
                              </p>
                            ) : null}
                          </>
                        ) : (
                          <p className="muted">正在重新读取这条记录…</p>
                        )}
                      </div>
                    )}
                  </article>
                );
              })}
            </div>
          )}
          {page?.has_more && (
            <div className="tasks-more">
              <button
                className="button"
                onClick={() => void more()}
                disabled={busy}
              >
                加载更多（已显示 {items.length} 条）
              </button>
              <p className="muted">
                继续读取的游标绑定当前排序与筛选；期间新增的记录比游标更新，会在下一次读取时出现。
              </p>
            </div>
          )}
        </>
      )}
    </section>
  );
}
