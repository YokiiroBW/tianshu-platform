import { requestId } from "../../app/requestId";
import { useEffect, useState } from "react";
import type { Binding, Instance } from "./types";
import { SchemaFields } from "./SchemaFields";

export function BindingsPanel({
  instance,
  busy,
  save,
}: {
  instance: Instance;
  busy: boolean;
  save: (bindings: Binding[]) => void;
}) {
  const [bindings, setBindings] = useState(instance.bindings);
  useEffect(
    () => setBindings(instance.bindings),
    [instance.id, instance.revision],
  );
  const overrideSchema = {
    ...instance.config_schema,
    required: [],
    properties: Object.fromEntries(
      Object.entries(instance.config_schema.properties ?? {}).filter(([key]) =>
        instance.binding_overrides.includes(key),
      ),
    ),
  };
  function update(index: number, value: Partial<Binding>) {
    setBindings((current) =>
      current.map((binding, at) =>
        at === index ? { ...binding, ...value } : binding,
      ),
    );
  }
  return (
    <form
      className="plugins-config"
      onSubmit={(event) => {
        event.preventDefault();
        save(bindings);
      }}
    >
      <p className="plugins-note">
        网页功能可以直接使用；需要给不同工作区设置独立参数时，在这里增加范围。机器人和角色范围将在对应提供者接入后开放。
      </p>
      {bindings.map((binding, index) => (
        <fieldset className="plugins-fields" key={binding.binding_id}>
          <legend>
            {binding.scope.kind === "workspace" ? "网页工作区" : "已有绑定"}
          </legend>
          <label className="plugins-check">
            <input
              type="checkbox"
              checked={binding.enabled}
              onChange={(event) =>
                update(index, { enabled: event.target.checked })
              }
            />
            启用此范围
          </label>
          {binding.scope.kind === "workspace" ? (
            <label className="plugins-field">
              工作区名称
              <input
                required
                value={binding.scope.workspace_id ?? ""}
                onChange={(event) =>
                  update(index, {
                    scope: {
                      kind: "workspace",
                      workspace_id: event.target.value,
                    },
                  })
                }
              />
            </label>
          ) : (
            <p>{JSON.stringify(binding.scope)}</p>
          )}
          <label className="plugins-field">
            优先级
            <input
              type="number"
              step={1}
              value={binding.priority}
              onChange={(event) =>
                update(index, { priority: Number(event.target.value) })
              }
            />
          </label>
          {instance.binding_overrides.length > 0 && (
            <fieldset className="plugins-fields">
              <legend>此范围的配置</legend>
              <SchemaFields
                schema={overrideSchema}
                value={binding.overrides}
                onChange={(overrides) => update(index, { overrides })}
              />
            </fieldset>
          )}
          <button
            type="button"
            onClick={() =>
              setBindings((current) => current.filter((_, at) => at !== index))
            }
          >
            移除此范围
          </button>
        </fieldset>
      ))}
      <button
        type="button"
        onClick={() =>
          setBindings((current) => [
            ...current,
            {
              kind: "binding",
              schema_version: 1,
              binding_id: requestId(),
              instance_id: instance.id,
              revision: 1,
              enabled: true,
              scope: { kind: "workspace", workspace_id: "main" },
              overrides: {},
              priority: 0,
            },
          ])
        }
      >
        添加网页工作区
      </button>
      <button className="button primary" disabled={busy}>
        保存范围
      </button>
    </form>
  );
}
