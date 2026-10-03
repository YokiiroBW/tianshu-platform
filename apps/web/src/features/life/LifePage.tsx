import { useEffect, useRef, useState } from "react";
import { RefreshCw, Sparkles, BookOpen } from "lucide-react";
import { useAuth } from "../../app/Auth";
import { integrationPost, readFailure } from "../../app/integrationApi";
import { StatePanel } from "../../components/StatePanel";
import { DailyLife } from "./DailyLife";
import "./life.css";

type Connection = {
  can_retry?: boolean;
  available: boolean;
  code: string;
  peer: { configured: boolean; verified_at: string | null; code: string };
};
type Actor = {
  actor_id: string;
  label?: string;
  actor_version: number;
  world_id: string;
  room_id: string;
};
type Actors = {
  schema_version: number;
  fictional: true;
  items: Actor[];
  next_after_actor_id: string | null;
};
type Snapshot = {
  actor_id: string;
  actor_version: number;
  world_id: string;
  room_id: string;
  timezone: string;
  activity: string | null;
  mood: string;
  outfit_ref: string | null;
  changed_at: number;
  observed_at: number;
  state_basis: "last_persisted";
  fictional: true;
};
type Diary = {
  diary_id: string;
  actor_id: string;
  day: string;
  state: string;
  version: number;
  published_revision_id: string;
  captured: {
    recipe_id: string;
    recipe_version: number;
    material_version: string;
  };
};
type Diaries = {
  actor_id: string;
  items: Diary[];
  next_after: { day: string; diary_id: string } | null;
  fictional: true;
};
type Revision = {
  diary_id: string;
  diary_version: number;
  revision_id: string;
  actor_id: string;
  content: string;
  created_at: number;
  captured: {
    recipe_id: string;
    recipe_version: number;
    material_version: string;
    config_version: number;
  };
  fictional: true;
};

function dateText(value: number) {
  return Number.isFinite(value)
    ? new Date(value * 1000).toLocaleString("zh-CN")
    : "没有可靠时间";
}

function unavailableTitle(code: string) {
  if (code.endsWith("_read_required")) return "当前账号未获角色生活读取权限";
  if (code.endsWith("_credential_missing")) return "角色生活服务凭据缺失";
  return "角色生活尚未配置";
}

