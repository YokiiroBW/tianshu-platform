import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { RefreshCw, ShieldCheck } from "lucide-react";
import { LoginLink, useSessionGuard } from "../../app/Auth";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import { requestId } from "../../app/requestId";
import {
  call,
  session as readSession,
  WebError,
  type ModelsView,
  type SessionState,
} from "./api";
import {
  ProviderManager,
  type Provider,
  type ProviderActions,
  type ProviderDraft,
} from "./ProviderManager";
import { readProviders } from "./providerApi";
import "./models.css";

type ProviderView = { providers: Provider[]; defaultRevision: number };

function errorMessage(cause: unknown) {
  if (cause instanceof WebError) {
    if (cause.code === "result_unknown")
      return `${cause.message} 页面不会自动重试。`;
    return cause.executionState === "unknown"
      ? `${cause.message} 本次执行结果未知；请重新读取状态，不会自动重试。`
      : cause.message;
  }
  return "连接中断，本次操作结果未知。请重新读取状态，不会自动重试。";
}

export function ProviderModelsPanel() {
  const [session, setSession] = useState<SessionState | null>(null);
  useSessionGuard(session);
  const [management, setManagement] = useState<ModelsView["management"] | null>(
    null,
  );
  const [providerView, setProviderView] = useState<ProviderView | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const active = useRef<AbortController | null>(null);
  const password = useRef<HTMLInputElement>(null);

  const start = useCallback(() => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    return controller;
  }, []);

  const load = useCallback(async (signal: AbortSignal) => {
    setBusy(true);
    setError("");
    try {
      const current = await readSession(signal);
      if (signal.aborted) return;
      setSession(current);
      if (!current.authenticated) {
        setManagement(null);
        setProviderView(null);
        return;
      }
      const view = await call<ModelsView>(
        "models/view",
        {},
        current.csrf,
        signal,
      );
      if (signal.aborted) return;
      setManagement(view.management);
      if (view.management.available && view.management.unlocked) {
        setProviderView(await readProviders(current.csrf, signal));
      } else {
        setProviderView(null);
      }
    } catch (cause) {
      if (!signal.aborted) {
        setProviderView(null);
        setError(errorMessage(cause));
        if (
          cause instanceof WebError &&
          ["session_expired", "unauthorized"].includes(cause.code)
        ) {
          try {
            setSession(await readSession(signal));
          } catch {
            setSession(null);
          }
        }
      }
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }, []);

  useEffect(() => {
    const controller = start();
    void load(controller.signal);
    return () => controller.abort();
  }, [load, start]);

  async function unlock(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!session) return;
    const controller = start();
    setBusy(true);
    setError("");
    try {
      await call(
        "models/unlock",
        { password: password.current?.value ?? "" },
        session.csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) {
        setNotice("模型管理已解锁，仅在当前会话有效。");
        await load(controller.signal);
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      if (password.current) password.current.value = "";
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function lock() {
    if (!session) return;
    const controller = start();
    setBusy(true);
    try {
      await call("models/lock", {}, session.csrf, controller.signal);
      if (!controller.signal.aborted) {
        setProviderView(null);
        setNotice("已锁定模型管理；聊天登录保持有效。");
        await load(controller.signal);
      }
    } catch (cause) {
      if (!controller.signal.aborted) setError(errorMessage(cause));
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  async function reloadProviders(signal: AbortSignal) {
    if (!session) throw new WebError("session_expired", 401);
    const result = await readProviders(session.csrf, signal);
    if (!signal.aborted) setProviderView(result);
  }

  async function invoke<T>(
    operation: string,
    body: object,
    signal: AbortSignal,
  ): Promise<T> {
    if (!session) throw new WebError("session_expired", 401);
    return call<T>(`providers/${operation}`, body, session.csrf, signal);
  }

  const actions: ProviderActions = {
    save: async (draft: ProviderDraft, original, signal) => {
      const body = {
        client_id: requestId(),
        ...(original
          ? {
              provider_id: original.providerId,
              expected_revision: original.revision,
            }
          : {}),
        name: draft.name,
        protocol: draft.protocol,
        base_url: draft.baseUrl,
        model_id: draft.modelId,
        enabled: original?.enabled ?? true,
        ...(draft.apiKey ? { api_key: draft.apiKey } : {}),
      };
      await invoke("save", body, signal);
      await reloadProviders(signal);
    },
    listModels: async (provider, signal) => {
      const result = await invoke<{
        provider_id: string;
        revision: number;
        models: string[];
      }>(
        "models",
        {
          provider_id: provider.providerId,
          expected_revision: provider.revision,
        },
        signal,
      );
      if (
        result.provider_id !== provider.providerId ||
        result.revision !== provider.revision ||
        !Array.isArray(result.models) ||
        !result.models.every((item) => typeof item === "string")
      )
        throw new WebError("dependency_unavailable", 502);
      return result.models;
    },
    test: async (provider, signal) => {
      const result = await invoke<{
        provider_id: string;
        revision: number;
        outcome: string;
      }>(
        "test",
        {
          client_id: requestId(),
          provider_id: provider.providerId,
          expected_revision: provider.revision,
        },
        signal,
      );
      if (
        result.provider_id !== provider.providerId ||
        result.revision !== provider.revision ||
        result.outcome !== "succeeded"
      )
        throw new WebError("dependency_unavailable", 502);
      await reloadProviders(signal);
    },
    setDefault: async (provider, signal) => {
      if (!providerView) throw new WebError("dependency_unavailable", 503);
      await invoke(
        "default",
        {
          client_id: requestId(),
          provider_id: provider.providerId,
          expected_revision: provider.revision,
          expected_default_revision: providerView.defaultRevision,
        },
        signal,
      );
      await reloadProviders(signal);
    },
    setEnabled: async (provider, enabled, signal) => {
      await invoke(
        "save",
        {
          client_id: requestId(),
          provider_id: provider.providerId,
          expected_revision: provider.revision,
          name: provider.name,
          protocol: provider.protocol,
          base_url: provider.baseUrl,
          model_id: provider.modelId,
          enabled,
        },
        signal,
      );
      await reloadProviders(signal);
    },
    clearKey: async (provider, signal) => {
      await invoke(
        "clear-key",
        {
          client_id: requestId(),
          provider_id: provider.providerId,
          expected_revision: provider.revision,
        },
        signal,
      );
      await reloadProviders(signal);
    },
    remove: async (provider, signal) => {
      await invoke(
        "delete",
        {
          client_id: requestId(),
          provider_id: provider.providerId,
          expected_revision: provider.revision,
        },
        signal,
      );
      await reloadProviders(signal);
    },
  };

  function handleProviderError(cause: unknown) {
    const message = errorMessage(cause);
    setError(message);
    const controller = start();
    void (async () => {
      try {
        if (
          cause instanceof WebError &&
          ["session_expired", "unauthorized", "management_required"].includes(
            cause.code,
          )
        ) {
          await load(controller.signal);
        } else {
          await reloadProviders(controller.signal);
        }
      } catch {
        // Keep the original action error. The explicit refresh button remains available.
      } finally {
        if (!controller.signal.aborted) setError(message);
      }
    })();
  }

  return (
    <section
      className="panel models"
      aria-label="模型供应商管理"
      aria-busy={busy}
    >
      <div className="section-heading">
        <h2>模型配置</h2>
        {management && (
          <StatusRail
            tone={
              !management.available
                ? "gray"
                : management.unlocked
                  ? "blue"
                  : "yellow"
            }
            label={
              !management.available
                ? "管理不可用"
                : management.unlocked
                  ? "管理已解锁"
                  : "管理待验证"
            }
          />
        )}
      </div>
      <p className="muted">
        管理你的模型服务，确认短回复测试后设置默认对话模型。
      </p>
      <div role="status" className="models-status">
        {busy
          ? "正在读取模型配置…"
          : session?.authenticated
            ? `已登录 · ${session.username}`
            : "请登录管理员账号"}
      </div>
      {error && (
        <p className="models-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="models-notice" role="status">
          {notice}
        </p>
      )}
      {!session ? (
        <StatePanel kind="loading" title="正在连接模型配置入口">
          <p>稍候。</p>
        </StatePanel>
      ) : !session.authenticated ? (
        <LoginLink />
      ) : (
        <>
          <div className="models-actions">
            <button
              className="button"
              onClick={() => void load(start().signal)}
              disabled={busy}
            >
              <RefreshCw aria-hidden="true" />
              重新读取
            </button>
            {management?.unlocked && (
              <button
                className="button"
                onClick={() => void lock()}
                disabled={busy}
              >
                锁定管理
              </button>
            )}
          </div>
          {management?.available === false && (
            <StatePanel
              kind="unconfigured"
              title={
                management.code === "management_disabled"
                  ? "此部署尚未开启模型管理"
                  : "当前账号没有模型管理权限"
              }
            >
              <p>请检查管理员账号与服务配置。</p>
            </StatePanel>
          )}
          {management?.available && !management.unlocked && (
            <div className="models-unlock">
              <ShieldCheck aria-hidden="true" />
              <h3>验证管理员密码后管理供应商</h3>
              <p className="muted">独立管理授权仅在当前登录会话有效。</p>
              <form onSubmit={(event) => void unlock(event)}>
                <label>
                  管理员密码
                  <input
                    ref={password}
                    type="password"
                    autoComplete="current-password"
                    minLength={12}
                    maxLength={256}
                    required
                    disabled={busy}
                  />
                </label>
                <button
                  className="button primary"
                  type="submit"
                  disabled={busy}
                >
                  解锁模型管理
                </button>
              </form>
            </div>
          )}
          {management?.unlocked && providerView && (
            <ProviderManager
              providers={providerView.providers}
              editable
              actions={actions}
              onError={handleProviderError}
            />
          )}
        </>
      )}
    </section>
  );
}
