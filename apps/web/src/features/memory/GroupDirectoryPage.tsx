import { useEffect, useRef, useState } from "react";
import { RefreshCw, UsersRound } from "lucide-react";
import { useAuth } from "../../app/Auth";
import { integrationPost, readFailure } from "../../app/integrationApi";
import { requestId } from "../../app/requestId";
import { StatePanel } from "../../components/StatePanel";
import { ObservationUnlock } from "../people/ObservationUnlock";
import {
  GroupRuleEditor,
  GROUP_MODE_LABELS,
  type GroupPolicy,
  type GroupRule,
} from "../settings/GroupPolicyEditor";
import GroupInsights from "./GroupInsights";
import "../people/people.css";
import "./groups.css";

type GroupEntry = {
  id: string;
  owner_id: string;
  owner_name: string;
  account_id: string;
  group_id: string;
  name: string | null;
  member_count: number | null;
  max_member_count: number | null;
  first_seen_at: number | null;
  last_seen_at: number | null;
  revision: number;
  rule: GroupRule;
  pending: boolean;
  read_enabled: boolean;
  has_archive: boolean;
  state: string;
  ordinary_claim_available: boolean;
  memory_group_id: string | null;
};
type Directory = {
  available: boolean;
  unlocked: boolean;
  actors: { id: string; label: string }[];
  groups: GroupEntry[];
  next_cursor: { owner_id: string; group_id: string } | null;
};
type MemoryState = {
  available: boolean;
  roles: { id: string; version: number; available: boolean }[];
};
const stamp = (value: number | null) =>
  value === null ? "尚未收到消息" : new Date(value * 1000).toLocaleString();

