import { LoginLink, useSessionGuard } from "../../app/Auth";
import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import "./companion.css";

import { request, type Session } from "./api";
import { DialoguePanel } from "./DialoguePanel";

export default function CompanionPage() {
  const [session, setSession] = useState<Session | null>(null);
  useSessionGuard(session);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [conversationId, setConversationId] = useState("");
  const [actorId, setActorId] = useState("");
  const active = useRef<AbortController | null>(null);
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
          {session?.authenticated && (
            <a className="button" href="#/memory/0">
              用户档案与关系
            </a>
          )}
          <button
            className="button"
            onClick={() => void connect()}
            disabled={busy}
          >
            <RefreshCw aria-hidden="true" />
            重新连接
          </button>
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
        <LoginLink />
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
