import { useCallback, useEffect, useRef, useState } from "react";
import { Copy, Plus, Save, Send } from "lucide-react";
import { author, reason, type SessionState } from "./personaApi";
import { shortTime, stateLabel } from "./personaTypes";

type Content = {
  persona: string;
  tone: string;
  style: string;
  address: string;
};
type Item = {
  id: string;
  kind: "profile" | "role";
  name: string;
  description: string;
  version: number;
  state: string;
  published_revision: string | null;
  draft_revision: string | null;
  updated_at: number | null;
  last_applied_profile_revision: string | null;
  applied_state?: "never" | "current" | "previous" | "unknown";
  applied_target?: string | null;
  content?: Content;
  additional_fields?: string[];
};
type Form = { name: string; description: string; content: Content };
const blank: Form = {
  name: "",
  description: "",
  content: { persona: "", tone: "", style: "", address: "" },
};
const labels: {
  key: keyof Content;
  label: string;
  hint: string;
  max: number;
}[] = [
  {
    key: "persona",
    label: "人设正文",
    hint: "角色的核心身份、性格和对话边界。",
    max: 20000,
  },
  {
    key: "tone",
    label: "语气",
    hint: "例如温和、简洁；说明说话的情绪与分寸。",
    max: 20000,
  },
  {
    key: "style",
    label: "表达风格",
    hint: "例如短句、口语化；描述措辞与节奏。",
    max: 20000,
  },
  { key: "address", label: "称呼", hint: "角色如何称呼对话者。", max: 4000 },
];

function fromItem(item: Item): Form {
  return {
    name: item.name,
    description: item.description || "",
    content: {
      persona: item.content?.persona || "",
      tone: item.content?.tone || "",
      style: item.content?.style || "",
      address: item.content?.address || "",
    },
  };
}

function applicationLabel(item: Item) {
  const target = item.applied_target;
  const newDraft =
    item.draft_revision &&
    item.last_applied_profile_revision !== item.draft_revision;
  if (item.applied_state === "current")
    return `当前生效于 ${target}${newDraft ? " · 有新草稿" : ""}`;
  if (item.applied_state === "previous")
    return `曾应用到 ${target}${newDraft ? " · 有新草稿" : ""}`;
  if (item.applied_state === "unknown") return "应用状态不在当前授权范围";
  return "草稿";
}

