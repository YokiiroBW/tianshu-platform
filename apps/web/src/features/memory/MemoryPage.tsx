import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useAuth } from "../../app/Auth";
import {
  IntegrationError,
  integrationPost,
  readFailure,
} from "../../app/integrationApi";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import "./memory.css";

type SubjectKey =
  | { kind: "person"; person_id: string }
  | { kind: "group"; conversation_id: string };
type Connection = {
  available: boolean;
  code: string;
  peer?: { configured: boolean; verified_at: string | null; code: string };
};
type Overview = {
  memory_group_count: number;
  subject_count: number;
  verified_at: string;
  scope_version: number;
};
type Subject = {
  subject: SubjectKey;
  categories: string[];
  group_count: number;
};
type Subjects = {
  items: Subject[];
  next_cursor: string | null;
  verified_at: string;
  scope_version: number;
};
type Unit = {
  record_id: string;
  record_version: number;
  statement: string;
  conditions: unknown;
  negations: unknown;
  valid_time: unknown;
  uncertainty: unknown;
  reality: unknown;
};
type RecordGroup = {
  semantic_group_id: string;
  category: string;
  field_key: string;
  item_key: string;
  units: Unit[];
};
type Records = {
  items: RecordGroup[];
  next_cursor: string | null;
  verified_at: string;
  scope_version: number;
};

function subjectText(subject: SubjectKey) {
  return subject.kind === "person"
    ? `人物 ${subject.person_id}`
    : `群 ${subject.conversation_id}`;
}

function unavailableTitle(code: string) {
  if (code === "memory_identity_not_ready")
    return "当前账号的记忆身份映射尚未就绪";
  if (code.endsWith("_read_required")) return "当前账号未获记忆读取权限";
  if (code.endsWith("_credential_missing")) return "记忆服务凭据缺失";
  return "记忆浏览尚未配置";
}

