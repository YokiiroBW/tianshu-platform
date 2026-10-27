import { useState } from "react";

export type GroupMode = "disabled" | "observe" | "reply" | "active";
export type GroupRule = {
  mode: GroupMode;
  actor_id: string | null;
  keywords: string[];
};
export type GroupPolicy = {
  version: 2;
  default: GroupRule;
  groups: Record<string, GroupRule>;
};
const LABELS: Record<GroupMode, string> = {
  disabled: "停用",
  observe: "仅观察",
  reply: "启用回复",
  active: "主动",
};

export default function GroupPolicyEditor({
  value,
  actors,
  disabled,
  onChange,
}: {
  value: GroupPolicy;
  actors: { id: string; label: string }[];
  disabled: boolean;
  onChange: (policy: GroupPolicy) => void;
}) {
  const [groupId, setGroupId] = useState("");
  function editor(id: string | null, rule: GroupRule) {
    const name = id === null ? "未单独配置的群" : `群 ${id}`;
    function update(changes: Partial<GroupRule>) {
      const next = { ...rule, ...changes };
      onChange(
        id === null
          ? { ...value, default: next }
          : { ...value, groups: { ...value.groups, [id]: next } },
      );
    }
    return (
      <fieldset key={id ?? "default"} className="bot-form" disabled={disabled}>
        <legend>{name}</legend>
        <label>
          状态
          <select
            aria-label={`${name}状态`}
            value={rule.mode}
            onChange={(e) => update({ mode: e.target.value as GroupMode })}
          >
            {Object.entries(LABELS).map(([mode, label]) => (
              <option key={mode} value={mode}>
                {label}
              </option>
            ))}
          </select>
        </label>
        <label>
          群角色
          <select
            aria-label={`${name}角色`}
            value={rule.actor_id ?? ""}
            onChange={(e) => update({ actor_id: e.target.value || null })}
          >
            <option value="">未绑定，仅归档</option>
            {actors.map((actor) => (
              <option key={actor.id} value={actor.id}>
                {actor.label}
              </option>
            ))}
          </select>
        </label>
        <label>
          回复关键词
          <textarea
            aria-label={`${name}关键词`}
            rows={2}
            value={rule.keywords.join("\n")}
            onChange={(e) =>
              update({
                keywords: [
                  ...new Set(
                    e.target.value.split(/\r?\n/).filter((word) => word.trim()),
                  ),
                ],
              })
            }
            placeholder="每行一个原文关键词，不使用正则"
          />
        </label>
        {id !== null && rule.mode !== "disabled" && !rule.actor_id && (
          <p role="alert">保存此群前请选择真实角色。</p>
        )}
        {id !== null && (
          <button
            type="button"
            className="button"
            onClick={() => {
              const groups = { ...value.groups };
              delete groups[id];
              onChange({ ...value, groups });
            }}
          >
            恢复默认状态
          </button>
        )}
      </fieldset>
    );
  }
  return (
    <fieldset className="bot-observation-policy" disabled={disabled}>
      <legend>逐群状态</legend>
      <p className="muted">
        停用：不采集、不回复、不主动，也暂停待整理材料。仅观察：整理话题、画像和记忆。启用回复：在观察上由真实
        @
        或关键词触发。主动：再按角色兴趣判断是否参与。历史档案仍按原读取授权保留；重新启用不补采旧消息。
      </p>
      {editor(null, value.default)}
      {Object.entries(value.groups).map(([id, rule]) => editor(id, rule))}
      <div className="bot-form">
        <label>
          新增群号
          <input
            aria-label="新增群号"
            value={groupId}
            inputMode="numeric"
            onChange={(e) => setGroupId(e.target.value)}
          />
        </label>
        <button
          type="button"
          className="button"
          disabled={
            !/^[1-9][0-9]*$/.test(groupId) ||
            Object.keys(value.groups).length >= 256
          }
          onClick={() => {
            onChange({
              ...value,
              groups: {
                ...value.groups,
                [groupId]: {
                  ...value.default,
                  keywords: [...value.default.keywords],
                },
              },
            });
            setGroupId("");
          }}
        >
          配置此群
        </button>
      </div>
    </fieldset>
  );
}
