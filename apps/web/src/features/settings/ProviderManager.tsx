import { useEffect, useRef, useState, type FormEvent } from "react";
import { StatusRail } from "../../components/StatusRail";

/** Public, redacted fields only. The secret is never part of a provider view. */
export type Provider = {
  providerId: string;
  name: string;
  protocol: "openai-chat-completions";
  baseUrl: string;
  modelId: string;
  enabled: boolean;
  revision: number;
  hasKey: boolean;
  isDefault: boolean;
  test: {
    state: "passed" | "failed" | "untested";
    code: string | null;
    revision: number | null;
    at: string | null;
  };
};

export type ProviderDraft = {
  name: string;
  protocol: "openai-chat-completions";
  baseUrl: string;
  modelId: string;
  apiKey: string;
};

export type ProviderActions = {
  save: (
    draft: ProviderDraft,
    original: Provider | null,
    signal: AbortSignal,
  ) => Promise<void>;
  listModels: (provider: Provider, signal: AbortSignal) => Promise<string[]>;
  test: (provider: Provider, signal: AbortSignal) => Promise<void>;
  setDefault: (provider: Provider, signal: AbortSignal) => Promise<void>;
  setEnabled: (
    provider: Provider,
    enabled: boolean,
    signal: AbortSignal,
  ) => Promise<void>;
  clearKey: (provider: Provider, signal: AbortSignal) => Promise<void>;
  remove: (provider: Provider, signal: AbortSignal) => Promise<void>;
};

const presets = [
  { id: "custom", name: "自定义兼容服务", baseUrl: "" },
  { id: "openai", name: "OpenAI", baseUrl: "https://api.openai.com/v1" },
  { id: "deepseek", name: "DeepSeek", baseUrl: "https://api.deepseek.com" },
] as const;

const empty: ProviderDraft = {
  name: "",
  protocol: "openai-chat-completions",
  baseUrl: "",
  modelId: "",
  apiKey: "",
};

function stamp(value: string | null) {
  if (!value) return "尚无记录";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime())
    ? "时间未知"
    : parsed.toLocaleString("zh-CN", { hour12: false });
}

function tested(provider: Provider) {
  return (
    provider.test.state === "passed" &&
    provider.test.revision === provider.revision
  );
}

function testLabel(provider: Provider) {
  if (tested(provider)) return "测试通过";
  if (
    provider.test.state === "failed" &&
    provider.test.revision === provider.revision
  )
    return "测试失败";
  return "尚未测试";
}

const testHelp: Record<string, string> = {
  authentication_failed: "请检查 API Key，然后保存并重新测试。",
  endpoint_failed: "请检查 API 基础地址和兼容协议。",
  model_not_found: "请检查模型 ID，或重新读取列表。",
  timed_out: "测试超时，结果可能未知；请检查服务状态后再决定是否手动重试。",
  connection_failed: "请检查模型服务是否可连接。",
  cancelled: "已取消等待；如果服务已收到请求，仍可能继续执行。",
  unknown: "结果尚未确认。请先重新读取状态，不会自动重试。",
};

