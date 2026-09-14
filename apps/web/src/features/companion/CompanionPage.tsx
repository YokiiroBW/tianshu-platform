import { useEffect, useRef, useState } from "react";
import { RefreshCw, LogOut, LockKeyhole } from "lucide-react";
import "./companion.css";

import { request, type Session } from "./api";
import { DialoguePanel } from "./DialoguePanel";

export default function CompanionPage() {
  const [session, setSession] = useState<Session | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [conversationId, setConversationId] = useState("");
  const [actorId, setActorId] = useState("");
  const active = useRef<AbortController | null>(null);
  const password = useRef<HTMLInputElement>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const conversations = session?.conversations ?? [];
  const conversation =
    conversations.find((item) => item.id === conversationId) ??
    conversations[0];
  const actors = conversation?.actors ?? [];
  const actor = actors.includes(actorId) ? actorId : (actors[0] ?? "");

  async function connect() {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      const result = await request("session", controller.signal);
      setSession(result);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setSession(null);
        setError(
          cause instanceof Error ? cause.message : "连接中断，请重新连接。",
        );
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  useEffect(() => {
    void connect();
    const refresh = () => {
      if (!document.hidden) void connect();
    };
    window.addEventListener("online", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      active.current?.abort();
      window.removeEventListener("online", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, []);

  async function authenticate(form: HTMLFormElement) {
    if (!session) return;
    const data = new FormData(form);
    const controller = new AbortController();
    active.current?.abort();
    active.current = controller;
    setBusy(true);
    setError("");
    const credentials = {
      username: String(data.get("username")),
      password: String(data.get("password")),
    };
    if (password.current) password.current.value = "";
    try {
      await request("login", controller.signal, credentials, session.csrf);
      await connect();
      heading.current?.focus();
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(cause instanceof Error ? cause.message : "登录失败，请重试。");
        setBusy(false);
        password.current?.focus();
      }
    }
  }

  async function logout() {
    if (!session) return;
    const controller = new AbortController();
    active.current?.abort();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      await request("logout", controller.signal, {}, session.csrf);
      setSession(null);
      await connect();
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(
          cause instanceof Error
            ? `退出未确认：${cause.message}`
            : "退出未确认，请重新连接后重试。",
        );
        setBusy(false);
      }
    }
  }

  return (
    <section
      className="chat-console glass"
      aria-label="网页文字对话"
      aria-busy={busy}
    >
      <div className="chat-toolbar">
        <div>
          <p className="eyebrow">同源 · 私人对话</p>
          <h2 ref={heading} tabIndex={-1}>
            留一点时间，慢慢聊
          </h2>
        </div>
        <div className="chat-actions">
          <button
            className="button"
            onClick={() => void connect()}
            disabled={busy}
          >
            <RefreshCw aria-hidden="true" />
            重新连接
          </button>
          {session?.authenticated && (
            <button
              className="button"
              onClick={() => void logout()}
              disabled={busy}
            >
              <LogOut aria-hidden="true" />
              退出登录
            </button>
          )}
        </div>
      </div>
      <div role="status" className="chat-status">
        {busy
          ? "正在连接…"
          : session?.authenticated
            ? `已登录 · ${session.username}`
            : "请登录家庭管理员账号"}
      </div>
      {error && (
        <p className="chat-error" role="alert">
          {error}
        </p>
      )}
      {!session?.authenticated ? (
        <div className="chat-login">
          <LockKeyhole aria-hidden="true" />
          <h3>从自己的账号开始</h3>
          <p className="muted">登录后仅显示已授权的角色与会话。</p>
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
              className="button primary"
              type="submit"
              disabled={!session || busy}
            >
              登录
            </button>
          </form>
          <p className="muted">
            没有默认账号。仅保存本次浏览器会话，退出后清除。
          </p>
        </div>
      ) : (
        <>
          <div className="chat-selectors">
            <label>
              会话
              <select
                value={conversation?.id ?? ""}
                disabled={busy || !conversations.length}
                onChange={(event) => {
                  setConversationId(event.target.value);
                  setActorId("");
                }}
              >
                {!conversations.length && (
                  <option value="">没有已授权会话</option>
                )}
                {conversations.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              角色
              <select
                value={actor}
                disabled={busy || !actors.length}
                onChange={(event) => {
                  setActorId(event.target.value);
                }}
              >
                {!actors.length && <option value="">没有已授权角色</option>}
                {actors.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <DialoguePanel
            key={`${conversation?.id}:${actor}:${session.csrf}`}
            session={session}
            conversation={conversation?.id ?? ""}
            actor={actor}
          />
        </>
      )}
    </section>
  );
}
