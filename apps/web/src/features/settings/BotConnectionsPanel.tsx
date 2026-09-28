import { useEffect, useRef, useState, type FormEvent } from "react";
import { useAuth } from "../../app/Auth";
import { integrationPost, readFailure } from "../../app/integrationApi";
import { StatusRail } from "../../components/StatusRail";
import "./bots.css";

type Slot = {
  slot_id: string;
  label: string;
  adapter: "nonebot" | "astrbot";
  platform_id: string;
  self_id: string;
  namespace: "qq" | "tg";
  conversation_id: string;
  actor_ids: string[];
  registered_authors: number;
  created: boolean;
};
type Connection = {
  connection_id: string;
  slot_id: string;
  adapter: "nonebot" | "astrbot";
  actor_ids: string[];
  enabled: boolean;
  state: "online" | "pending" | "disabled" | "failed";
  last_seen_at: string | null;
  last_event_at: string | null;
  last_error: string | null;
  credential: string;
  delivery: {
    pending: number;
    claimed: number;
    sent: number;
    failed: number;
    unknown: number;
  };
};
type View = {
  available: boolean;
  slots: Slot[];
  connections: Connection[];
  management: { code: string; unlocked: boolean };
};
type Created = { connection_id: string; token?: string; enabled: boolean };

const state: Record<
  Connection["state"],
  { tone: "blue" | "yellow" | "gray" | "red"; label: string }
> = {
  online: { tone: "blue", label: "插件在线" },
  pending: { tone: "yellow", label: "待配置或离线" },
  disabled: { tone: "gray", label: "已停用" },
  failed: { tone: "red", label: "授权失败" },
};

function stamp(value: string | null) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "尚无";
}

