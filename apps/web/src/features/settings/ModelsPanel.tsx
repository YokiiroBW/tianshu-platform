import { useCallback, useEffect, useRef, useState } from "react";
import { LockKeyhole, RefreshCw, ShieldCheck } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import {
  call,
  session as readSession,
  WebError,
  type ModelsView,
  type Preview,
  type Publication,
  type SessionState,
  type Target,
} from "./api";
import "./models.css";

const tone: Record<string, "blue" | "yellow" | "red" | "gray"> = {
  available: "blue",
  expired: "yellow",
  revoked: "red",
  unconfigured: "gray",
};
const wording: Record<string, string> = {
  available: "可用",
  expired: "已到期",
  revoked: "已撤销",
  unconfigured: "未配置",
};

/** Unknown availability stays unknown: it is never shown as healthy or stopped. */
function rail(availability: string) {
  return {
    tone: tone[availability] ?? "gray",
    label: wording[availability] ?? "未知",
  };
}

function stamp(value: string | null) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("zh-CN", { hour12: false });
}

function reason(cause: unknown) {
  return cause instanceof Error ? cause.message : "连接中断，请重新连接。";
}

export function ModelsPanel() {
  const [session, setSession] = useState<SessionState | null>(null);
  const [view, setView] = useState<ModelsView | null>(null);
  const [busy, setBusy] = useState(true);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [templateId, setTemplateId] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const active = useRef<AbortController | null>(null);
  const password = useRef<HTMLInputElement>(null);

  const start = useCallback(() => {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    return controller;
  }, []);

  const load = useCallback(async (signal: AbortSignal, retry = true) => {
    setBusy(true);
    try {
      const state = await readSession(signal);
      setSession(state);
      if (!state.authenticated) {
        setView(null);
        setPreview(null);
        return;
      }
      try {
        const result = await call<ModelsView>(
          "models/view",
          {},
          state.csrf,
          signal,
        );
        setView(result);
        setTemplateId(
          (current) =>
            current || result.management.templates[0]?.template_id || "",
        );
      } catch (cause) {
        // A revoked or expired login is a state, not a stale panel: re-read once, then show it.
        setView(null);
        setPreview(null);
        if (
          retry &&
          cause instanceof WebError &&
          ["session_expired", "unauthorized"].includes(cause.code)
        ) {
          await load(signal, false);
          setError(cause.message);
          return;
        }
        throw cause;
      }
    } catch (cause) {
      if (!signal.aborted) setError(reason(cause));
    } finally {
      if (!signal.aborted) setBusy(false);
    }
  }, []);

  useEffect(() => {
    const controller = start();
    void load(controller.signal);
    return () => {
      controller.abort();
    };
  }, [load, start]);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      setError("");
      await load(signal ?? start().signal, false);
    },
    [load, start],
  );

  /** Every write goes through the real session, CSRF header and server-side authority. */
  const submit = useCallback(
    async (path: string, body: object) => {
      if (!session) return null;
      const controller = start();
      setBusy(true);
      setError("");
      setNotice("");
      try {
        return await call<Record<string, unknown>>(
          path,
          body,
          session.csrf,
          controller.signal,
        );
      } catch (cause) {
        if (controller.signal.aborted) return null;
        const message = reason(cause);
        const code = cause instanceof WebError ? cause.code : "";
        setError(message);
        if (
          ["session_expired", "unauthorized", "management_required"].includes(
            code,
          )
        ) {
          setPreview(null);
          await refresh(controller.signal);
          // The re-read clears the panel error, so the reason is restored after it.
          setError(message);
        } else if (code === "version_conflict") {
          // Another browser published first: drop the stale plan and re-read reality.
          setPreview(null);
          await refresh(controller.signal);
          setError(message);
          setNotice("已按后台当前版本重新读取；请重新预览后再发布。");
        }
        return null;
      } finally {
        if (!controller.signal.aborted) setBusy(false);
      }
    },
    [refresh, session, start],
  );

  async function authenticate(form: HTMLFormElement) {
    const data = new FormData(form);
    const result = await submit("login", {
      username: String(data.get("username")),
      password: String(data.get("password")),
    });
    if (password.current) password.current.value = "";
    if (result) await refresh();
  }

  async function unlock(form: HTMLFormElement) {
    const data = new FormData(form);
    const result = await submit("models/unlock", {
      password: String(data.get("password")),
    });
    if (password.current) password.current.value = "";
    if (result) {
      setNotice("模型管理已解锁；解锁只作用于当前会话。");
      await refresh();
    }
  }

  async function lock() {
    const result = await submit("models/lock", {});
    if (result) {
      setPreview(null);
      setNotice("已锁定模型管理；聊天登录保持有效。");
      await refresh();
    }
  }

  async function previewTemplate() {
    const result = await submit("models/preview", { template_id: templateId });
    if (result) setPreview(result as unknown as Preview);
  }

  async function publish() {
    if (!preview) return;
    const result = await submit("models/publish", {
      template_id: preview.template.template_id,
      expected_version: preview.expected_version,
      client_id: crypto.randomUUID(),
    });
    if (!result) return;
    const publication = result as unknown as Publication;
    setPreview(null);
    setNotice(
      publication.state === "replayed"
        ? `这次请求此前已执行，没有重复创建版本（版本 ${publication.version}）。`
        : `已发布版本 ${publication.version}，可用至 ${stamp(publication.usable_until)}。`,
    );
    await refresh();
  }

  async function revoke(target: string, version: number) {
    const result = await submit("models/revoke", { target, version });
    if (!result) return;
    setPreview(null);
    setNotice(`已撤销版本 ${version}；该版本对所有读取方都不再可用。`);
    await refresh();
  }

  const management = view?.management;
  const locked = management?.available === true && !management.unlocked;

  const targetPanels = view?.targets.map((target: Target) => {
    const state = rail(target.availability);
    return (
      <article className="models-target" key={target.target}>
        <div className="models-target-head">
          <div>
            <h3>{target.label}</h3>
            <p className="muted">{target.detail}</p>
          </div>
          <StatusRail tone={state.tone} label={state.label} />
        </div>
        <dl className="models-facts">
          <div>
            <dt>当前版本</dt>
            <dd>{target.current_version ?? "未发布"}</dd>
          </div>
          <div>
            <dt>当前版本可用至</dt>
            <dd>{stamp(target.usable_until)}</dd>
          </div>
          <div>
            <dt>工作负载</dt>
            <dd>{target.workload}</dd>
          </div>
        </dl>
        {target.versions.length ? (
          <ul className="models-versions">
            {target.versions.map((item) => {
              const itemState = rail(item.availability);
              return (
                <li key={item.version}>
                  <StatusRail tone={itemState.tone} label={itemState.label}>
                    <span className="models-version">版本 {item.version}</span>
                  </StatusRail>
                  <p className="muted">
                    发布 {stamp(item.published_at)} · 可用至{" "}
                    {stamp(item.usable_until)}
                  </p>
                  <p className="muted">
                    {item.providers
                      .map(
                        (provider) =>
                          `${provider.provider_id} · ${provider.model_id} · ${provider.protocol}`,
                      )
                      .join("；") || "没有登记的提供方"}
                  </p>
                  <p className="muted">
                    {item.bindings
                      .map(
                        (binding) =>
                          `${binding.workload} → ${binding.model_id}`,
                      )
                      .join("；") || "没有绑定"}
                  </p>
                  {management?.unlocked && !item.revoked && (
                    <button
                      className="button"
                      onClick={() => void revoke(target.target, item.version)}
                      disabled={busy}
                    >
                      撤销版本 {item.version}
                    </button>
                  )}
                </li>
              );
            })}
          </ul>
        ) : (
          <p className="muted">这个目标还没有发布任何版本。</p>
        )}
      </article>
    );
  });

  return (
    <section
      className="panel models"
      aria-label="模型配置管理"
      aria-busy={busy}
    >
      <div className="section-heading">
        <h2>模型配置</h2>
        {management && (
          <StatusRail
            tone={
              management.available
                ? management.unlocked
                  ? "blue"
                  : "yellow"
                : "gray"
            }
            label={
              management.available
                ? management.unlocked
                  ? "管理已解锁"
                  : "管理待验证"
                : management.code === "management_disabled"
                  ? "管理未开启"
                  : "无管理权限"
            }
          />
        )}
      </div>
      <p className="muted">
        这里发布与撤销的是服务器登记并审查过的配置模板：浏览器只提交模板标识与所选版本，
        提供方地址、凭据引用与有效期都由服务器填写，页面不会收到密钥或内部标识。
      </p>
      <div role="status" className="models-status">
        {busy
          ? "正在读取模型配置…"
          : session?.authenticated
            ? `已登录 · ${session.username}`
            : "请登录本机管理员账号"}
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
        <StatePanel kind="loading" title="正在连接网页配置入口">
          <p>稍候。</p>
        </StatePanel>
      ) : !session.authenticated ? (
        <div className="models-login">
          <LockKeyhole aria-hidden="true" />
          <p className="muted">
            模型配置管理沿用真实登录会话，普通聊天登录不包含管理权限。
          </p>
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
                disabled={busy}
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
                disabled={busy}
              />
            </label>
            <button className="button primary" type="submit" disabled={busy}>
              登录
            </button>
          </form>
        </div>
      ) : (
        <>
          <div className="models-actions">
            <button
              className="button"
              onClick={() => void refresh()}
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
                  ? "这个部署没有开启网页模型管理"
                  : "当前账号没有模型配置权限"
              }
            >
              <p>
                {management.code === "management_disabled"
                  ? "设置里的 web_models 未开启，网页只能读取版本，不能发布或撤销。"
                  : "账号需要 config.publish、config.revoke 与 config.view 权限才能管理模型配置。"}
              </p>
            </StatePanel>
          )}
          {locked && (
            <div className="models-unlock">
              <ShieldCheck aria-hidden="true" />
              <h3>验证管理员密码后解锁模型管理</h3>
              <p className="muted">
                这一步是独立授权：解锁只作用于当前会话，到期后需要重新验证。
              </p>
              <form
                onSubmit={(event) => {
                  event.preventDefault();
                  void unlock(event.currentTarget);
                }}
              >
                <label>
                  管理员密码
                  <input
                    ref={password}
                    name="password"
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
          {management?.unlocked && (
            <>
              <div className="models-toolbar">
                <label>
                  配置模板
                  <select
                    value={templateId}
                    disabled={busy || !management.templates.length}
                    onChange={(event) => {
                      setTemplateId(event.target.value);
                      setPreview(null);
                    }}
                  >
                    {!management.templates.length && (
                      <option value="">没有可用模板</option>
                    )}
                    {management.templates.map((template) => (
                      <option
                        key={template.template_id}
                        value={template.template_id}
                      >
                        {template.label}（{template.target}）
                      </option>
                    ))}
                  </select>
                </label>
                <button
                  className="button"
                  onClick={() => void previewTemplate()}
                  disabled={busy || !templateId}
                >
                  预览
                </button>
              </div>
              {preview && (
                <div className="models-preview">
                  <div className="models-target-head">
                    <h3>发布预览</h3>
                    <span className="badge">尚未发布</span>
                  </div>
                  <dl className="models-facts">
                    <div>
                      <dt>模板</dt>
                      <dd>{preview.template.label}</dd>
                    </div>
                    <div>
                      <dt>目标</dt>
                      <dd>{preview.target}</dd>
                    </div>
                    <div>
                      <dt>将发布版本</dt>
                      <dd>{preview.version}</dd>
                    </div>
                    <div>
                      <dt>基于当前版本</dt>
                      <dd>{preview.expected_version ?? "未发布"}</dd>
                    </div>
                    <div>
                      <dt>有效期至</dt>
                      <dd>{stamp(preview.usable_until)}</dd>
                    </div>
                    <div>
                      <dt>绑定</dt>
                      <dd>
                        {preview.bindings
                          .map(
                            (binding) =>
                              `${binding.workload} → ${binding.model_id}`,
                          )
                          .join("；")}
                      </dd>
                    </div>
                  </dl>
                  <div className="models-actions">
                    <button
                      className="button primary"
                      onClick={() => void publish()}
                      disabled={busy}
                    >
                      发布版本 {preview.version}
                    </button>
                    <button
                      className="button"
                      onClick={() => setPreview(null)}
                      disabled={busy}
                    >
                      放弃预览
                    </button>
                  </div>
                </div>
              )}
            </>
          )}
          {targetPanels}
        </>
      )}
    </section>
  );
}
