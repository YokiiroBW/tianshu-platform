import { useEffect, useRef, useState, type FormEvent } from "react";
import { requestId } from "../../app/requestId";
import { adapterPost } from "./botAdapterApi";

type Server = {
  id: string;
  name: string;
  token: string;
  account_id: string | null;
  connected: boolean;
  observation_id: string | null;
  observation_state: string | null;
  ws_path: string;
};
type ServerView =
  | { available: false; servers: [] }
  | { available: true; ws_port: number; servers: Server[] };

function address(port: number, path: string) {
  const hostname = window.location.hostname;
  const host =
    hostname.includes(":") && !hostname.startsWith("[")
      ? `[${hostname}]`
      : hostname;
  return `ws://${host}:${port}${path}`;
}

async function copy(value: string) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const input = document.createElement("textarea");
  input.value = value;
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.appendChild(input);
  input.select();
  try {
    if (!document.execCommand("copy")) throw new Error("copy failed");
  } finally {
    input.remove();
  }
}

export function OneBotOnboarding({
  csrf,
  onChanged,
}: {
  csrf: string;
  onChanged: () => void;
}) {
  const [view, setView] = useState<ServerView | null>(null);
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [revealed, setRevealed] = useState<string[]>([]);
  const lastObservations = useRef("");
  const attempt = useRef<{ name: string; token: string; id: string } | null>(
    null,
  );
  const changed = useRef(onChanged);
  changed.current = onChanged;

  useEffect(() => {
    const controller = new AbortController();
    async function refresh() {
      try {
        const result = await adapterPost<ServerView>(
          "onebot-servers",
          {},
          csrf,
          controller.signal,
        );
        if (controller.signal.aborted) return;
        setView(result);
        setError("");
        const ids = result.servers
          .map((server) => server.observation_id ?? "")
          .join(",");
        if (lastObservations.current !== ids) {
          lastObservations.current = ids;
          changed.current();
        }
      } catch (cause) {
        if (!controller.signal.aborted)
          setError(
            cause instanceof Error ? cause.message : "无法读取机器人状态。",
          );
      }
    }
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3000);
    return () => {
      window.clearInterval(timer);
      controller.abort();
    };
  }, [csrf]);

  async function create(event: FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const serverName = name.trim();
      if (
        !attempt.current ||
        attempt.current.name !== serverName ||
        attempt.current.token !== token
      )
        attempt.current = { name: serverName, token, id: requestId() };
      const result = await adapterPost<ServerView>(
        "onebot-create",
        { name: serverName, token, client_id: attempt.current.id },
        csrf,
        new AbortController().signal,
      );
      setView(result);
      attempt.current = null;
      setName("");
      setToken("");
      setNotice(
        "机器人已创建。在 SnowLuma 中填写它的地址和 Token，连接后会自动关联 QQ。",
      );
    } catch (cause) {
      setError(
        cause instanceof Error ? cause.message : "创建失败，请稍后重试。",
      );
    } finally {
      setBusy(false);
    }
  }

  async function copyField(value: string, label: string) {
    try {
      await copy(value);
      setNotice(`${label}已复制。`);
    } catch {
      setNotice(`未能复制${label}，请手动选择并复制。`);
    }
  }

  if (!view && !error) return <p className="muted">正在读取机器人服务…</p>;
  if (view && !view.available)
    return (
      <p className="muted">此部署未启用内置 OneBot，请选择外部宿主接入。</p>
    );

  return (
    <>
      {error && <p role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      <form className="bot-form" onSubmit={(event) => void create(event)}>
        <label>
          机器人名称
          <input
            required
            maxLength={64}
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="例如：QQ 1200"
          />
        </label>
        <label>
          WebSocket Token（留空自动生成）
          <input
            type="password"
            autoComplete="new-password"
            value={token}
            onChange={(event) => setToken(event.target.value)}
            placeholder="也可以自行填写，至少 16 位"
          />
        </label>
        <button className="button" disabled={busy || !view?.available}>
          创建机器人
        </button>
      </form>
      {view?.available &&
        view.servers.map((server) => (
          <section
            className="bot-observation-card bot-form"
            key={server.id}
            aria-label={server.name}
          >
            <h3>{server.name}</h3>
            <p role="status">
              {server.connected
                ? `已连接 QQ ${server.account_id}`
                : server.account_id
                  ? `QQ ${server.account_id} 已断开，等待重连`
                  : "等待客户端连接"}
              {server.connected && !server.observation_id
                ? " · 正在准备账号策略"
                : ""}
              {server.connected &&
              server.observation_id &&
              server.observation_state !== "ready"
                ? " · 账号策略待恢复"
                : ""}
            </p>
            <label>
              反向 WebSocket 地址
              <input
                readOnly
                value={address(view.ws_port, server.ws_path)}
                onFocus={(event) => event.currentTarget.select()}
              />
            </label>
            <button
              type="button"
              className="button"
              onClick={() =>
                void copyField(address(view.ws_port, server.ws_path), "地址")
              }
            >
              复制地址
            </button>
            <label>
              WebSocket Token
              <input
                readOnly
                autoComplete="off"
                type={revealed.includes(server.id) ? "text" : "password"}
                value={server.token}
                onFocus={(event) => event.currentTarget.select()}
              />
            </label>
            <div className="bot-onebot-actions">
              <button
                type="button"
                className="button"
                onClick={() => void copyField(server.token, "Token")}
              >
                复制 Token
              </button>
              <button
                type="button"
                className="button"
                onClick={() =>
                  setRevealed(
                    revealed.includes(server.id)
                      ? revealed.filter((id) => id !== server.id)
                      : [...revealed, server.id],
                  )
                }
              >
                {revealed.includes(server.id) ? "隐藏 Token" : "显示 Token"}
              </button>
            </div>
          </section>
        ))}
    </>
  );
}