export function BotConnectionsPanel() {
  const { session } = useAuth();
  const csrf = session?.authenticated ? session.csrf : "";
  const active = useRef<AbortController | null>(null);
  const [view, setView] = useState<View | null>(null);
  const [selected, setSelected] = useState("");
  const [actors, setActors] = useState<string[]>([]);
  const [password, setPassword] = useState("");
  const [secret, setSecret] = useState<Created | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function request<T>(path: string, body: object): Promise<T> {
    const controller = new AbortController();
    active.current = controller;
    return integrationPost<T>(`bots/${path}`, body, csrf, controller.signal);
  }

  async function load() {
    if (!csrf) return;
    active.current?.abort();
    setBusy(true);
    setError("");
    try {
      const answer = await request<View>("view", {});
      setView(answer);
      const first = answer.slots.find((slot) => !slot.created);
      if (
        !answer.slots.some((slot) => slot.slot_id === selected && !slot.created)
      ) {
        setSelected(first?.slot_id ?? "");
        setActors(first?.actor_ids.slice(0, 1) ?? []);
      }
    } catch (cause) {
      setError(readFailure(cause));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    void load();
    return () => active.current?.abort();
  }, [csrf]);

  async function unlock(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await request("unlock", { password });
      setPassword("");
      await load();
    } catch (cause) {
      setError(readFailure(cause));
      setBusy(false);
    }
  }

  async function create(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      const created = await request<Created>("create", {
        slot_id: selected,
        actor_ids: actors,
      });
      setSecret(created);
      await load();
    } catch (cause) {
      setError(readFailure(cause));
      setBusy(false);
    }
  }

  async function change(
    connection_id: string,
    operation: "enable" | "disable" | "rotate",
  ) {
    setBusy(true);
    setError("");
    try {
      const result = await request<Created>(operation, { connection_id });
      if (result.token) setSecret(result);
      await load();
    } catch (cause) {
      setError(readFailure(cause));
      setBusy(false);
    }
  }

  const slot = view?.slots.find((item) => item.slot_id === selected);
  return (
    <section className="panel bot-connections" aria-label="机器人连接管理">
      <div className="section-heading">
        <h2>机器人连接</h2>
        <button className="button" onClick={() => void load()} disabled={busy}>
          刷新状态
        </button>
      </div>
      <p>
        先由部署管理员安装对应插件并登记机器人、会话和作者；在这里选择角色、创建连接并复制一次性凭据。在线仅表示插件已认证，真实收发需另行测试。
      </p>
      {error && (
        <p className="bot-error" role="alert">
          {error}
        </p>
      )}
      {!view && !error && <p>正在读取连接…</p>}
      {view && !view.available && (
        <div>
          <p>本部署尚未登记机器人连接。</p>
          <ol>
            <li>
              在机器人宿主安装 NoneBot 或 AstrBot 插件，并确认机器人账号。
            </li>
            <li>请部署管理员登记允许会话、作者、角色与对应 Core 绑定。</li>
            <li>部署更新后回到此页创建连接，复制一次性凭据到插件私有配置。</li>
          </ol>
        </div>
      )}
      {view &&
        view.available &&
        view.management.code === "operator_not_authorized" && (
          <p>当前账号没有机器人连接管理权限。</p>
        )}
      {view && view.available && view.slots.length === 0 && (
        <p>
          尚无可用槽位。请部署管理员按 NoneBot 或 AstrBot 登记样例配置宿主
          ID、机器人账号、允许会话、作者和角色，再更新平台与 Core。
        </p>
      )}
      {view &&
        view.available &&
        view.management.code !== "operator_not_authorized" && (
          <>
            {!view.management.unlocked && (
              <form
                className="bot-form"
                onSubmit={(event) => void unlock(event)}
              >
                <label>
                  管理员密码{" "}
                  <input
                    type="password"
                    autoComplete="current-password"
                    value={password}
                    onChange={(event) => setPassword(event.target.value)}
                    required
                    minLength={12}
                    maxLength={256}
                  />
                </label>
                <button className="button" disabled={busy}>
                  解锁连接管理
                </button>
              </form>
            )}
            {secret?.token && (
              <div className="bot-secret" role="status">
                <strong>一次性连接凭据</strong>
                <p>连接 ID：{secret.connection_id}</p>
                <code>{secret.token}</code>
                <p>
                  请立即保存到对应插件的私有设置。关闭后平台不会再次显示此凭据。
                </p>
                <button className="button" onClick={() => setSecret(null)}>
                  已保存，隐藏凭据
                </button>
              </div>
            )}
            {view.management.unlocked &&
              view.slots.some((item) => !item.created) && (
                <form
                  className="bot-form"
                  onSubmit={(event) => void create(event)}
                >
                  <label>
                    连接槽位
                    <select
                      value={selected}
                      onChange={(event) => {
                        const next = view.slots.find(
                          (item) => item.slot_id === event.target.value,
                        );
                        setSelected(event.target.value);
                        setActors(next?.actor_ids.slice(0, 1) ?? []);
                      }}
                    >
                      {view.slots
                        .filter((item) => !item.created)
                        .map((item) => (
                          <option key={item.slot_id} value={item.slot_id}>
                            {item.label} ·{" "}
                            {item.adapter === "nonebot" ? "NoneBot" : "AstrBot"}
                          </option>
                        ))}
                    </select>
                  </label>
                  {slot && (
                    <p>
                      渠道 {slot.namespace} · 会话 {slot.conversation_id} ·
                      已登记作者 {slot.registered_authors} 人。宿主 ID{" "}
                      {slot.platform_id}，机器人 ID {slot.self_id}。
                    </p>
                  )}
                  <fieldset>
                    <legend>允许回复的角色</legend>
                    {slot?.actor_ids.map((id) => (
                      <label key={id}>
                        <input
                          type="checkbox"
                          checked={actors.includes(id)}
                          onChange={(event) =>
                            setActors(
                              event.target.checked
                                ? [...actors, id]
                                : actors.filter((value) => value !== id),
                            )
                          }
                        />
                        {id}
                      </label>
                    ))}
                  </fieldset>
                  <button
                    className="button"
                    disabled={busy || !selected || actors.length === 0}
                  >
                    创建连接
                  </button>
                </form>
              )}
            <ul className="bot-list">
              {view.connections.map((connection) => {
                const selectedSlot = view.slots.find(
                  (item) => item.slot_id === connection.slot_id,
                );
                return (
                  <li key={connection.connection_id}>
                    <div>
                      <strong>
                        {selectedSlot?.label ?? connection.slot_id}
                      </strong>
                      <p>
                        {connection.adapter === "nonebot"
                          ? "NoneBot"
                          : "AstrBot"}{" "}
                        · {selectedSlot?.conversation_id} · 角色{" "}
                        {connection.actor_ids.join("、")}
                      </p>
                      <small>
                        最近认证：{stamp(connection.last_seen_at)} ·
                        最近有效事件：{stamp(connection.last_event_at)}
                        {connection.last_error
                          ? ` · ${connection.last_error}`
                          : ""}
                      </small>
                      <p>
                        回复记录：已发送 {connection.delivery.sent} · 待领取{" "}
                        {connection.delivery.pending} · 已领取{" "}
                        {connection.delivery.claimed} · 未知{" "}
                        {connection.delivery.unknown} · 失败{" "}
                        {connection.delivery.failed}
                      </p>
                    </div>
                    <StatusRail
                      tone={state[connection.state].tone}
                      label={state[connection.state].label}
                    />
                    {view.management.unlocked && (
                      <div className="bot-actions">
                        <button
                          className="button"
                          onClick={() =>
                            void change(
                              connection.connection_id,
                              connection.enabled ? "disable" : "enable",
                            )
                          }
                          disabled={busy}
                        >
                          {connection.enabled ? "停用" : "启用"}
                        </button>
                        <button
                          className="button"
                          onClick={() =>
                            void change(connection.connection_id, "rotate")
                          }
                          disabled={busy}
                        >
                          轮换凭据
                        </button>
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </>
        )}
    </section>
  );
}