/** Read-only view of persisted fictional life and the published diary pointer. */
export default function LifePage() {
  const { session } = useAuth();
  const csrf = session?.authenticated ? session.csrf : "";
  const [state, setState] = useState<Connection | null>(null);
  const [actors, setActors] = useState<Actor[]>([]);
  const [actorCursor, setActorCursor] = useState<string | null>(null);
  const [actor, setActor] = useState("");
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [diaries, setDiaries] = useState<Diary[]>([]);
  const [diaryCursor, setDiaryCursor] = useState<Diaries["next_after"]>(null);
  const [selected, setSelected] = useState<Diary | null>(null);
  const [revision, setRevision] = useState<Revision | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stateError, setStateError] = useState("");
  const active = useRef<AbortController | null>(null);
  const generation = useRef(0);

  function start() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    return controller;
  }

  async function loadState() {
    if (!csrf) return;
    const controller = start();
    generation.current++;
    setState(null);

    setStateError("");
    setActors([]);
    setActorCursor(null);
    setActor("");
    setSnapshot(null);
    setDiaries([]);
    setSelected(null);
    setRevision(null);
    try {
      const next = await integrationPost<Connection>(
        "life/state",
        {},
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) setState(next);
    } catch (cause) {
      if (!controller.signal.aborted) setStateError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    generation.current++;
    setActors([]);
    setActorCursor(null);
    setActor("");
    setSnapshot(null);
    setDiaries([]);
    setSelected(null);
    setRevision(null);
    void loadState();
    return () => active.current?.abort();
  }, [csrf]);

  async function listActors(after_actor_id: string | null = null) {
    if (!csrf) return;
    const controller = start();
    const mark = generation.current;
    if (!after_actor_id) {
      setActors([]);
      setActorCursor(null);
    }
    try {
      const answer = await integrationPost<Actors>(
        "life/actors",
        { limit: 20, after_actor_id },
        csrf,
        controller.signal,
      );
      if (controller.signal.aborted || generation.current !== mark) return;

      setActors((before) =>
        after_actor_id ? [...before, ...answer.items] : answer.items,
      );
      setActorCursor(answer.next_after_actor_id);
      if (!after_actor_id)
        setActor((before) =>
          answer.items.some((row) => row.actor_id === before)
            ? before
            : (answer.items[0]?.actor_id ?? ""),
        );
    } catch (cause) {
      if (!controller.signal.aborted && generation.current === mark)
        setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    if (state?.available) void listActors();
  }, [state?.available, csrf]);

  async function readActor(id: string) {
    if (!csrf || !id) return;
    const controller = start();
    const mark = generation.current;
    setSnapshot(null);
    setDiaries([]);
    setDiaryCursor(null);
    setSelected(null);
    setRevision(null);
    try {
      const [life, list] = await Promise.all([
        integrationPost<Snapshot>(
          "life/snapshot",
          { actor_id: id },
          csrf,
          controller.signal,
        ),
        integrationPost<Diaries>(
          "life/diaries",
          { actor_id: id, limit: 20, after: null },
          csrf,
          controller.signal,
        ),
      ]);
      if (controller.signal.aborted || generation.current !== mark) return;

      setSnapshot(life);
      setDiaries(list.items);
      setDiaryCursor(list.next_after);
    } catch (cause) {
      if (!controller.signal.aborted && generation.current === mark)
        setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    generation.current++;
    if (actor) active.current?.abort();
    setError("");
    setSnapshot(null);
    setDiaries([]);
    setSelected(null);
    setRevision(null);
    if (actor) void readActor(actor);
  }, [actor, csrf]);

  function changeActor(next: string) {
    if (next === actor) return;
    generation.current++;
    active.current?.abort();
    setSnapshot(null);
    setDiaries([]);
    setDiaryCursor(null);
    setSelected(null);
    setRevision(null);
    setError("");
    setBusy(false);

    setActor(next);
  }

  async function moreDiaries() {
    if (!csrf || !actor || !diaryCursor) return;
    const controller = start();
    const mark = generation.current;
    try {
      const answer = await integrationPost<Diaries>(
        "life/diaries",
        { actor_id: actor, limit: 20, after: diaryCursor },
        csrf,
        controller.signal,
      );
      if (controller.signal.aborted || generation.current !== mark) return;

      setDiaries((before) => [...before, ...answer.items]);
      setDiaryCursor(answer.next_after);
    } catch (cause) {
      if (!controller.signal.aborted && generation.current === mark)
        setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function readRevision(row: Diary) {
    if (!csrf || !actor) return;
    const controller = start();
    const mark = generation.current;
    setSelected(row);
    setRevision(null);
    try {
      const answer = await integrationPost<Revision>(
        "life/revision",
        {
          actor_id: actor,
          diary_id: row.diary_id,
          revision_id: row.published_revision_id,
          expected_diary_version: row.version,
        },
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted && generation.current === mark) {
        setRevision(answer);
      }
    } catch (cause) {
      if (!controller.signal.aborted && generation.current === mark)
        setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  if (!csrf)
    return (
      <StatePanel kind="error" title="请先登录">
        <p>角色生活与日记只对已授权账号开放。</p>
      </StatePanel>
    );
  if (stateError)
    return (
      <StatePanel
        kind="error"
        title="连接状态没有读到"
        action={
          <button className="button" onClick={() => void loadState()}>
            重新读取
          </button>
        }
      >
        <p>{stateError}</p>
      </StatePanel>
    );
  if (!state)
    return (
      <StatePanel kind="loading" title="正在读取生活连接">
        <p>请稍候。</p>
      </StatePanel>
    );
  if (!state.available)
    return (
      <StatePanel
        kind={state.code.endsWith("_not_configured") ? "unconfigured" : "error"}
        title={unavailableTitle(state.code)}
        action={
          <a className="button" href="#/settings/1">
            查看连接状态
          </a>
        }
      >
        <p>
          部署端需要登记生活读取身份和角色授权。网页无需填写服务地址或令牌。
        </p>
        <p>状态码：{state.code}</p>
      </StatePanel>
    );

  return (
    <div className="life-page">
      <header className="life-toolbar">
        <div className="life-identity">
          <span className="life-avatar">
            <Sparkles aria-hidden="true" />
          </span>
          {actors.length > 0 && (
            <label className="life-actor">
              <span className="sr-only">选择角色</span>
              <select
                value={actor}
                onChange={(event) => changeActor(event.target.value)}
              >
                {actors.map((row) => (
                  <option key={row.actor_id} value={row.actor_id}>
                    {row.label || row.actor_id}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>
        <div className="life-page-title">
          <h2>今天的生活</h2>
          <p className="muted">在平凡的日子里，发现值得珍藏的瞬间。</p>
        </div>
        <button
          className="button life-icon-button"
          aria-label="重新读取角色"
          title="重新读取角色"
          disabled={busy}
          onClick={() => void listActors()}
        >
          <RefreshCw aria-hidden="true" />
        </button>
        {actorCursor && (
          <button
            className="button"
            disabled={busy}
            onClick={() => void listActors(actorCursor)}
          >
            继续读取角色
          </button>
        )}
      </header>
      {error && (
        <StatePanel
          kind="error"
          title="本次读取失败"
          action={
            <button
              className="button"
              onClick={() =>
                selected
                  ? void readRevision(selected)
                  : actor
                    ? void readActor(actor)
                    : void listActors()
              }
            >
              重新读取
            </button>
          }
        >
          <p>{error}</p>
          <p>失败没有被当作空生活状态或空日记。</p>
        </StatePanel>
      )}
      {!busy && !error && actors.length === 0 && (
        <StatePanel kind="empty" title="没有可读角色">
          <p>服务成功返回了空的授权角色列表。这不表示其它角色不存在。</p>
        </StatePanel>
      )}
      {actor && (
        <>
          <DailyLife
            key={`${csrf}:${actor}`}
            actor={actor}
            csrf={csrf}
            canRetry={state.can_retry === true}
            snapshot={snapshot?.actor_id === actor ? snapshot : null}
          />
          <details id="life-diaries" className="life-diary-section">
            <summary>
              <BookOpen aria-hidden="true" /> 生活日记{" "}
              <span className="muted">
                {diaries.length
                  ? `${diaries.length}${diaryCursor ? "+" : ""} 篇已发布`
                  : "记录生活的片刻"}
              </span>
            </summary>
            <div className="life-columns">
              <section className="panel">
                <h2>已发布日记</h2>

                {!busy && !error && snapshot && diaries.length === 0 ? (
                  <StatePanel kind="empty" title="还没有已发布日记">
                    <p>这位角色当前没有可读取的已发布日记。</p>
                  </StatePanel>
                ) : (
                  <ul className="life-list">
                    {diaries.map((row) => (
                      <li key={row.diary_id}>
                        <button
                          type="button"
                          data-selected={
                            selected?.diary_id === row.diary_id || undefined
                          }
                          onClick={() => void readRevision(row)}
                        >
                          <strong>{row.day}</strong>
                          <small>已发布 · 点击阅读</small>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
                {diaryCursor && (
                  <button
                    className="button"
                    disabled={busy}
                    onClick={() => void moreDiaries()}
                  >
                    继续读取日记
                  </button>
                )}
              </section>
              <section className="panel">
                <h2>日记正文</h2>
                {!selected ? (
                  <p className="muted">选择一篇已发布日记。</p>
                ) : !revision && !error ? (
                  <StatePanel kind="loading" title="正在核对发布版本">
                    <p>请稍候。</p>
                  </StatePanel>
                ) : (
                  revision && (
                    <article className="life-revision">
                      <p className="muted">{selected.day}</p>
                      <p>{revision.content}</p>
                      <small>正文记录于 {dateText(revision.created_at)}</small>
                    </article>
                  )
                )}
              </section>
            </div>
          </details>
        </>
      )}
    </div>
  );
}
