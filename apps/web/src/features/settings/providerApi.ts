import { call, WebError } from "./api";
import type { Provider } from "./ProviderManager";

type WireProvider = {
  provider_id: string;
  name: string;
  protocol: "openai-chat-completions";
  base_url: string;
  model_id: string;
  enabled: boolean;
  revision: number;
  has_key: boolean;
  test: { revision: number; outcome: string; tested_at: number } | null;
};

type WireView = {
  providers: WireProvider[];
  default: {
    provider_id: string | null;
    revision: number;
    provider_revision: number | null;
    configured: boolean;
  };
};

function validProvider(item: WireProvider): boolean {
  return (
    !!item &&
    typeof item.provider_id === "string" &&
    typeof item.name === "string" &&
    item.protocol === "openai-chat-completions" &&
    typeof item.base_url === "string" &&
    typeof item.model_id === "string" &&
    typeof item.enabled === "boolean" &&
    Number.isInteger(item.revision) &&
    item.revision > 0 &&
    typeof item.has_key === "boolean" &&
    (item.test === null ||
      (!!item.test &&
        Number.isInteger(item.test.revision) &&
        typeof item.test.outcome === "string" &&
        Number.isFinite(item.test.tested_at) &&
        !Number.isNaN(new Date(item.test.tested_at * 1000).getTime())))
  );
}

export function mapProviderView(wire: WireView): {
  providers: Provider[];
  defaultRevision: number;
} {
  if (
    !wire ||
    !Array.isArray(wire.providers) ||
    !wire.default ||
    !(
      wire.default.provider_id === null ||
      typeof wire.default.provider_id === "string"
    ) ||
    !(
      wire.default.provider_revision === null ||
      Number.isInteger(wire.default.provider_revision)
    ) ||
    typeof wire.default.configured !== "boolean" ||
    !Number.isInteger(wire.default.revision) ||
    wire.default.revision < 0 ||
    !wire.providers.every(validProvider)
  )
    throw new WebError("dependency_unavailable", 502);
  return {
    defaultRevision: wire.default.revision,
    providers: wire.providers.map((item) => ({
      providerId: item.provider_id,
      name: item.name,
      protocol: item.protocol,
      baseUrl: item.base_url,
      modelId: item.model_id,
      enabled: item.enabled,
      revision: item.revision,
      hasKey: item.has_key,
      isDefault:
        wire.default.configured &&
        item.provider_id === wire.default.provider_id &&
        item.revision === wire.default.provider_revision,
      test: item.test
        ? {
            state:
              item.test.outcome === "succeeded"
                ? ("passed" as const)
                : [
                      "unknown",
                      "cancelled",
                      "timed_out",
                      "connection_failed",
                    ].includes(item.test.outcome)
                  ? ("unknown" as const)
                  : ("failed" as const),
            code: item.test.outcome === "succeeded" ? null : item.test.outcome,
            revision: item.test.revision,
            at: new Date(item.test.tested_at * 1000).toISOString(),
          }
        : { state: "untested" as const, code: null, revision: null, at: null },
    })),
  };
}

export async function readProviders(csrf: string, signal: AbortSignal) {
  const result = await call<WireView>("providers/view", {}, csrf, signal);
  return mapProviderView(result);
}