export default function GroupDirectoryPage() {
  const { session } = useAuth();
  const csrf = session?.csrf ?? "";
  const [view, setView] = useState<Directory | null>(null);
  const [memory, setMemory] = useState<MemoryState | null>(null);
  const [selected, setSelected] = useState("");
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState<GroupRule | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [refresh, setRefresh] = useState(0);
  const active = useRef<AbortController | null>(null);
  const loadedPages = useRef(1);
  const directoryGeneration = useRef(0);
  const current = view?.groups.find((group) => group.id === selected);
  const role = memory?.roles.find((item) => item.id === current?.rule.actor_id);

  async function load(signal: AbortSignal, more = false) {
    const generation = ++directoryGeneration.current;
    let cursor: Directory["next_cursor"] = null;
    let next: Directory | null = null;
    const found = new Map<string, GroupEntry>();
    const count = loadedPages.current + (more ? 1 : 0);
    for (let page = 0; page < count; page++) {
      next = await integrationPost<Directory>(
        "bot-observation/groups",
        { limit: 100, cursor },
        csrf,
        signal,
      );
      next.groups.forEach((item) => found.set(item.id, item));
      cursor = next.next_cursor;
      if (!cursor || !next.available) break;
    }
    if (!next) return;
    if (signal.aborted || generation !== directoryGeneration.current) return;
    const result = next;
    if (more) loadedPages.current = count;
    setView((previous) => {
      // A directory request started before a save cannot undo its read-back.
      for (const prior of previous?.groups ?? []) {
        const incoming = found.get(prior.id);
        if (incoming && incoming.revision < prior.revision)
          found.set(prior.id, prior);
      }
      return { ...result, groups: result.available ? [...found.values()] : [] };
    });
    setSelected((previous) =>
      found.has(previous) ? previous : (found.values().next().value?.id ?? ""),
    );
  }

  useEffect(() => {
    if (!csrf) return;
    const controller = new AbortController();
    const update = async () => {
      if (document.hidden || controller.signal.aborted) return;
      try {
        await load(controller.signal);
        if (!controller.signal.aborted) setError("");
      } catch (cause) {
        if (!controller.signal.aborted) setError(readFailure(cause));
      }
      try {
        const next = await integrationPost<MemoryState>(
          "memory/state",
          {},
          csrf,
          controller.signal,
        );
        if (!controller.signal.aborted) setMemory(next);
      } catch {
        if (!controller.signal.aborted) setMemory(null);
      }
    };
    void update();
    const timer = window.setInterval(() => void update(), 15000);
    document.addEventListener("visibilitychange", update);
    return () => {
      controller.abort();
      active.current?.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
    };
  }, [csrf, refresh]);

  useEffect(() => {
    setDraft(
      current
        ? { ...current.rule, keywords: [...current.rule.keywords] }
        : null,
    );
  }, [current?.id, current?.revision]);

  async function more() {
    if (!view?.next_cursor || busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    try {
      await load(controller.signal, true);
    } catch (cause) {
      if (!controller.signal.aborted) setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function save() {
    if (!current || !draft || busy) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await integrationPost<{
        group: GroupEntry;
        connection: {
          pending: boolean;
          revision: number;
          state: string;
          group_policy: GroupPolicy;
        };
      }>(
        "bot-observation/group-policy",
        {
          owner_id: current.owner_id,
          group_id: current.group_id,
          expected_revision: current.revision,
          rule: draft,
          client_id: requestId(),
        },
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        const saved = result.connection;
        setView(
          (previous) =>
            previous && {
              ...previous,
              groups: previous.groups.map((group) =>
                group.id === result.group.id
                  ? result.group
                  : group.owner_id !== current.owner_id
                    ? group
                    : {
                        ...group,
                        pending: saved.pending,
                        revision: saved.revision,
                        state: saved.state,
                        rule:
                          saved.group_policy.groups[group.group_id] ??
                          saved.group_policy.default,
                      },
              ),
            },
        );
        setNotice(
          saved.pending
            ? "状态已保存，等待宿主确认。"
            : "群状态已保存并由宿主确认。",
        );
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(readFailure(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const groups =
    view?.groups.filter((group) =>
      `${group.name ?? ""} ${group.group_id} ${group.owner_name} ${group.account_id}`.includes(
        query.trim(),
      ),
    ) ?? [];
  const canRead =
    current?.has_archive &&
    current.read_enabled &&
    current.memory_group_id &&
    memory?.available &&
    role?.available &&
    !current.pending;
  return (
    <div className="group-directory-page">
      <section className="panel">
        <div className="section-heading">
          <div>
            <h2>群档案</h2>
            <p className="muted">
              收到群消息即登记基础资料。新群默认停用，开启后才整理消息、画像和记忆。
            </p>
          </div>
          <button
            className="button"
            disabled={busy}
            onClick={() => setRefresh((value) => value + 1)}
          >
            <RefreshCw size={16} aria-hidden="true" />
            刷新群档案
          </button>
        </div>
        {view && !view.unlocked && view.available && (
          <ObservationUnlock
            csrf={csrf}
            title="解锁群状态管理"
            description="群档案可以直接查看。调整状态沿用机器人管理的密码验证。"
            buttonLabel="解锁群状态"
            onUnlocked={() => setRefresh((value) => value + 1)}
          />
        )}
        {error && <p role="alert">{error}</p>}
        {notice && <p role="status">{notice}</p>}
        {!view && !error && <p role="status">正在读取群档案…</p>}
        {view && !view.available && (
          <StatePanel kind="unconfigured" title="群目录暂不可用">
            <p>请检查当前账号的机器人管理权限与账号连接。</p>
          </StatePanel>
        )}
        {view?.available && !view.groups.length && (
          <StatePanel kind="empty" title="尚未发现群">
            <p>
              已接入的 BOT
              收到群消息后，这里会自动显示空档案，无需先配置观察群。
            </p>
          </StatePanel>
        )}
      </section>
      {!!view?.groups.length && (
        <div className="group-directory-layout">
          <section className="panel group-directory-list">
            <label>
              查找群
              <input
                aria-label="查找群"
                placeholder="群名、群号或 BOT"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
              />
            </label>
            <ul>
              {groups.map((group) => (
                <li key={group.id}>
                  <button
                    type="button"
                    aria-pressed={selected === group.id}
                    onClick={() => {
                      setSelected(group.id);
                      setError("");
                      setNotice("");
                    }}
                  >
                    <UsersRound size={18} aria-hidden="true" />
                    <span>
                      <strong>{group.name ?? `群 ${group.group_id}`}</strong>
                      <small>
                        {group.group_id} ·{" "}
                        {group.member_count === null
                          ? "人数未知"
                          : `${group.member_count} 人`}
                      </small>
                      <small>
                        {group.owner_name} · BOT {group.account_id}
                      </small>
                    </span>
                    <span className="group-directory-mode">
                      {GROUP_MODE_LABELS[group.rule.mode]}
                      {group.pending ? " · 待确认" : ""}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            {!groups.length && (
              <p className="muted">已加载的档案中没有匹配的群。</p>
            )}
            {view.next_cursor && (
              <button
                className="button"
                disabled={busy}
                onClick={() => void more()}
              >
                加载更多群
              </button>
            )}
          </section>
          {current && draft && (
            <section className="panel group-directory-detail">
              <div className="section-heading">
                <h2>{current.name ?? `群 ${current.group_id}`}</h2>
                <span>
                  {GROUP_MODE_LABELS[current.rule.mode]}
                  {current.pending ? " · 等待宿主确认" : ""}
                </span>
              </div>
              <dl className="group-directory-facts">
                <div>
                  <dt>群号</dt>
                  <dd>{current.group_id}</dd>
                </div>
                <div>
                  <dt>群人数</dt>
                  <dd>
                    {current.member_count ?? "未知"}
                    {current.max_member_count !== null
                      ? ` / ${current.max_member_count}`
                      : ""}
                  </dd>
                </div>
                <div>
                  <dt>所属 BOT</dt>
                  <dd>
                    {current.owner_name} · {current.account_id}
                  </dd>
                </div>
                <div>
                  <dt>首次发现</dt>
                  <dd>{stamp(current.first_seen_at)}</dd>
                </div>
                <div>
                  <dt>最近收到消息</dt>
                  <dd>{stamp(current.last_seen_at)}</dd>
                </div>
              </dl>
              <GroupRuleEditor
                name="此群"
                value={draft}
                actors={view.actors}
                disabled={!view.unlocked || busy}
                onChange={setDraft}
              />
              <p className="muted">
                停用：仅更新基础资料。仅观察：整理群聊。启用回复：由 @
                或关键词触发。主动：再按角色兴趣参与。开启不补采停用期间的消息。
              </p>
              {draft.mode !== "disabled" && !draft.actor_id && (
                <p>开启前请选择负责此群的角色。</p>
              )}
              {draft.mode === "active" && !current.ordinary_claim_available && (
                <p role="status">
                  当前宿主尚未报告普通消息参与能力，主动参与暂不可用。
                </p>
              )}
              {!current.read_enabled && (
                <p role="status">
                  此 BOT
                  的历史读取与新增观察已暂停。群状态会保留，恢复账号观察后才能生效。
                </p>
              )}
              <button
                className="button"
                disabled={
                  !view.unlocked ||
                  busy ||
                  (draft.mode !== "disabled" && !draft.actor_id)
                }
                onClick={() => void save()}
              >
                保存此群状态
              </button>
            </section>
          )}
        </div>
      )}
      {current &&
        (canRead && role && current.memory_group_id ? (
          <GroupInsights
            key={`${current.id}:${role.id}:${role.version}:${current.revision}`}
            roleId={role.id}
            roleVersion={role.version}
            csrf={csrf}
            fixedGroupId={current.memory_group_id}
          />
        ) : (
          <section className="panel">
            <h2>{current.has_archive ? "群内容暂不可读取" : "空档案"}</h2>
            <p className="muted">
              {current.has_archive
                ? "已有收集记录按原角色和读取授权保留；当前权限或宿主状态未就绪，暂不展示内容。"
                : "目前仅登记基础资料，尚无已收集的聊天内容或整理结果。开启观察后才会逐步形成话题、群画像、互动与热度。"}
            </p>
          </section>
        ))}
    </div>
  );
}