/** The UI is independent of HTTP paths until the platform contract is published. */
export function ProviderManager({
  providers,
  editable,
  actions,
  onError,
}: {
  providers: Provider[];
  editable: boolean;
  actions: ProviderActions;
  onError: (cause: unknown) => void;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [draft, setDraft] = useState<ProviderDraft>(empty);
  const [preset, setPreset] = useState<string>("custom");
  const [models, setModels] = useState<{
    providerId: string;
    items: string[];
  } | null>(null);
  const [confirmId, setConfirmId] = useState<string | null>(null);
  const [operation, setOperation] = useState("");
  const [message, setMessage] = useState("");
  const [formError, setFormError] = useState("");
  const active = useRef<AbortController | null>(null);
  const keyInput = useRef<HTMLInputElement>(null);
  const original =
    providers.find((item) => item.providerId === editingId) ?? null;

  useEffect(() => () => active.current?.abort(), []);

  function begin(provider: Provider | null) {
    active.current?.abort();
    setEditingId(provider?.providerId ?? null);
    setDraft(
      provider
        ? {
            name: provider.name,
            protocol: provider.protocol,
            baseUrl: provider.baseUrl,
            modelId: provider.modelId,
            apiKey: "",
          }
        : empty,
    );
    setPreset("custom");
    setModels(null);
    setConfirmId(null);
    setMessage("");
    setFormError("");
    setFormOpen(true);
  }

  async function run(
    name: string,
    task: (signal: AbortSignal) => Promise<void>,
    success: string,
  ) {
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setOperation(name);
    setMessage("");
    try {
      await task(controller.signal);
      if (!controller.signal.aborted) setMessage(success);
      return !controller.signal.aborted;
    } catch (cause) {
      if (!controller.signal.aborted) onError(cause);
      return false;
    } finally {
      if (!controller.signal.aborted) setOperation("");
    }
  }

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submitted = {
      ...draft,
      name: draft.name.trim(),
      baseUrl: draft.baseUrl.trim(),
      modelId: draft.modelId.trim(),
    };
    if (!submitted.name || !submitted.baseUrl) return;
    try {
      const url = new URL(submitted.baseUrl);
      if (url.protocol !== "https:") throw new Error();
    } catch {
      setFormError(
        "当前模型供应商接入要求 HTTPS 服务地址；天枢网页仍可从局域网 HTTP 入口打开。",
      );
      return;
    }
    setFormError("");
    const ok = await run(
      "save",
      (signal) => actions.save(submitted, original, signal),
      "已保存。保存不会发送测试请求；请单独测试回复。",
    );
    if (ok) {
      if (keyInput.current) keyInput.current.value = "";
      setDraft(empty);
      setFormOpen(false);
      setEditingId(null);
      setModels(null);
    }
  }

  async function enumerate(provider: Provider) {
    await run(
      "models",
      async (signal) => {
        const items = await actions.listModels(provider, signal);
        if (!signal.aborted)
          setModels({ providerId: provider.providerId, items });
      },
      "已读取模型列表；这不代表模型能生成回复。请选择模型后保存，再单独测试。",
    );
  }

  return (
    <div className="providers">
      <div className="section-heading">
        <div>
          <h3>模型供应商</h3>
          <p className="muted">
            添加服务、选择模型，再发送一条短测试，最后设为默认对话模型。
          </p>
        </div>
        {editable && (
          <button
            className="button primary"
            type="button"
            onClick={() => begin(null)}
            disabled={!!operation}
          >
            添加供应商
          </button>
        )}
      </div>
      {location.protocol === "http:" && (
        <p className="models-transport">
          当前使用局域网
          HTTP：网页与平台之间的传输未加密。请只在可信网络中输入密钥。
        </p>
      )}
      {message && (
        <p className="models-notice" role="status">
          {message}
        </p>
      )}
      {operation && (
        <div className="models-wait" role="status">
          正在处理
          {operation === "test"
            ? "短回复测试"
            : operation === "models"
              ? "模型列表"
              : "请求"}
          …{" "}
          <button
            className="button"
            type="button"
            onClick={() => {
              active.current?.abort();
              setOperation("");
              setMessage(
                "已取消等待；如果请求已到达服务端，结果可能仍在执行。不会自动重试，请重新读取状态。",
              );
            }}
          >
            取消等待
          </button>
        </div>
      )}
      {!providers.length && (
        <div className="models-empty">
          <h3>还没有供应商</h3>
          <p>
            添加一个使用 HTTPS 的 OpenAI Chat Completions
            兼容服务，即可开始配置对话模型。可使用预设地址，也可输入自己的服务地址。
          </p>
          {!editable && <p>验证管理员密码后可以添加。</p>}
        </div>
      )}
      <div className="providers-grid">
        {providers.map((provider) => (
          <article className="provider-card" key={provider.providerId}>
            <div className="models-target-head">
              <h3>{provider.name}</h3>
              <div className="provider-badges">
                {provider.isDefault && (
                  <StatusRail tone="blue" label="默认对话模型" />
                )}
                <StatusRail
                  tone={provider.enabled ? "blue" : "gray"}
                  label={provider.enabled ? "已启用" : "已停用"}
                />
              </div>
            </div>
            <p className="muted">Chat Completions 兼容 · {provider.baseUrl}</p>
            <dl className="models-facts">
              <div>
                <dt>模型</dt>
                <dd>{provider.modelId || "尚未选择"}</dd>
              </div>
              <div>
                <dt>密钥</dt>
                <dd>{provider.hasKey ? "已保存，不会回显" : "未设置"}</dd>
              </div>
              <div>
                <dt>短回复测试</dt>
                <dd>
                  {testLabel(provider)}
                  {provider.test.revision === provider.revision &&
                  provider.test.at
                    ? ` · ${stamp(provider.test.at)}`
                    : ""}
                </dd>
              </div>
            </dl>
            {editable && (
              <div className="models-actions">
                <button
                  className="button"
                  type="button"
                  disabled={!!operation}
                  onClick={() => begin(provider)}
                >
                  编辑
                </button>
                <button
                  className="button"
                  type="button"
                  disabled={!!operation}
                  onClick={() => void enumerate(provider)}
                >
                  获取模型
                </button>
                <button
                  className="button"
                  type="button"
                  disabled={
                    !!operation || !provider.enabled || !provider.modelId
                  }
                  onClick={() =>
                    void run(
                      "test",
                      (signal) => actions.test(provider, signal),
                      "短回复测试已提交；以重新读取的测试结果为准。",
                    )
                  }
                >
                  测试回复
                </button>
                <button
                  className="button primary"
                  type="button"
                  disabled={
                    !!operation ||
                    !provider.enabled ||
                    !tested(provider) ||
                    provider.isDefault
                  }
                  onClick={() =>
                    void run(
                      "default",
                      (signal) => actions.setDefault(provider, signal),
                      "已保存默认对话模型。请到对话页确认通道状态。",
                    )
                  }
                >
                  设为默认
                </button>
                <button
                  className="button"
                  type="button"
                  disabled={!!operation || provider.isDefault}
                  onClick={() =>
                    void run(
                      "enabled",
                      (signal) =>
                        actions.setEnabled(provider, !provider.enabled, signal),
                      provider.enabled
                        ? "已停用供应商。"
                        : "已启用供应商；请确认测试状态。",
                    )
                  }
                >
                  {provider.enabled ? "停用" : "启用"}
                </button>
                <button
                  className="button"
                  type="button"
                  disabled={
                    !!operation || !provider.hasKey || provider.isDefault
                  }
                  onClick={() =>
                    void run(
                      "key",
                      (signal) => actions.clearKey(provider, signal),
                      "已清除密钥；原测试状态不再有效。",
                    )
                  }
                >
                  清除密钥
                </button>
                <button
                  className="button"
                  type="button"
                  disabled={!!operation || provider.isDefault}
                  onClick={() => setConfirmId(provider.providerId)}
                >
                  删除
                </button>
              </div>
            )}
            {provider.isDefault && (
              <p className="muted">
                如需停用、清除密钥或删除，请先选择其他默认模型。
              </p>
            )}
            {!tested(provider) && provider.enabled && (
              <p className="muted">设为默认前，请先确认短回复测试通过。</p>
            )}
            {provider.test.revision === provider.revision &&
              provider.test.code &&
              testHelp[provider.test.code] && (
                <p className="models-error">{testHelp[provider.test.code]}</p>
              )}
            <p className="muted models-test-disclosure">
              短回复测试会向此服务发送一条简短测试内容，可能产生费用；不会发送聊天历史。模型列表读取不等于测试通过。
            </p>
            {models?.providerId === provider.providerId && (
              <div className="provider-models">
                <strong>可选模型</strong>
                {models.items.length ? (
                  <ul>
                    {models.items.map((id) => (
                      <li key={id}>
                        <button
                          className="button"
                          type="button"
                          disabled={!!operation}
                          onClick={() => {
                            begin(provider);
                            setDraft((current) => ({
                              ...current,
                              modelId: id,
                            }));
                          }}
                        >
                          {id}
                        </button>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p>服务未返回模型。仍可编辑并手动填写模型 ID。</p>
                )}
              </div>
            )}
            {confirmId === provider.providerId && (
              <div className="provider-confirm">
                <p>确定删除“{provider.name}”？此操作会移除保存的配置和密钥。</p>
                <button
                  className="button"
                  type="button"
                  disabled={!!operation}
                  onClick={() => setConfirmId(null)}
                >
                  保留
                </button>
                <button
                  className="button"
                  type="button"
                  disabled={!!operation}
                  onClick={() =>
                    void run(
                      "delete",
                      (signal) => actions.remove(provider, signal),
                      "供应商已删除。",
                    ).then((ok) => {
                      if (ok) setConfirmId(null);
                    })
                  }
                >
                  确认删除
                </button>
              </div>
            )}
          </article>
        ))}
      </div>
      {formOpen && editable && (
        <div className="provider-editor">
          <div className="models-target-head">
            <h3>{original ? `编辑 ${original.name}` : "添加供应商"}</h3>
            <button
              className="button"
              type="button"
              disabled={!!operation}
              onClick={() => {
                setFormOpen(false);
                setDraft(empty);
                setEditingId(null);
              }}
            >
              关闭
            </button>
          </div>
          <p className="muted">
            目前支持使用 HTTPS 的 OpenAI Chat Completions
            兼容服务。地址预设只填写服务地址，模型和密钥由你选择。
          </p>
          {original?.isDefault && (
            <p className="models-transport">
              编辑当前默认供应商会使旧测试失效，并需要重新测试、重新设为默认。期间对话可能不可用。
            </p>
          )}
          {formError && (
            <p className="models-error" role="alert">
              {formError}
            </p>
          )}
          <form onSubmit={(event) => void save(event)}>
            <label>
              服务预设
              <select
                value={preset}
                disabled={!!operation}
                onChange={(event) => {
                  const item =
                    presets.find((entry) => entry.id === event.target.value) ??
                    presets[0];
                  setPreset(item.id);
                  if (item.id !== "custom")
                    setDraft((current) => ({
                      ...current,
                      name: item.name,
                      baseUrl: item.baseUrl,
                    }));
                }}
              >
                {presets.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.name}
                  </option>
                ))}
              </select>
            </label>
            <label>
              名称
              <input
                required
                maxLength={100}
                value={draft.name}
                disabled={!!operation}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    name: event.target.value,
                  }))
                }
                placeholder="例如：我的本地模型"
              />
            </label>
            <label>
              API 基础地址
              <input
                required
                type="url"
                value={draft.baseUrl}
                disabled={!!operation}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    baseUrl: event.target.value,
                  }))
                }
                placeholder="https://api.example.com/v1"
              />
            </label>
            <label>
              模型 ID（可稍后选择）
              <input
                maxLength={200}
                value={draft.modelId}
                disabled={!!operation}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    modelId: event.target.value,
                  }))
                }
                placeholder="从列表选择，或手动输入"
              />
            </label>
            <label>
              API Key
              <input
                ref={keyInput}
                type="password"
                autoComplete="new-password"
                value={draft.apiKey}
                disabled={!!operation}
                onChange={(event) =>
                  setDraft((current) => ({
                    ...current,
                    apiKey: event.target.value,
                  }))
                }
                placeholder={
                  original?.hasKey ? "留空表示保留已保存密钥" : "按服务要求填写"
                }
              />
            </label>
            {original?.hasKey && (
              <p className="muted">
                已保存密钥不会回显；留空表示保留。需要移除时请使用卡片上的“清除密钥”。
              </p>
            )}
            <p className="muted">
              保存只保存配置，不会调用模型或产生测试费用。模型列表和短回复测试需分别执行。
            </p>
            <div className="models-actions">
              <button
                className="button primary"
                type="submit"
                disabled={!!operation}
              >
                保存供应商
              </button>
              <button
                className="button"
                type="button"
                disabled={!!operation}
                onClick={() => {
                  setFormOpen(false);
                  setDraft(empty);
                  setEditingId(null);
                }}
              >
                取消
              </button>
            </div>
          </form>
        </div>
      )}
      {providers.some(
        (provider) =>
          provider.isDefault && provider.enabled && tested(provider),
      ) && (
        <a className="button primary provider-start" href="#/companion">
          开始对话
        </a>
      )}
    </div>
  );
}