/** A scoped, read-only view; no approval/forget controls are exposed to a service reader. */
export default function MemoryPage({ section }: { section: number }) {
  const { session } = useAuth();
  const csrf = session?.authenticated ? session.csrf : "";
  const [state, setState] = useState<Connection | null>(null);
  const [overview, setOverview] = useState<Overview | null>(null);
  const [subjects, setSubjects] = useState<Subject[]>([]);
  const [subjectsCursor, setSubjectsCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<SubjectKey | null>(null);
  const [records, setRecords] = useState<RecordGroup[]>([]);
  const [recordsCursor, setRecordsCursor] = useState<string | null>(null);
  const [recordsScope, setRecordsScope] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [stateError, setStateError] = useState("");
  const current = useRef<AbortController | null>(null);
  const generation = useRef(0);

  function start() {
    current.current?.abort();
    const controller = new AbortController();
    current.current = controller;
    setBusy(true);
    setError("");
    return controller;
  }

  async function loadState() {
    if (!csrf) return;
    const controller = start();
    setState(null);
    setStateError("");
    try {
      const next = await integrationPost<Connection>(
        "memory/state",
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
    setOverview(null);
    setSubjects([]);
    setSelected(null);
    setRecords([]);
    setError("");
    void loadState();
    return () => current.current?.abort();
  }, [csrf]);

  async function readOverview() {
    if (!csrf) return;
    const controller = start();
    setOverview(null);
    try {
      const answer = await integrationPost<Overview>(
        "memory/overview",
        {},
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) setOverview(answer);
    } catch (cause) {
      if (!controller.signal.aborted) setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function listSubjects(cursor: string | null = null) {
    if (!csrf) return;
    const controller = start();
    const mark = generation.current;
    if (!cursor) {
      setSubjects([]);
      setSubjectsCursor(null);
    }
    try {
      const answer = await integrationPost<Subjects>(
        "memory/subjects",
        { limit: 20, cursor },
        csrf,
        controller.signal,
      );
      if (controller.signal.aborted || mark !== generation.current) return;
      setSubjects((before) =>
        cursor ? [...before, ...answer.items] : answer.items,
      );
      setSubjectsCursor(answer.next_cursor);
    } catch (cause) {
      if (!controller.signal.aborted && mark === generation.current) {
        setError(readFailure(cause));
        if (
          cause instanceof IntegrationError &&
          [
            "scope_changed",
            "upstream_forbidden",
            "memory_identity_not_ready",
          ].includes(cause.code)
        ) {
          setSubjects([]);
          setSubjectsCursor(null);
          setSelected(null);
          setRecords([]);
          setRecordsCursor(null);
        }
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function listRecords(
    subject: SubjectKey | null,
    cursor: string | null = null,
  ) {
    if (!csrf) return;
    const controller = start();
    const mark = generation.current;
    const key = subject ? subjectText(subject) : "本人记忆";
    if (!cursor) {
      setRecords([]);
      setRecordsCursor(null);
      setRecordsScope(key);
    }
    try {
      const answer = await integrationPost<Records>(
        "memory/records",
        { subject, limit: 20, cursor },
        csrf,
        controller.signal,
      );
      if (controller.signal.aborted || mark !== generation.current) return;
      setRecords((before) =>
        cursor ? [...before, ...answer.items] : answer.items,
      );
      setRecordsCursor(answer.next_cursor);
    } catch (cause) {
      if (!controller.signal.aborted && mark === generation.current) {
        setError(readFailure(cause));
        if (
          cause instanceof IntegrationError &&
          [
            "scope_changed",
            "upstream_forbidden",
            "memory_identity_not_ready",
          ].includes(cause.code)
        ) {
          setRecords([]);
          setRecordsCursor(null);
          setSubjects([]);
          setSubjectsCursor(null);
        }
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    generation.current++;
    if (state?.available) current.current?.abort();
    setOverview(null);
    setSubjects([]);
    setSubjectsCursor(null);
    setSelected(null);
    setRecords([]);
    setRecordsCursor(null);
    setError("");
    if (!state?.available) return;
    if (section === 0) void readOverview();
    else if (section === 1) void listSubjects();
    else if (section === 3) void listRecords(null);
  }, [state?.available, section, csrf]);

  function choose(row: SubjectKey) {
    generation.current++;
    current.current?.abort();
    setSelected(row);
    setRecords([]);
    setRecordsCursor(null);
    setError("");
    void listRecords(row);
  }

  if (!csrf)
    return (
      <StatePanel kind="error" title="请先登录">
        <p>记忆内容只对已授权账号开放。</p>
      </StatePanel>
    );
  if (stateError)
    return (
      <StatePanel
        kind="error"
        title="记忆连接状态没有读到"
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
      <StatePanel kind="loading" title="正在读取记忆连接">
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
          部署端需要为当前账号配置精确的 Memory
          读取身份与范围。网页无需填写服务地址、账号映射或令牌。
        </p>
        <p>状态码：{state.code}</p>
      </StatePanel>
    );

  return (
    <div className="memory-page">
      <section className="panel">
        <div className="section-heading">
          <h2>授权记忆范围</h2>
          <button
            className="button"
            onClick={() =>
              section === 0
                ? void readOverview()
                : section === 1
                  ? void listSubjects()
                  : void listRecords(null)
            }
            disabled={busy}
          >
            <RefreshCw aria-hidden="true" />
            重新读取
          </button>
        </div>
        <StatusRail tone="yellow" label="按当前账号授权读取">
          <p>
            内容来自 Memory
            已确认的有效语义组；人物、群和本人范围分别校验。没有网页批准、遗忘或账号关联操作。
          </p>
        </StatusRail>
      </section>
      {error && (
        <StatePanel
          kind="error"
          title="本次记忆读取失败"
          action={
            <button
              className="button"
              onClick={() =>
                section === 0
                  ? void readOverview()
                  : section === 1 && !selected
                    ? void listSubjects()
                    : void listRecords(selected)
              }
            >
              从第一页重读
            </button>
          }
        >
          <p>{error}</p>
          <p>失败没有被当作空记忆。</p>
        </StatePanel>
      )}
      {section === 0 && (
        <section className="panel">
          <h2>记忆概览</h2>
          {!overview && !error ? (
            <StatePanel kind="loading" title="正在读取记忆概览">
              <p>请稍候。</p>
            </StatePanel>
          ) : (
            overview && (
              <>
                <dl className="memory-counts">
                  <div>
                    <dt>有效记忆组</dt>
                    <dd>{overview.memory_group_count}</dd>
                  </div>
                  <div>
                    <dt>可见人物与群</dt>
                    <dd>{overview.subject_count}</dd>
                  </div>
                </dl>
                <p className="muted">
                  读取于 {overview.verified_at} · 范围版本{" "}
                  {overview.scope_version}。计数只覆盖当前账号获准的范围。
                </p>
                <div className="memory-actions">
                  <a className="button" href="#/memory/1">
                    查看人物与群
                  </a>
                  <a className="button" href="#/memory/3">
                    查看本人记忆
                  </a>
                </div>
              </>
            )
          )}
        </section>
      )}
      {section === 1 && (
        <div className="memory-columns">
          <section className="panel">
            <h2>人物与群</h2>
            {subjects.length === 0 && !busy && !error ? (
              <StatePanel kind="empty" title="没有可见人物或群">
                <p>这次读取成功，但当前授权范围没有可浏览条目。</p>
              </StatePanel>
            ) : (
              <ul className="memory-list">
                {subjects.map((row) => (
                  <li key={subjectText(row.subject)}>
                    <button
                      type="button"
                      data-selected={
                        (selected &&
                          subjectText(selected) === subjectText(row.subject)) ||
                        undefined
                      }
                      onClick={() => choose(row.subject)}
                    >
                      <strong>{subjectText(row.subject)}</strong>
                      <small>
                        {row.group_count} 个有效组 ·{" "}
                        {row.categories.join("、") || "未分类"}
                      </small>
                    </button>
                  </li>
                ))}
              </ul>
            )}
            {subjectsCursor && (
              <button
                className="button"
                disabled={busy}
                onClick={() => void listSubjects(subjectsCursor)}
              >
                继续读取人物与群
              </button>
            )}
          </section>
          <section className="panel">
            <h2>共享画像</h2>
            {selected ? (
              <RecordList
                scope={recordsScope}
                records={records}
                cursor={recordsCursor}
                busy={busy}
                error={error}
                more={() => void listRecords(selected, recordsCursor)}
              />
            ) : (
              <p className="muted">
                选择人物或群后读取当前可见的共享画像。不会显示对方私有记忆。
              </p>
            )}
          </section>
        </div>
      )}
      {section === 3 && (
        <section className="panel">
          <h2>本人记忆</h2>
          <RecordList
            scope={recordsScope || "本人记忆"}
            records={records}
            cursor={recordsCursor}
            busy={busy}
            error={error}
            more={() => void listRecords(null, recordsCursor)}
          />
        </section>
      )}
    </div>
  );
}

function RecordList({
  scope,
  records,
  cursor,
  busy,
  error,
  more,
}: {
  scope: string;
  records: RecordGroup[];
  cursor: string | null;
  busy: boolean;
  error: string;
  more: () => void;
}) {
  if (busy && records.length === 0)
    return (
      <StatePanel kind="loading" title="正在读取记忆条目">
        <p>请稍候。</p>
      </StatePanel>
    );
  if (!busy && !error && records.length === 0)
    return (
      <StatePanel kind="empty" title="这个范围没有有效记忆">
        <p>{scope}成功返回空列表。其它范围可能有不同内容。</p>
      </StatePanel>
    );
  return (
    <>
      <ul className="memory-records">
        {records.map((group) => (
          <li key={group.semantic_group_id}>
            <h3>
              {group.category} · {group.field_key}
            </h3>
            <p className="muted">语义组 {group.semantic_group_id}</p>
            <ul>
              {group.units.map((unit) => (
                <li key={unit.record_id}>
                  <p>{unit.statement}</p>
                  <small>记录版本 {unit.record_version}</small>
                </li>
              ))}
            </ul>
          </li>
        ))}
      </ul>
      {cursor && (
        <button className="button" disabled={busy} onClick={more}>
          继续读取记忆
        </button>
      )}
    </>
  );
}
