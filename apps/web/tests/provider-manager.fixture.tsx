import { useState } from "react";
import { createRoot } from "react-dom/client";
import {
  ProviderManager,
  type Provider,
  type ProviderActions,
} from "../src/features/settings/ProviderManager";
import "../src/design/tokens.css";
import "../src/app/app.css";
import "../src/features/settings/models.css";
import { mapProviderView } from "../src/features/settings/providerApi";
import { openCodePreset } from "../src/features/settings/providerPresets";

declare global {
  interface Window {
    providerEvents: string[];
  }
}
window.providerEvents = [];

function Fixture() {
  const [providers, setProviders] = useState<Provider[]>(() =>
    new URLSearchParams(location.search).has("rejected")
      ? mapProviderView({
          providers: [
            {
              provider_id: "synthetic-provider",
              name: "Rejected provider",
              protocol: "openai-chat-completions",
              base_url: "https://opencode.ai/zen/go/v1",
              model_id: "deepseek-v4.1-flash",
              enabled: true,
              revision: 3,
              has_key: true,
              test: {
                revision: 3,
                outcome: "unknown",
                tested_at: 1800000000,
                error_code: "upstream_rejected",
              },
            },
          ],
          default: {
            provider_id: null,
            revision: 0,
            provider_revision: null,
            configured: false,
          },
        }).providers
      : [],
  );
  const [error, setError] = useState("");
  const [defaultRevision, setDefaultRevision] = useState(0);
  const update = (id: string, change: (provider: Provider) => Provider) =>
    setProviders((items) =>
      items.map((item) => (item.providerId === id ? change(item) : item)),
    );
  const actions: ProviderActions = {
    async save(draft, original) {
      window.providerEvents.push("save");
      const provider: Provider = {
        providerId: original?.providerId ?? "synthetic-provider",
        name: draft.name,
        baseUrl: draft.baseUrl,
        protocol: draft.protocol,
        modelId: draft.modelId,
        enabled: true,
        revision: (original?.revision ?? 0) + 1,
        hasKey: Boolean(draft.apiKey || original?.hasKey),
        isDefault: false,
        test: { state: "untested", code: null, revision: null, at: null },
      };
      setProviders((items) =>
        original
          ? items.map((item) =>
              item.providerId === original.providerId ? provider : item,
            )
          : [...items, provider],
      );
    },
    async listModels(provider) {
      window.providerEvents.push(`models:${provider.revision}`);
      return openCodePreset(provider.baseUrl)
        ? ["deepseek-v4.1-flash", "gpt-6-luna", "minimax-m3"]
        : ["synthetic-small", "synthetic-large"];
    },
    async test(provider) {
      window.providerEvents.push(`test:${provider.revision}`);
      update(provider.providerId, (item) => ({
        ...item,
        test: {
          state: "passed",
          code: null,
          revision: provider.revision,
          at: "2026-09-26T00:00:00Z",
        },
      }));
    },
    async setDefault(provider) {
      window.providerEvents.push(`default:${provider.revision}`);
      setDefaultRevision((value) => value + 1);
      update(provider.providerId, (item) => ({ ...item, isDefault: true }));
    },
    async setEnabled(provider, enabled) {
      window.providerEvents.push(`enabled:${enabled}`);
      update(provider.providerId, (item) => ({
        ...item,
        enabled,
        isDefault: enabled ? item.isDefault : false,
      }));
    },
    async clearKey(provider) {
      window.providerEvents.push("clear-key");
      update(provider.providerId, (item) => ({
        ...item,
        hasKey: false,
        revision: item.revision + 1,
        test: { state: "untested", code: null, revision: null, at: null },
      }));
    },
    async remove(provider) {
      window.providerEvents.push("delete");
      setProviders((items) =>
        items.filter((item) => item.providerId !== provider.providerId),
      );
    },
  };
  return (
    <main style={{ maxWidth: "64rem", margin: "2rem auto", padding: "0 1rem" }}>
      <section className="panel models">
        <h1>模型配置 · 合成组件夹具</h1>
        <h2>供应商设置</h2>
        <p>此页只用于组件测试，不接后端。</p>
        <p className="sr-only">默认修订 {defaultRevision}</p>
        {error && <p role="alert">{error}</p>}
        <ProviderManager
          providers={providers}
          editable
          actions={actions}
          onError={() => setError("合成操作失败")}
        />
      </section>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<Fixture />);
