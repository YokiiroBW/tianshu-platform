import { useCallback, useEffect, useRef, useState } from "react";
import {
  ChevronLeft,
  ChevronRight,
  LockKeyhole,
  LogOut,
  RefreshCw,
} from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import {
  conflictCodes,
  login as submitLogin,
  logout as submitLogout,
  read,
  reason,
  session as readSession,
  sessionCodes,
  word,
  PersonaError,
  type SessionState,
} from "./personaApi";
import { PersonaHistory } from "./PersonaHistory";
import { PersonaRevision } from "./PersonaRevision";
import {
  HISTORY_KINDS,
  rowErrorLabel,
  shortDigest,
  shortTime,
  stateLabel,
  type CatalogPage,
  type CompareView,
  type HistoryKind,
  type HistoryPage,
  type RevisionView,
} from "./personaTypes";
import "./personas.css";

/** The three deployment/permission words the server may send, and this page's word for each. */
const deploymentState: Record<string, string> = {
  personas_not_configured: "未配置",
  personas_disabled: "未启用",
  persona_read_required: "未授权",
};

const conflictLabel: Record<string, string> = {
  cursor_conflict: "续读位置已过期",
  version_conflict: "版本已过期",
};

type Read = "catalog" | "history" | "revision" | "compare";

export default function PersonaPanel() {
  const [session, setSession] = useState<SessionState | null>(null);
  const [unreachable, setUnreachable] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [stale, setStale] = useState<{ code: string; from: Read } | null>(null);
  const [notice, setNotice] = useState("");
  const [catalog, setCatalog] = useState<CatalogPage | null>(null);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);
  const [absent, setAbsent] = useState("");
  const [subject, setSubject] = useState("");
  const [kind, setKind] = useState<HistoryKind>("revisions");
  const [pages, setPages] = useState<HistoryPage[]>([]);
  const [current, setCurrent] = useState(0);
  const [baseline, setBaseline] = useState<string | null>(null);
  const [revision, setRevision] = useState<RevisionView | null>(null);
  const [comparison, setComparison] = useState<CompareView | null>(null);
  const [versionGone, setVersionGone] = useState("");
  const active = useRef<AbortController | null>(null);
  const live = useRef<SessionState | null>(null);
  const password = useRef<HTMLInputElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    live.current = session;
  }, [session]);

  /**
   * The session every read must use, updated before the next render.
   *
   * Logging in replaces the session (and its CSRF token), so a read that started right after
   * `setSession` would still send the previous token and be refused: the ref is written here, in
   * the same turn as the state, not in an effect that runs after it.
   */
  const applySession = useCallback((next: SessionState | null) => {
    live.current = next;
    setSession(next);
  }, []);

  const start = useCallback(() => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    return controller;
  }, []);

  const forget = useCallback(() => {
    setCatalog(null);
    setCursors([null]);
    setPage(0);
    setPages([]);
    setCurrent(0);
    setRevision(null);
    setComparison(null);
    setVersionGone("");
    setStale(null);
  }, []);

  /**
   * One read that always ends in a stated state.
   *
   * A conflict is never retried in a loop: the page says the position or the persona version is
   * stale and offers to open the first page again, because only the operator knows whether the
   * newer list is what they wanted. A deployment that cannot read at all is stated as that, and an
   * empty directory is only ever reported when the server really returned zero rows.
   */
  const guard = useCallback(
    async <T,>(
      signal: AbortSignal,
      from: Read,
      work: (state: SessionState) => Promise<T>,
    ): Promise<T | null> => {
      const state = live.current;
      if (!state) return null;
      setStale(null);
      try {
        return await work(state);
      } catch (cause) {
        if (signal.aborted) return null;
        if (!(cause instanceof PersonaError)) {
          setError(reason(cause));
          return null;
        }
        if (cause.code in deploymentState) {
          setAbsent(cause.code);
          setError("");
          forget();
          return null;
        }
        if (conflictCodes.includes(cause.code)) {
          setStale({ code: cause.code, from });
          setError("");
          return null;
        }
        if (sessionCodes.includes(cause.code)) {
          // A revoked or expired login is a state of this page, not a stale panel.
          const fresh = await readSession(signal).catch(() => null);
          applySession(fresh?.authenticated ? fresh : null);
          forget();
          setError(cause.message);
          return null;
        }
        setError(reason(cause));
        return null;
      }
    },
    [applySession, forget],
  );

  /**
   * One directory page. The position is the server's own opaque cursor, kept verbatim, and earlier
   * pages are remembered only so the operator can step back without asking for a guess.
   */
  const loadCatalog = useCallback(
    async (signal: AbortSignal, position: number, cursor: string | null) => {
      setBusy(true);
      setError("");
      const answer = await guard(signal, "catalog", (state) =>
        read<CatalogPage>("catalog", { cursor }, state.csrf, signal),
      );
      if (signal.aborted) return;
      if (answer) {
        setCatalog(answer);
        setAbsent("");
        setPage(position);
        setCursors((known) => [...known.slice(0, position), cursor]);
        setSubject((chosen) =>
          answer.entries.some((entry) => entry.subject === chosen)
            ? chosen
            : (
                answer.entries.find((entry) => !entry.error) ??
                answer.entries[0]
              )?.subject || "",
        );
      }
      setBusy(false);
    },
    [guard],
  );

  const connect = useCallback(async () => {
    const controller = start();
    setBusy(true);
    setUnreachable("");
    setError("");
    try {
      const state = await readSession(controller.signal);
      const had = live.current?.authenticated === true;
      applySession(state);
      if (!state.authenticated) {
        // A session that was here a moment ago and is gone now is stated, not silently emptied.
        if (had) setError(word("session_expired"));
        forget();
        return;
      }
      await loadCatalog(controller.signal, 0, null);
    } catch (cause) {
      if (!controller.signal.aborted) {
        applySession(null);
        setUnreachable(reason(cause));
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }, [applySession, forget, loadCatalog, start]);

  useEffect(() => {
    void connect();
    window.addEventListener("online", connect);
    return () => {
      active.current?.abort();
      window.removeEventListener("online", connect);
    };
  }, [connect]);

  /** One history page at one position, in the same shape as the directory. */
  const loadHistory = useCallback(
    async (signal: AbortSignal, position: number, cursor: string | null) => {
      setBusy(true);
      setError("");
      setNotice("");
      const answer = await guard(signal, "history", (state) =>
        read<HistoryPage>(
          "history",
          { subject, kind, cursor },
          state.csrf,
          signal,
        ),
      );
      if (signal.aborted) return;
      if (answer) {
        setPages((loaded) => [...loaded.slice(0, position), answer]);
        setCurrent(position);
        setRevision(null);
        setComparison(null);
        setVersionGone("");
      }
      setBusy(false);
    },
    [guard, kind, subject],
  );

  useEffect(() => {
    if (!session?.authenticated || !subject || absent) return;
    const controller = start();
    setBaseline(null);
    // Switching character or history kind clears the chosen version at once: the pane must never
    // show one character's revision under another character's name while the new page loads.
    setRevision(null);
    setComparison(null);
    setVersionGone("");
    void loadHistory(controller.signal, 0, null);
    return () => controller.abort();
  }, [session, subject, kind, absent, loadHistory, start]);

  const refuse = useCallback((id: string, extra: string) => {
    setRevision(null);
    setComparison(null);
    setVersionGone(`${extra}版本 ${shortDigest(id)} 读不到。`);
  }, []);

  async function openRevision(revisionId: string) {
    const controller = start();
    setBusy(true);
    setError("");
    setNotice("");
    const answer = await guard(controller.signal, "revision", (state) =>
      read<RevisionView>(
        "revision",
        { subject, revision_id: revisionId },
        state.csrf,
        controller.signal,
      ),
    );
    if (controller.signal.aborted) return;
    if (answer) {
      setRevision(answer);
      setComparison(null);
      setVersionGone("");
      setNotice(`已读取版本 ${shortDigest(revisionId)}。`);
    } else {
      refuse(revisionId, "已选版本不可用：");
    }
    setBusy(false);
  }

  async function compareWith(revisionId: string) {
    if (!baseline) return;
    const controller = start();
    setBusy(true);
    setError("");
    setNotice("");
    const answer = await guard(controller.signal, "compare", (state) =>
      read<CompareView>(
        "compare",
        { subject, left: baseline, right: revisionId },
        state.csrf,
        controller.signal,
      ),
    );
    if (controller.signal.aborted) return;
    if (answer) {
      setComparison(answer);
      setRevision(null);
      setVersionGone("");
      setNotice(
        `已比较 ${shortDigest(baseline)} 与 ${shortDigest(revisionId)}。`,
      );
    } else {
      refuse(revisionId, "无法比较：");
    }
    setBusy(false);
  }

  async function authenticate(form: HTMLFormElement) {
    const state = live.current;
    if (!state) return;
    const data = new FormData(form);
    const controller = start();
    setBusy(true);
    setError("");
    const username = String(data.get("username"));
    const secret = String(data.get("password"));
    if (password.current) password.current.value = "";
    try {
      await submitLogin(username, secret, state.csrf, controller.signal);
      await connect();
      heading.current?.focus();
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(reason(cause));
        setBusy(false);
        password.current?.focus();
      }
    }
  }

  async function exit() {
    const state = live.current;
    if (!state) return;
    const controller = start();
    setBusy(true);
    setError("");
    try {
      await submitLogout(state.csrf, controller.signal);
      applySession(null);
      forget();
      await connect();
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(`退出未确认：${reason(cause)}`);
        setBusy(false);
      }
    }
  }

  /** A stale position is reopened where it was raised: the directory or the history. */
  function reopen() {
    const controller = start();
    if (stale?.from === "catalog") void loadCatalog(controller.signal, 0, null);
    else void loadHistory(controller.signal, 0, null);
  }

  const chosen = catalog?.entries.find((entry) => entry.subject === subject);
  const empty = !!catalog && catalog.count === 0;
  const status = unreachable
    ? "读取失败"
    : error
      ? "读取失败"
      : absent
        ? (deploymentState[absent] ?? "不可用")
        : busy
          ? "读取中"
          : stale
            ? (conflictLabel[stale.code] ?? "已过期")
            : catalog
              ? "可读"
              : session?.authenticated
                ? "尚未读取"
                : "未登录";

  return (
    <section
      className="persona-page glass"
      aria-label="人格目录与版本"
      aria-busy={busy}
    >
      <div className="persona-toolbar">
        <div>
          <p className="eyebrow">只读 · 角色服务</p>
          <h2 ref={heading} tabIndex={-1}>
            人格目录与版本
          </h2>
        </div>
        <div className="persona-actions">
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={() => void connect()}
          >
            <RefreshCw aria-hidden="true" />
            重新读取
          </button>
          {session?.authenticated && (
            <button
              type="button"
              className="button"
              disabled={busy}
              onClick={() => void exit()}
            >
              <LogOut aria-hidden="true" />
              退出登录
            </button>
          )}
        </div>
      </div>

      <div className="persona-state">
        <StatusRail
          tone={
            unreachable || error || absent
              ? "red"
              : busy || stale
                ? "yellow"
                : "blue"
          }
          label={status}
        >
          <p>
            {unreachable
              ? unreachable
              : error
                ? error
                : absent
                  ? `${absent}：这个部署或这个账号不能读取人格。`
                  : catalog
                    ? `已授权角色 ${catalog.subjects} 个 · 每页 ${catalog.page} 个`
                    : "登录后只显示本部署登记的角色。"}
          </p>
          {!!error && !!catalog && (
            // Rows left over from an earlier successful read are never passed off as this read.
            <p>下面显示的目录是上一次成功读取的结果，不是这一次的结果。</p>
          )}
        </StatusRail>
      </div>

      {notice && (
        <p className="persona-notice" role="status">
          {notice}
        </p>
      )}

      {!session?.authenticated ? (
        <div className="persona-login">
          <LockKeyhole aria-hidden="true" />
          <h3>从自己的账号开始</h3>
          <p className="muted">
            登录后只读取本部署允许的角色；这里不会显示任何服务凭据或地址。
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
                disabled={!session || busy}
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
                disabled={!session || busy}
              />
            </label>
            <button
              type="submit"
              className="button primary"
              disabled={!session || busy}
            >
              登录
            </button>
          </form>
        </div>
      ) : absent ? (
        <StatePanel
          kind="unconfigured"
          title={deploymentState[absent] ?? "不可用"}
        >
          <p>{absent}</p>
          <p>
            人格页需要部署登记一个角色服务、一份显式角色名单和读取权限，三者缺一都不会显示角色。
          </p>
        </StatePanel>
      ) : empty ? (
        <StatePanel kind="empty" title="目录为空">
          <p>这个部署登记了 0 个角色，因此没有可读的目录。</p>
        </StatePanel>
      ) : !catalog ? (
        // No directory at all is a state of the read, never an empty list: the panel says which.
        busy ? (
          <StatePanel kind="loading" title="正在读取目录">
            <p>正在向本部署登记的角色服务读取目录。</p>
          </StatePanel>
        ) : (
          <StatePanel kind="error" title="目录读取失败">
            <p>{error || unreachable || "目录没有读到。"}</p>
            <p>没有任何角色被显示出来，因为这一页确实没有读到目录。</p>
          </StatePanel>
        )
      ) : (
        <div className="persona-layout">
          <div className="persona-catalog">
            <h3>角色目录</h3>
            <ul className="persona-subjects">
              {catalog.entries.map((entry) => (
                <li key={entry.subject}>
                  <button
                    type="button"
                    className="persona-subject"
                    aria-pressed={entry.subject === subject}
                    disabled={busy}
                    onClick={() => setSubject(entry.subject)}
                  >
                    <span className="persona-subject-id">{entry.subject}</span>
                    <span className="persona-subject-meta">
                      {entry.error
                        ? rowErrorLabel(entry.error)
                        : `${stateLabel(entry.state)} · 版本 ${entry.version ?? "未知"}`}
                    </span>
                    <span className="persona-subject-time">
                      {entry.error
                        ? "没有可显示的指针"
                        : `更新 ${shortTime(entry.updated_at)}`}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <div className="persona-pager">
              <p className="muted" role="status">
                {busy
                  ? "正在读取目录…"
                  : `第 ${page + 1} 页 · 本页 ${catalog.entries.length} 个角色 · 共 ${catalog.subjects} 个`}
              </p>
              <div className="persona-pager-actions">
                <button
                  type="button"
                  className="button"
                  disabled={busy || page === 0}
                  onClick={() =>
                    void loadCatalog(
                      start().signal,
                      page - 1,
                      cursors[page - 1] ?? null,
                    )
                  }
                >
                  <ChevronLeft aria-hidden="true" />
                  上一页角色
                </button>
                <button
                  type="button"
                  className="button"
                  disabled={busy || !catalog.has_more}
                  onClick={() =>
                    void loadCatalog(
                      start().signal,
                      page + 1,
                      catalog.next_cursor,
                    )
                  }
                >
                  下一页角色
                  <ChevronRight aria-hidden="true" />
                </button>
              </div>
            </div>
          </div>
          <div className="persona-detail">
            {stale && (
              <StatusRail
                tone="yellow"
                label={conflictLabel[stale.code] ?? "已过期"}
              >
                <p>
                  {stale.code === "cursor_conflict"
                    ? "这一页的续读位置由本页签发，已经不再对应当前会话。"
                    : "人格在这期间已经变化，旧的一页不再代表同一批记录。"}
                  这里不会自动翻页，请重新打开第一页。
                </p>
                <button
                  type="button"
                  className="button"
                  disabled={busy}
                  onClick={reopen}
                >
                  重新打开第一页
                </button>
              </StatusRail>
            )}
            {chosen?.error ? (
              <StatusRail tone="red" label="这个角色读不到">
                <p>
                  {rowErrorLabel(chosen.error)}
                  {`：角色服务对 ${chosen.subject} 的回答不可用，这里不会显示成空历史。`}
                </p>
              </StatusRail>
            ) : (
              <>
                <PersonaHistory
                  kind={kind}
                  subject={subject}
                  pages={pages}
                  current={current}
                  busy={busy}
                  baseline={baseline}
                  onKind={setKind}
                  onPrevious={() =>
                    setCurrent((index) => Math.max(0, index - 1))
                  }
                  onNext={() => {
                    const known = pages[current + 1];
                    const next = pages[current]?.next_cursor ?? null;
                    if (known) setCurrent(current + 1);
                    else if (next) {
                      void loadHistory(start().signal, current + 1, next);
                    }
                  }}
                  onRestart={() => void loadHistory(start().signal, 0, null)}
                  onSelect={(id) => void openRevision(id)}
                  onBaseline={(id) => {
                    setBaseline(id);
                    setNotice(`已把 ${shortDigest(id)} 设为对比基线。`);
                  }}
                  onCompare={(id) => void compareWith(id)}
                />
                <PersonaRevision
                  revision={revision}
                  comparison={comparison}
                  busy={busy}
                  unavailable={versionGone}
                />
              </>
            )}
          </div>
        </div>
      )}
      <p className="muted persona-foot">
        {`历史类别共 ${HISTORY_KINDS.length} 类，每页固定 ${catalog?.page ?? 20} 条，续读位置由本页签发。这里只读取，不修改、不批准、不发布、不回退。`}
      </p>
    </section>
  );
}
