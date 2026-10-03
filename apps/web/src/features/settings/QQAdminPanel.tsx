import { useEffect, useState, type FormEvent } from "react";
import { call, session, WebError } from "./api";
import "./qq-admin.css";

type Grant = {
  qq_id: string;
  note: string;
  actor_ids: string[];
  conversations: string[];
  capabilities: string[];
};
type View = {
  version: number;
  grants: Grant[];
  capabilities: string[];
  alias_pending: number;
  roles: { id: string; label: string }[];
};
const split = (value: string) =>
  value
    .split(/[\n,，]/)
    .map((part) => part.trim())
    .filter(Boolean);

export function QQAdminPanel() {
  const [csrf, setCsrf] = useState("");
  const [view, setView] = useState<View | null>(null);
  const [qq, setQq] = useState("");
  const [note, setNote] = useState("");
  const [actors, setActors] = useState("");
  const [conversations, setConversations] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const login = await session(controller.signal);
        if (!login.authenticated) throw new WebError("unauthorized", 401);
        setCsrf(login.csrf);
        const current = await call<View>(
          "qq-admin/view",
          {},
          login.csrf,
          controller.signal,
        );
        setView(current);
      } catch (cause) {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error ? cause.message : "无法读取 QQ 管理设置。",
          );
      }
    })();
    return () => controller.abort();
  }, []);

  async function refresh() {
    const controller = new AbortController();
    setView(await call<View>("qq-admin/view", {}, csrf, controller.signal));
  }

  async function save(event: FormEvent) {
    event.preventDefault();
    if (!view || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await call<View>(
        "qq-admin/grant",
        {
          qq_id: qq.trim(),
          note: note.trim(),
          expected_version: view.version,
          actor_ids: split(actors),
          conversations: split(conversations),
          capabilities: enabled ? ["identity.explain"] : [],
        },
        csrf,
        new AbortController().signal,
      );
      setView(result);
      setNotice("QQ 管理身份已保存。新消息会按当前范围重新核验。");
    } catch (cause) {
      await refresh().catch(() => undefined);
      setError(
        cause instanceof Error ? cause.message : "保存失败，请重新读取后操作。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function revoke(id: string) {
    if (!view || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      setView(
        await call<View>(
          "qq-admin/revoke",
          {
            qq_id: id,
            expected_version: view.version,
          },
          csrf,
          new AbortController().signal,
        ),
      );
      setNotice("已撤销。生成中的回复会在发送前再次核验。");
    } catch (cause) {
      await refresh().catch(() => undefined);
      setError(
        cause instanceof Error ? cause.message : "撤销失败，请重新读取后操作。",
      );
    } finally {
      setBusy(false);
    }
  }

  function toggleActor(id: string) {
    setActors((current) => {
      const selected = new Set(split(current));
      if (selected.has(id)) selected.delete(id);
      else selected.add(id);
      return [...selected].join("\n");
    });
  }

  return (
    <div className="qq-admin-page">
      <section className="panel">
        <div className="section-heading">
          <h2>QQ 管理身份</h2>
          <a className="button" href="#/memory/0">
            前往用户档案
          </a>
        </div>
        <p>
          设置和撤销对话中的管理身份。用户档案、画像、记忆与回复设置统一在用户页面管理。
        </p>
        <p>
          此授权仅允许对话说明管理身份。回复白名单单独设置，数据删除、导出和设备控制均不包含在内。
        </p>
        {error && <p role="alert">{error}</p>}
        {notice && <p role="status">{notice}</p>}
        {view && (
          <>
            <ul className="qq-admin-list">
              {view.grants.map((grant) => (
                <li key={grant.qq_id}>
                  <strong>{grant.note || `QQ ${grant.qq_id}`}</strong>
                  <span>QQ {grant.qq_id}</span>
                  <small>
                    角色：
                    {grant.actor_ids
                      .map(
                        (id) =>
                          view.roles.find((role) => role.id === id)?.label ??
                          id,
                      )
                      .join("、") || "所有已准入角色"}
                    ；会话：{grant.conversations.join("、") || "已准入会话"}
                  </small>
                  <button
                    className="button"
                    type="button"
                    disabled={busy}
                    onClick={() => void revoke(grant.qq_id)}
                  >
                    撤销
                  </button>
                </li>
              ))}
            </ul>
            {view.alias_pending > 0 && (
              <p role="status">
                有 {view.alias_pending} 条称呼正在等待档案服务同步。
              </p>
            )}
            <form
              className="qq-admin-form"
              onSubmit={(event) => void save(event)}
            >
              <label>
                QQ 号
                <input
                  required
                  inputMode="numeric"
                  pattern="[1-9][0-9]*"
                  value={qq}
                  onChange={(event) => setQq(event.target.value)}
                />
              </label>
              <label>
                备注称呼
                <input
                  maxLength={80}
                  value={note}
                  onChange={(event) => setNote(event.target.value)}
                />
              </label>
              {view.roles.length > 0 && (
                <fieldset className="qq-admin-roles">
                  <legend>适用角色（不选择表示所有已准入角色）</legend>
                  {view.roles.map((role) => (
                    <label key={role.id}>
                      <input
                        type="checkbox"
                        checked={split(actors).includes(role.id)}
                        onChange={() => toggleActor(role.id)}
                      />
                      {role.label}
                    </label>
                  ))}
                </fieldset>
              )}
              <details className="qq-admin-advanced">
                <summary>高级设置：手动指定角色范围</summary>
                <label>
                  角色标识（每行一个；留空表示所有已准入角色）
                  <textarea
                    value={actors}
                    onChange={(event) => setActors(event.target.value)}
                    placeholder="actor:..."
                  />
                </label>
              </details>
              <label>
                适用会话（留空表示所有已准入会话）
                <textarea
                  value={conversations}
                  onChange={(event) => setConversations(event.target.value)}
                  placeholder="群聊 group:群号；私聊 private:该用户QQ号（每行一个）"
                />
              </label>
              <label className="qq-admin-toggle">
                <input
                  type="checkbox"
                  checked={enabled}
                  onChange={(event) => setEnabled(event.target.checked)}
                />
                允许在对话中说明管理身份
              </label>
              <button className="button" type="submit" disabled={busy}>
                保存管理身份
              </button>
            </form>
          </>
        )}
      </section>
    </div>
  );
}