export default function PersonaAuthor({
  session,
  onApplied,
}: {
  session: SessionState | null;
  onApplied: () => void;
}) {
  const [profiles, setProfiles] = useState<Item[]>([]);
  const [targets, setTargets] = useState<Item[]>([]);
  const [permissions, setPermissions] = useState({
    create: false,
    edit: false,
    apply: false,
  });
  const [selected, setSelected] = useState<Item | null>(null);
  const [copySource, setCopySource] = useState<{
    id: string;
    version: number;
  } | null>(null);
  const [form, setForm] = useState<Form>(blank);
  const [baseline, setBaseline] = useState<Form>(blank);
  const [target, setTarget] = useState("");
  const [targetVersion, setTargetVersion] = useState<number | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loaded, setLoaded] = useState(false);
  const request = useRef<{ body: string; id: string } | null>(null);
  const controller = useRef<AbortController | null>(null);
  const dirty = JSON.stringify(form) !== JSON.stringify(baseline);
  const dirtyRef = useRef(dirty);
  dirtyRef.current = dirty;

  useEffect(() => {
    const warn = (event: BeforeUnloadEvent) => {
      if (!dirtyRef.current) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, []);

  useEffect(() => {
    const protect = (event: Event) => {
      if (
        dirtyRef.current &&
        !window.confirm("有尚未保存的输入。离开后会丢失，确定继续吗？")
      )
        event.preventDefault();
    };
    window.addEventListener("tianshu:before-route", protect);
    return () => window.removeEventListener("tianshu:before-route", protect);
  }, []);

  useEffect(() => () => controller.current?.abort(), []);

  const call = useCallback(
    async <T,>(
      path: "profiles" | "view" | "create" | "save" | "apply",
      body: object,
    ) => {
      if (!session?.authenticated) throw new Error("请先登录。");
      const next = new AbortController();
      controller.current = next;
      return author<T>(path, body, session.csrf, next.signal);
    },
    [session],
  );

  const reload = useCallback(async () => {
    const result = await call<{
      profiles: Item[];
      targets: Item[];
      permissions: { create: boolean; edit: boolean; apply: boolean };
    }>("profiles", {});
    setProfiles(result.profiles);
    setTargets(result.targets);
    setPermissions(result.permissions);
    setLoaded(true);
  }, [call]);

  useEffect(() => {
    if (!session?.authenticated) return;
    void reload().catch((cause) => setError(reason(cause)));
  }, [reload, session?.authenticated]);

  const mayLeave = () =>
    !dirty || window.confirm("有尚未保存的输入。离开后会丢失，确定继续吗？");

  const open = async (id: string) => {
    if (busy || !mayLeave()) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const answer = await call<{ item: Item }>("view", { id });
      const item = answer.item;
      const next = fromItem(item);
      setSelected(item);
      setCopySource(null);
      setForm(next);
      setBaseline(next);
      setTarget(item.kind === "role" ? item.id : "");
      setTargetVersion(item.kind === "role" ? item.version : null);
      request.current = null;
    } catch (cause) {
      setError(reason(cause));
    } finally {
      setBusy(false);
    }
  };

  const create = () => {
    if (busy || !mayLeave()) return;
    setSelected(null);
    setCopySource(null);
    setForm(blank);
    setBaseline(blank);
    setTarget("");
    setTargetVersion(null);
    setNotice(
      "新档案不会新增角色、来源或发送权限。先保存草稿，再选择授权角色应用。",
    );
    setError("");
    request.current = null;
  };

  const copy = () => {
    if (!selected || busy || !mayLeave()) return;
    const next = { ...form, name: `${form.name} 副本` };
    setSelected(null);
    setCopySource({ id: selected.id, version: selected.version });
    setForm(next);
    setBaseline(blank);
    setTarget("");
    setTargetVersion(null);
    setNotice("复制内容已放入新档案，保存草稿后才会入库；不会自动应用。");
    setError("");
    request.current = null;
  };

  const chooseTarget = async (id: string) => {
    if (busy) return;
    setBusy(true);
    setError("");
    try {
      const answer = await call<{ item: Item }>("view", { id });
      setTarget(id);
      setTargetVersion(answer.item.version);
    } catch (cause) {
      setError(reason(cause));
    } finally {
      setBusy(false);
    }
  };

  const submit = async (apply: boolean) => {
    if (busy || !form.name.trim() || !form.content.persona.trim()) return;
    if (apply && (!selected || !target || targetVersion === null)) return;
    if (apply && (!permissions.edit || !permissions.apply)) return;
    if (!apply && !(selected ? permissions.edit : permissions.create)) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const path = apply ? "apply" : selected ? "save" : "create";
      const body: Record<string, unknown> = { ...form };
      if (!selected && copySource)
        Object.assign(body, {
          copy_from: copySource.id,
          copy_expected: copySource.version,
        });
      if (selected)
        Object.assign(body, { id: selected.id, expected: selected.version });
      if (apply)
        Object.assign(body, { target, target_expected: targetVersion });
      const signature = JSON.stringify([path, body]);
      if (request.current?.body !== signature)
        request.current = { body: signature, id: crypto.randomUUID() };
      body.client_id = request.current.id;
      const result = await call<{ item: Item; target?: Item }>(path, body);
      request.current = null;
      const next = { ...result.item, content: { ...form.content } };
      setSelected(next);
      setCopySource(null);
      setBaseline(form);
      if (result.target) setTargetVersion(result.target.version);
      else if (next.kind === "role") setTargetVersion(next.version);
      setNotice(
        apply
          ? `已应用到 ${target}。新准备的对话轮次将使用新版；在途轮次保留原快照。`
          : "草稿已保存，运行中的人格没有改变。",
      );
      await reload();
      if (apply) onApplied();
    } catch (cause) {
      setError(reason(cause));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section className="persona-author" aria-label="人格档案编辑">
      <div className="persona-author-head">
        <div>
          <p className="eyebrow">人格档案</p>
          <h3>创建与编辑</h3>
          <p className="muted">
            草稿只保存内容。明确应用后，所选授权角色才会使用新版人格。
          </p>
        </div>
        <button
          type="button"
          className="button primary"
          onClick={create}
          disabled={busy || !permissions.create}
        >
          <Plus aria-hidden="true" /> 新建档案
        </button>
      </div>
      {error && (
        <p className="persona-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="persona-notice" role="status">
          {notice}
        </p>
      )}
      {loaded &&
        !permissions.create &&
        !permissions.edit &&
        !permissions.apply && (
          <p className="muted">
            当前账号可查看档案；创建、编辑和应用权限未开放。
          </p>
        )}
      {!loaded && !error && <p className="muted">正在读取档案…</p>}
      <div className="persona-author-layout">
        <nav className="persona-author-list" aria-label="选择人格档案或角色">
          <h4>可复用档案</h4>
          {profiles.length === 0 && (
            <p className="muted">还没有档案。可以新建或复制已有角色。</p>
          )}
          {profiles.map((item) => (
            <button
              key={item.id}
              type="button"
              className="persona-subject"
              aria-pressed={selected?.id === item.id}
              onClick={() => void open(item.id)}
            >
              <strong>{item.name}</strong>
              <span className="persona-subject-meta">
                {item.description || "没有简介"}
              </span>
              <span className="persona-subject-time">
                {applicationLabel(item)} · {shortTime(item.updated_at)}
              </span>
            </button>
          ))}
          <h4>已有授权角色</h4>
          {targets.map((role) => (
            <button
              key={role.id}
              type="button"
              className="persona-subject"
              aria-pressed={selected?.id === role.id}
              onClick={() => void open(role.id)}
            >
              {role.name !== role.id && <strong>{role.name}</strong>}
              <span className="persona-subject-id">{role.id}</span>
            </button>
          ))}
        </nav>
        <div className="persona-author-form">
          <div className="persona-author-title">
            <h4>{selected ? selected.name : "新档案"}</h4>
            {selected && (
              <button
                type="button"
                className="button"
                onClick={copy}
                disabled={busy}
              >
                <Copy aria-hidden="true" /> 复制为新档案
              </button>
            )}
          </div>
          {selected && (
            <p className="muted">
              {selected.kind === "profile" ? "可复用档案" : "已登记角色"} ·{" "}
              {stateLabel(selected.state)} · 更新{" "}
              {shortTime(selected.updated_at)} ·{" "}
              {selected.draft_revision ? "有草稿" : "无草稿"}
            </p>
          )}
          {selected?.kind === "profile" && (
            <p className="muted">
              {applicationLabel(
                profiles.find((item) => item.id === selected.id) || selected,
              )}
            </p>
          )}
          <label className="persona-author-field">
            名称 <span className="muted">必填，最多 80 字</span>
            <input
              value={form.name}
              maxLength={80}
              required
              onChange={(event) =>
                setForm({ ...form, name: event.target.value })
              }
            />
          </label>
          <label className="persona-author-field">
            简介 <span className="muted">档案说明，不会进入系统提示词</span>
            <textarea
              value={form.description}
              maxLength={400}
              rows={2}
              onChange={(event) =>
                setForm({ ...form, description: event.target.value })
              }
            />
          </label>
          {labels.map(({ key, label, hint, max }) => (
            <label key={key} className="persona-author-field">
              {label}
              <span className="muted">
                {hint} {key === "persona" ? "必填。" : "可留空。"}{" "}
                {form.content[key].length}/{max} 字
              </span>
              <textarea
                value={form.content[key]}
                maxLength={max}
                rows={key === "persona" ? 7 : 3}
                required={key === "persona"}
                onChange={(event) =>
                  setForm({
                    ...form,
                    content: { ...form.content, [key]: event.target.value },
                  })
                }
              />
            </label>
          ))}
          {!!selected?.additional_fields?.length && (
            <p className="muted">
              这版还有 {selected.additional_fields.join("、")}{" "}
              字段；编辑时后台会保留其原值。
            </p>
          )}
          {selected && (
            <div className="persona-author-targets">
              <h4>应用到</h4>
              {selected.kind === "role" ? (
                <p className="muted">当前角色：{selected.id}</p>
              ) : (
                <div className="persona-target-options">
                  {targets.map((role) => (
                    <button
                      type="button"
                      key={role.id}
                      className="button"
                      aria-pressed={target === role.id}
                      disabled={busy}
                      onClick={() => void chooseTarget(role.id)}
                    >
                      {role.name !== role.id
                        ? `${role.name} · ${role.id}`
                        : role.id}
                    </button>
                  ))}
                </div>
              )}
              {target && (
                <p className="muted">
                  目标 {target} · 读取版本 {targetVersion ?? "读取中"}
                </p>
              )}
            </div>
          )}
          {dirty && (
            <p className="persona-notice" role="status">
              有尚未保存的输入。
            </p>
          )}
          <div className="persona-author-actions">
            <button
              type="button"
              className="button"
              disabled={
                busy ||
                !(selected ? permissions.edit : permissions.create) ||
                !form.name.trim() ||
                !form.content.persona.trim()
              }
              onClick={() => void submit(false)}
            >
              <Save aria-hidden="true" /> {busy ? "处理中…" : "保存草稿"}
            </button>
            <button
              type="button"
              className="button primary"
              disabled={
                busy ||
                !permissions.edit ||
                !permissions.apply ||
                !selected ||
                !target ||
                targetVersion === null ||
                !form.name.trim() ||
                !form.content.persona.trim()
              }
              onClick={() => void submit(true)}
            >
              <Send aria-hidden="true" /> 保存并应用
            </button>
          </div>
          {!selected && (
            <p className="muted">新档案先保存为草稿，再选择授权角色应用。</p>
          )}
        </div>
      </div>
    </section>
  );
}
