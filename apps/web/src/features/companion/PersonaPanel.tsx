import { LoginLink, useSessionGuard } from "../../app/Auth";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, RefreshCw } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import {
  conflictCodes,
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
import PersonaAuthor from "./PersonaAuthor";
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

/** What the detail pane is showing: one character and one history kind, nothing else. */
type Scope = { subject: string; kind: HistoryKind };

/**
 * What one read was started under: the page's scope generation and the session it belongs to.
 *
 * Every side effect of a read - the answer, every word of a failure, the session it re-reads - has
 * to be checked against this first. A failure needs that proof exactly as much as a success does:
 * a request that was refused after the operator left its scope must not empty, forbid or log out
 * the page that is on screen now.
 */
type Mark = Scope & { token: number; session: string };

/** One session's identity for this page: a different login, or none at all, is a different page. */
function sessionKey(state: SessionState | null) {
  return state?.authenticated ? `session:${state.csrf}` : "anonymous";
}

export default function PersonaPanel() {
  const [session, setSession] = useState<SessionState | null>(null);
  useSessionGuard(session);
  const [unreachable, setUnreachable] = useState("");
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [stale, setStale] = useState<{ code: string; from: Read } | null>(null);
  const [notice, setNotice] = useState("");
  const [authorOpen, setAuthorOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(true);
  const [catalog, setCatalog] = useState<CatalogPage | null>(null);
  const [cursors, setCursors] = useState<(string | null)[]>([null]);
  const [page, setPage] = useState(0);
  const [absent, setAbsent] = useState("");
  const [scope, setScope] = useState<Scope>({ subject: "", kind: "revisions" });
  /**
   * How many times a scope has been asked for.
   *
   * The read effect is keyed on the scope *and* on this counter, so asking for the scope that is
   * already open is a real re-read rather than a cleared panel with nothing to trigger the read.
   */
  const [attempt, setAttempt] = useState(0);
  const [pages, setPages] = useState<HistoryPage[]>([]);
  const [current, setCurrent] = useState(0);
  const [historyFailure, setHistoryFailure] = useState("");
  const [baseline, setBaseline] = useState<string | null>(null);
  const [revision, setRevision] = useState<RevisionView | null>(null);
  const [comparison, setComparison] = useState<CompareView | null>(null);
  const [versionGone, setVersionGone] = useState("");
  const active = useRef<AbortController | null>(null);
  /** The scope's own history read, cancelled when the scope changes and by nothing else. */
  const scoped = useRef<AbortController | null>(null);
  const live = useRef<SessionState | null>(null);
  /**
   * The scope every read is stamped with, and its generation.
   *
   * A reply is applied only when both still match what the read started under: a character or a
   * kind that was switched away, and a character that was switched to and then away again, are the
   * same case here - the answer belongs to a page this operator is no longer looking at.
   */
  const scopeRef = useRef<Scope>({ subject: "", kind: "revisions" });
  const generation = useRef(0);
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

  /**
   * Begin this page's current operator action: the previous one is abandoned, never awaited.
   *
   * Only the actions the operator asks for share this one slot. The history read that follows a
   * scope change keeps a slot of its own: it is a consequence of the scope, and letting it abort
   * this slot would cancel the very refresh that changed the scope (the directory read would then
   * never be sent, and the panel would quietly keep showing the previous page).
   */
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
    setHistoryFailure("");
    setRevision(null);
    setComparison(null);
    setVersionGone("");
    setBaseline(null);
    setStale(null);
  }, []);

  /**
   * Enter a scope: this character and this kind, from this moment.
   *
   * The previous scope's rows, body, comparison, baseline and error are cleared in the same turn as
   * the switch, so nothing of the old scope can be read as the new one's result - not while the new
   * read is running and not afterwards, because the generation it was stamped with is gone.
   *
   * Clicking the character that is already open is the same request as clicking another one - "show
   * me this character's history" - so it opens that history again instead of emptying the panel: an
   * attempt counter the read effect depends on is what makes an unchanged scope ask again. Without
   * it, the cleared page would have nothing left to trigger a re-read and the operator would be
   * looking at an empty panel that no click can refill.
   */
  const enter = useCallback((subject: string, kind: HistoryKind) => {
    generation.current += 1;
    scopeRef.current = { subject, kind };
    setScope({ subject, kind });
    setPages([]);
    setCurrent(0);
    setHistoryFailure("");
    setRevision(null);
    setComparison(null);
    setVersionGone("");
    setBaseline(null);
    setNotice("");
    setStale(null);
    setError("");
    setAttempt((count) => count + 1);
  }, []);

  /** The scope a read is running under, captured before its first byte leaves. */
  const mark = useCallback(
    (): Mark => ({
      token: generation.current,
      subject: scopeRef.current.subject,
      kind: scopeRef.current.kind,
      session: sessionKey(live.current),
    }),
    [],
  );

  /**
   * Is this read still this page's own read?
   *
   * Asked before every side effect a read can have, including the ones inside `guard`: the answer,
   * each error word, the cleared panel and the re-read session. A read whose scope or whose session
   * has been replaced is not this page's read any more, so it may neither fill nor clear it - the
   * operator has already moved on to another character, another kind or another login.
   */
  const stillMine = useCallback(
    (signal: AbortSignal, started: Mark) =>
      !signal.aborted &&
      started.token === generation.current &&
      started.session === sessionKey(live.current),
    [],
  );

  /**
   * One read that always ends in a stated state.
   *
   * A conflict is never retried in a loop: the page says the position or the persona version is
   * stale and offers to open the first page again, because only the operator knows whether the
   * newer list is what they wanted. A deployment that cannot read at all is stated as that, and an
   * empty directory is only ever reported when the server really returned zero rows.
   *
   * Nothing here touches the page before `stillMine` says the read is still the current one: a
   * refusal that arrives after the operator switched character, switched kind, logged out or logged
   * in again belongs to a page that no longer exists, and the panel showing beta (or the new
   * session) must survive it untouched. A refusal of a read that *is* still current - a revoked
   * action, an expired session - still clears the page, because that is a true statement about it.
   */
  const guard = useCallback(
    async <T,>(
      signal: AbortSignal,
      from: Read,
      started: Mark,
      work: (state: SessionState) => Promise<T>,
    ): Promise<{ answer: T | null; failure: string }> => {
      const state = live.current;
      if (!state) return { answer: null, failure: "" };
      if (stillMine(signal, started)) setStale(null);
      try {
        return { answer: await work(state), failure: "" };
      } catch (cause) {
        if (!stillMine(signal, started)) return { answer: null, failure: "" };
        if (!(cause instanceof PersonaError)) {
          const text = reason(cause);
          setError(text);
          return { answer: null, failure: text };
        }
        if (cause.code in deploymentState) {
          setAbsent(cause.code);
          setError("");
          forget();
          return { answer: null, failure: "" };
        }
        if (conflictCodes.includes(cause.code)) {
          setStale({ code: cause.code, from });
          setError("");
          return { answer: null, failure: reason(cause) };
        }
        if (sessionCodes.includes(cause.code)) {
          // A revoked or expired login is a state of this page, not a stale panel - but a session
          // re-read that comes back after this page has moved on says nothing about it either.
          const fresh = await readSession(signal).catch(() => null);
          if (!stillMine(signal, started)) return { answer: null, failure: "" };
          applySession(fresh ?? null);
          forget();
          setError(cause.message);
          return { answer: null, failure: cause.message };
        }
        const text = reason(cause);
        setError(text);
        return { answer: null, failure: text };
      }
    },
    [applySession, forget, stillMine],
  );

  /**
   * One directory page. The position is the server's own opaque cursor, kept verbatim, and earlier
   * pages are remembered only so the operator can step back without asking for a guess.
   */
  const loadCatalog = useCallback(
    async (signal: AbortSignal, position: number, cursor: string | null) => {
      const started = mark();
      setBusy(true);
      setError("");
      const { answer } = await guard(signal, "catalog", started, (state) =>
        read<CatalogPage>("catalog", { cursor }, state.csrf, signal),
      );
      if (!stillMine(signal, started)) return;
      if (answer) {
        setCatalog(answer);
        setAbsent("");
        setPage(position);
        setCursors((known) => [...known.slice(0, position), cursor]);
        // A directory that no longer holds the chosen character starts on one it does hold, and
        // that is a scope change like any other: the old character's rows and body are dropped.
        const first =
          answer.entries.find((entry) => !entry.error) ?? answer.entries[0];
        const chosen = scopeRef.current.subject;
        if (!answer.entries.some((entry) => entry.subject === chosen)) {
          if (first) enter(first.subject, scopeRef.current.kind);
        }
      }
      setBusy(false);
    },
    [enter, guard, mark, stillMine],
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
      const started = mark();
      setBusy(true);
      setError("");
      setNotice("");
      const { answer, failure } = await guard(
        signal,
        "history",
        started,
        (state) =>
          read<HistoryPage>(
            "history",
            { subject: started.subject, kind: started.kind, cursor },
            state.csrf,
            signal,
          ),
      );
      if (!stillMine(signal, started)) return;
      if (answer) {
        setPages((loaded) => [...loaded.slice(0, position), answer]);
        setCurrent(position);
        setHistoryFailure("");
      } else {
        // This scope's own failure: the pane says this character and this kind have no result,
        // and the rows of whatever was shown before are already gone.
        setPages([]);
        setCurrent(0);
        setHistoryFailure(failure);
      }
      setBusy(false);
    },
    [guard, mark, stillMine],
  );

  useEffect(() => {
    if (!session?.authenticated || !scope.subject || absent) return;
    // Its own controller, so entering a scope cancels the previous scope's history read without
    // cancelling the directory read that entered it.
    scoped.current?.abort();
    const controller = new AbortController();
    scoped.current = controller;
    void loadHistory(controller.signal, 0, null);
    return () => controller.abort();
  }, [session, scope.subject, scope.kind, attempt, absent, loadHistory]);

  const refuse = useCallback((id: string, extra: string) => {
    setRevision(null);
    setComparison(null);
    setVersionGone(`${extra}版本 ${shortDigest(id)} 读不到。`);
  }, []);

  async function openRevision(revisionId: string) {
    const started = mark();
    const controller = start();
    setBusy(true);
    setError("");
    setNotice("");
    const { answer } = await guard(
      controller.signal,
      "revision",
      started,
      (state) =>
        read<RevisionView>(
          "revision",
          { subject: started.subject, revision_id: revisionId },
          state.csrf,
          controller.signal,
        ),
    );
    if (!stillMine(controller.signal, started)) return;
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
    const started = mark();
    const controller = start();
    setBusy(true);
    setError("");
    setNotice("");
    const { answer } = await guard(
      controller.signal,
      "compare",
      started,
      (state) =>
        read<CompareView>(
          "compare",
          { subject: started.subject, left: baseline, right: revisionId },
          state.csrf,
          controller.signal,
        ),
    );
    if (!stillMine(controller.signal, started)) return;
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

  /** A stale position is reopened where it was raised: the directory or the history. */
  function reopen() {
    const controller = start();
    if (stale?.from === "catalog") void loadCatalog(controller.signal, 0, null);
    else void loadHistory(controller.signal, 0, null);
  }

  const chosen =
    catalog?.entries.find((entry) => entry.subject === scope.subject) ?? null;
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
      aria-label="人格版本"
      aria-busy={busy}
    >
      <div className="persona-toolbar">
        <div>
          <p className="eyebrow">角色服务 · 人格管理</p>
          <h2 ref={heading} tabIndex={-1}>
            人格版本
          </h2>
          <p className="muted persona-intro">
            创建可复用档案，编辑已有角色，保存草稿并明确应用。下方保留版本历史与差异查看。
          </p>
        </div>
        <div className="persona-actions">
          {session?.authenticated && (
            <button
              type="button"
              className="button primary"
              onClick={() => {
                setAuthorOpen(true);
                setHistoryOpen(false);
              }}
            >
              创建与编辑
            </button>
          )}
          <button
            type="button"
            className="button"
            disabled={busy}
            onClick={() => void connect()}
          >
            <RefreshCw aria-hidden="true" />
            重新读取
          </button>
        </div>
      </div>

      {authorOpen && session?.authenticated && (
        <PersonaAuthor session={session} onApplied={() => void connect()} />
      )}

      <details
        className="persona-read-details"
        open={historyOpen}
        onToggle={(event) => setHistoryOpen(event.currentTarget.open)}
      >
        <summary>版本历史与差异</summary>
        <div className="persona-read-body">
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
            <LoginLink />
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
                <h3 id="persona-subject-label">角色目录</h3>
                <ul
                  className="persona-subjects"
                  aria-labelledby="persona-subject-label"
                >
                  {catalog.entries.map((entry) => (
                    <li key={entry.subject}>
                      <button
                        type="button"
                        className="persona-subject"
                        aria-pressed={entry.subject === scope.subject}
                        // Choosing a character is always possible: it starts a new scope and abandons
                        // the read it replaces instead of waiting for it.
                        onClick={() =>
                          enter(entry.subject, scopeRef.current.kind)
                        }
                      >
                        <span className="persona-subject-id">
                          {entry.subject}
                        </span>
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
                <label className="persona-picker">
                  角色目录
                  <select
                    value={scope.subject}
                    onChange={(event) =>
                      enter(event.target.value, scopeRef.current.kind)
                    }
                  >
                    {catalog.entries.map((entry) => (
                      <option key={entry.subject} value={entry.subject}>
                        {entry.error
                          ? `${entry.subject} · ${rowErrorLabel(entry.error)}`
                          : `${entry.subject} · ${stateLabel(entry.state)} · 版本 ${entry.version ?? "未知"}`}
                      </option>
                    ))}
                  </select>
                </label>
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
                <section
                  className="persona-pointer"
                  aria-label="当前发布与草稿指针"
                >
                  <h3>当前发布与草稿</h3>
                  {!chosen ? (
                    <p className="muted">还没有选中角色。</p>
                  ) : chosen.error ? (
                    <p className="muted">
                      {`${rowErrorLabel(chosen.error)}：这个角色没有可显示的指针。`}
                    </p>
                  ) : (
                    <dl className="persona-facts">
                      <div>
                        <dt>状态</dt>
                        <dd data-pointer="state">{stateLabel(chosen.state)}</dd>
                      </div>
                      <div>
                        <dt>版本</dt>
                        <dd data-pointer="version">
                          {chosen.version ?? "未知"}
                        </dd>
                      </div>
                      <div>
                        <dt>当前发布</dt>
                        <dd data-pointer="published">
                          {shortDigest(chosen.published_revision)}
                        </dd>
                      </div>
                      <div>
                        <dt>当前草稿</dt>
                        <dd data-pointer="draft">
                          {shortDigest(chosen.draft_revision)}
                        </dd>
                      </div>
                      <div>
                        <dt>更新</dt>
                        <dd data-pointer="updated">
                          {shortTime(chosen.updated_at)}
                        </dd>
                      </div>
                    </dl>
                  )}
                </section>
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
                <PersonaHistory
                  kind={scope.kind}
                  subject={scope.subject}
                  pages={pages}
                  current={current}
                  busy={busy}
                  baseline={baseline}
                  failure={historyFailure}
                  onKind={(next) => enter(scopeRef.current.subject, next)}
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
              </div>
            </div>
          )}
          <p className="muted persona-foot">
            {`历史类别共 ${HISTORY_KINDS.length} 类，每页固定 ${catalog?.page ?? 20} 条，续读位置由本页签发。历史区仅供查看。`}
          </p>
        </div>
      </details>
    </section>
  );
}
