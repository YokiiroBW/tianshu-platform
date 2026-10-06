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
import {
  readRolePreference,
  saveRolePreference,
} from "../../app/rolePreference";
import "./memory.css";

type SubjectKey =
  | { kind: "person"; person_id: string }
  | { kind: "group"; conversation_id: string };
type Connection = {
  available: boolean;
  code: string;
  actor_id: string | null;
  roles: RoleChoice[];
  peer?: { configured: boolean; verified_at: string | null; code: string };
};
type RoleChoice = {
  id: string;
  label: string;
  version: number;
  available: boolean;
  reason: string | null;
};
type Overview = {
  memory_group_count: number;
  counts_truncated: boolean;
  verified_at: string;
  scope_version: number;
};
type Subject = {
  subject: SubjectKey;
  categories: string[];
  group_count: number;
  group_count_truncated: boolean;
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

function roleReason(code: string | null) {
  if (code === "role_disabled") return "角色已停用";
  if (code === "memory_read_disabled") return "该角色未开启记忆读取";
  if (code === "role_configuring") return "角色正在核验，请稍后刷新";
  return "该角色暂不可读取";
}

/** A scoped, read-only view; no approval/forget controls are exposed to a service reader. */
export default function MemoryPage({
  section,
  groupsOnly = false,
}: {
  section: number;
  groupsOnly?: boolean;
}) {
  const { session } = useAuth();
  const csrf = session?.authenticated ? session.csrf : "";
  const storageKey = `tianshu-memory-role:${encodeURIComponent(session?.username || csrf)}`;
  const [state, setState] = useState<Connection | null>(null);
  const [roleId, setRoleId] = useState<string | null>(null);
  const [subjects, setSubjects] = useState<Subject[]>([]);
  const [subjectsCursor, setSubjectsCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<SubjectKey | null>(null);
  const [records, setRecords] = useState<RecordGroup[]>([]);
  const [recordsCursor, setRecordsCursor] = useState<string | null>(null);
  const [recordsScope, setRecordsScope] = useState("");
  const [busy, setBusy] = useState(false);
  const [revalidating, setRevalidating] = useState(false);
  const [error, setError] = useState("");
  const [stateError, setStateError] = useState("");
  const current = useRef<AbortController | null>(null);
  const generation = useRef(0);
  const role = state?.roles.find((item) => item.id === roleId);
  const visibleSubjects = groupsOnly
    ? subjects.filter((item) => item.subject.kind === "group")
    : subjects;

  function clearResults() {
    generation.current++;
    current.current?.abort();
    setSubjects([]);
    setSubjectsCursor(null);
    setSelected(null);
    setRecords([]);
    setRecordsCursor(null);
    setRecordsScope("");
    setError("");
    setBusy(false);
    setRevalidating(false);
  }

  function roleRequest() {
    if (!role || !role.available) return null;
    return { role_id: role.id, role_version: role.version };
  }

  function failRead(cause: unknown) {
    const message = readFailure(cause);
    clearResults();
    if (
      cause instanceof IntegrationError &&
      [
        "scope_changed",
        "upstream_forbidden",
        "memory_identity_not_ready",
        "role_disabled",
        "role_configuring",
        "role_unavailable",
        "memory_read_disabled",
        "memory_read_required",
        "session_expired",
      ].includes(cause.code)
    ) {
      setState((before) =>
        before && roleId
          ? {
              ...before,
              roles: before.roles.map((item) =>
                item.id === roleId
                  ? { ...item, available: false, reason: cause.code }
                  : item,
              ),
            }
          : before,
      );
    }
    setError(message);
  }

  function chooseRole(next: string) {
    clearResults();
    setRoleId(next);
    saveRolePreference(storageKey, next);
  }

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
    const wasListed = state?.roles.some((item) => item.id === roleId);
    const previousRoleId = roleId;
    clearResults();
    const controller = start();
    setState(null);
    setRoleId(null);
    setStateError("");
    try {
      const next = await integrationPost<Connection>(
        "memory/state",
        {},
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        setState(next);
        const remembered = readRolePreference(storageKey);
        setRoleId(
          next.roles.length === 0
            ? null
            : next.roles.some((item) => item.id === remembered) ||
                (wasListed && remembered === previousRoleId)
              ? remembered
              : next.actor_id,
        );
      }
    } catch (cause) {
      if (!controller.signal.aborted) setStateError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    clearResults();
    if (!csrf) saveRolePreference(storageKey, null);
    void loadState();
    return () => current.current?.abort();
  }, [csrf, storageKey]);

  async function listSubjects(cursor: string | null = null) {
    const selection = roleRequest();
    if (!csrf || !selection) return;
    const controller = start();
    const mark = generation.current;
    if (!cursor) {
      setSubjects([]);
      setSubjectsCursor(null);
    }
    try {
      const answer = await integrationPost<Subjects>(
        "memory/subjects",
        { ...selection, limit: 20, cursor },
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
        failRead(cause);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function listRecords(
    subject: SubjectKey | null,
    cursor: string | null = null,
  ) {
    const selection = roleRequest();
    if (!csrf || !selection) return;
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
        { ...selection, subject, limit: 20, cursor },
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
        failRead(cause);
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    if (!state) return;
    clearResults();
    if (!state?.available || !role?.available) return;
    if (section === 1) void listSubjects();
    else if (section === 3) void listRecords(null);
  }, [state, roleId, section, csrf]);

  useEffect(() => {
    const selection = roleRequest();
    if (!csrf || !state?.available || !selection) return;
    let pending: AbortController | null = null;
    const verify = async (resumed = false) => {
      if (document.hidden || pending) return;
      const controller = new AbortController();
      const mark = generation.current;
      pending = controller;
      try {
        await integrationPost<Overview>(
          "memory/overview",
          selection,
          csrf,
          controller.signal,
        );
        if (
          controller.signal.aborted ||
          mark !== generation.current ||
          !resumed
        )
          return;
        if (section === 1) await listSubjects();
        else if (section === 3) await listRecords(null);
      } catch (cause) {
        if (!controller.signal.aborted && mark === generation.current)
          failRead(cause);
      } finally {
        if (pending === controller) pending = null;
        if (
          !controller.signal.aborted &&
          mark === generation.current &&
          resumed
        )
          setRevalidating(false);
      }
    };
    const visible = () => {
      if (document.hidden) return;
      pending?.abort();
      pending = null;
      clearResults();
      setRevalidating(true);
      void verify(true);
    };
    const timer = window.setInterval(() => void verify(), 15000);
    document.addEventListener("visibilitychange", visible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", visible);
      pending?.abort();
    };
  }, [csrf, state?.available, role?.id, role?.version, section]);

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

  const rolePicker = (
    <section className="panel memory-role-panel">
      <div className="section-heading">
        <h2>当前角色</h2>
        <button
          className="button"
          onClick={() => void loadState()}
          disabled={busy}
        >
          <RefreshCw aria-hidden="true" />
          刷新角色
        </button>
      </div>
      <label htmlFor="memory-role-select">查看哪位角色的记忆</label>
      <select
        id="memory-role-select"
        value={roleId ?? ""}
        onChange={(event) => chooseRole(event.target.value)}
      >
        {roleId && !role && (
          <option value={roleId}>原角色已不在授权列表</option>
        )}
        {!roleId && (
          <option value="">{state.roles.length ? "请选择角色" : ""}</option>
        )}
        {state.roles.map((item) => (
          <option key={item.id} value={item.id}>
            {item.label}
            {item.available ? "" : ` · ${roleReason(item.reason)}`}
          </option>
        ))}
      </select>
      {role && !role.available && (
        <p className="muted">
          {roleReason(role.reason)}。已隐藏此角色的记忆内容。
        </p>
      )}
    </section>
  );

  if (!role || !role.available)
    return (
      <div className="memory-page">
        {rolePicker}
        <StatePanel
          kind="error"
          title={role ? roleReason(role.reason) : "角色不可用"}
        >
          <p>
            {roleId && !role
              ? "原角色已不在授权列表。"
              : "此角色当前不可读取。"}
            请刷新角色列表或选择当前可读取的角色。
          </p>
        </StatePanel>
      </div>
    );

  if (revalidating)
    return (
      <div className="memory-page">
        {rolePicker}
        <StatePanel kind="loading" title="正在重新核验角色记忆">
          <p>请稍候，核验完成后会显示当前角色的记忆。</p>
        </StatePanel>
      </div>
    );

  return (
    <div className="memory-page">
      {rolePicker}
      <section className="panel">
        <div className="section-heading">
          <h2>授权记忆范围</h2>
          <button
            className="button"
            onClick={() =>
              section === 1 ? void listSubjects() : void listRecords(null)
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
                section === 1 && !selected
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
      {section === 1 && (
        <div className="memory-columns">
          <section className="panel">
            <h2>{groupsOnly ? "群画像" : "人物与群"}</h2>
            {visibleSubjects.length === 0 && !busy && !error ? (
              <StatePanel
                kind="empty"
                title={
                  groupsOnly
                    ? "暂无可见群画像"
                    : subjectsCursor
                      ? "本页没有可见人物或群"
                      : "没有可见人物或群"
                }
              >
                <p>
                  {subjectsCursor
                    ? "本页扫描没有返回可见条目，仍有后续页可读取。"
                    : "这次读取成功，且当前授权范围没有更多可浏览条目。"}
                </p>
              </StatePanel>
            ) : (
              <ul className="memory-list">
                {visibleSubjects.map((row) => (
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
                        {row.group_count_truncated
                          ? `至少 ${row.group_count}`
                          : row.group_count}{" "}
                        个有效组 · {row.categories.join("、") || "未分类"}
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
                {groupsOnly ? "继续读取群画像" : "继续读取人物与群"}
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
                {groupsOnly
                  ? "选择群后查看当前角色可读取的群画像。用户身份与个人画像请在用户档案中查看。"
                  : "选择人物或群后读取当前可见的共享画像。不会显示对方私有记忆。"}
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
  if (!busy && !error && records.length === 0 && !cursor)
    return (
      <StatePanel kind="empty" title="这个范围没有有效记忆">
        <p>{scope}成功返回空列表。其它范围可能有不同内容。</p>
      </StatePanel>
    );
  return (
    <>
      {records.length === 0 && cursor && (
        <p className="muted">本页没有可见记忆，仍有后续页可读取。</p>
      )}
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
