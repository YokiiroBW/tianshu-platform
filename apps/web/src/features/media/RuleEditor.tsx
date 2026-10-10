import { Plus, Trash2 } from "lucide-react";
import type { Group, Rule, Rules } from "./types";
import { requestId } from "../../app/requestId";

const fields: { value: Rule["field"]; label: string }[] = [
  { value: "title", label: "标题" },
  { value: "description", label: "简介" },
  { value: "uploader_id", label: "UP 主 ID" },
  { value: "uploader_name", label: "UP 主昵称" },
  { value: "tags", label: "标签" },
];
const operations: { value: Rule["op"]; label: string }[] = [
  { value: "contains", label: "包含" },
  { value: "equals", label: "等于" },
  { value: "prefix", label: "开头为" },
  { value: "suffix", label: "结尾为" },
  { value: "regex", label: "正则表达式" },
];
const id = () => `r_${requestId().replaceAll("-", "")}`;
const newRule = (): Rule => ({
  id: id(),
  field: "title",
  op: "contains",
  value: "",
  case_sensitive: false,
});

export function RuleEditor({
  value,
  onChange,
  disabled = false,
}: {
  value: Rules;
  onChange: (rules: Rules) => void;
  disabled?: boolean;
}) {
  const set = (kind: "whitelist" | "blacklist", groups: Group[]) =>
    onChange({ ...value, [kind]: groups });
  const change = (
    kind: "whitelist" | "blacklist",
    index: number,
    rules: Rule[],
  ) =>
    set(
      kind,
      value[kind].map((group, position) =>
        position === index ? { ...group, rules } : group,
      ),
    );
  return (
    <div className="media-rules">
      <p className="muted">
        黑名单优先；同一组内全部满足（AND），任一组满足即可（OR）。白名单为空时不设门槛。
      </p>
      {(["blacklist", "whitelist"] as const).map((kind) => (
        <section
          key={kind}
          className="media-rule-list"
          aria-label={kind === "blacklist" ? "黑名单" : "白名单"}
        >
          <div className="media-section-head">
            <h4>
              {kind === "blacklist"
                ? "黑名单 · 命中即跳过"
                : "白名单 · 命中才下载"}
            </h4>
            <button
              type="button"
              className="button"
              disabled={
                disabled ||
                value.blacklist.length + value.whitelist.length >= 20
              }
              onClick={() =>
                set(kind, [...value[kind], { id: id(), rules: [newRule()] }])
              }
            >
              <Plus aria-hidden="true" />
              添加{kind === "blacklist" ? "黑名单" : "白名单"}组
            </button>
          </div>
          {value[kind].length === 0 ? (
            <p className="muted">
              {kind === "blacklist" ? "没有黑名单规则。" : "没有白名单限制。"}
            </p>
          ) : (
            value[kind].map((group, index) => (
              <fieldset
                className="media-rule-group"
                key={group.id}
                disabled={disabled}
              >
                <legend>
                  第 {index + 1} 组{index > 0 ? "（与上一组为 OR）" : ""}
                </legend>
                {group.rules.map((rule, position) => (
                  <div className="media-rule-row" key={rule.id}>
                    <label>
                      字段
                      <select
                        value={rule.field}
                        onChange={(event) =>
                          change(
                            kind,
                            index,
                            group.rules.map((item) =>
                              item.id === rule.id
                                ? {
                                    ...item,
                                    field: event.target.value as Rule["field"],
                                  }
                                : item,
                            ),
                          )
                        }
                      >
                        {fields.map((field) => (
                          <option key={field.value} value={field.value}>
                            {field.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      条件
                      <select
                        value={rule.op}
                        onChange={(event) =>
                          change(
                            kind,
                            index,
                            group.rules.map((item) =>
                              item.id === rule.id
                                ? {
                                    ...item,
                                    op: event.target.value as Rule["op"],
                                  }
                                : item,
                            ),
                          )
                        }
                      >
                        {operations.map((op) => (
                          <option key={op.value} value={op.value}>
                            {op.label}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="media-rule-value">
                      {position > 0 ? "并且匹配值" : "匹配值"}
                      <input
                        value={rule.value}
                        aria-label={position > 0 ? "并且匹配值" : "匹配值"}
                        aria-invalid={
                          Array.from(rule.value).length >
                            (rule.op === "regex" ? 512 : 4096) || undefined
                        }
                        required
                        onChange={(event) =>
                          change(
                            kind,
                            index,
                            group.rules.map((item) =>
                              item.id === rule.id
                                ? { ...item, value: event.target.value }
                                : item,
                            ),
                          )
                        }
                      />
                      <small className="muted">
                        {Array.from(rule.value).length} /{" "}
                        {rule.op === "regex" ? 512 : 4096} 字符
                      </small>
                    </label>
                    <label className="media-check">
                      <input
                        type="checkbox"
                        checked={rule.case_sensitive}
                        onChange={(event) =>
                          change(
                            kind,
                            index,
                            group.rules.map((item) =>
                              item.id === rule.id
                                ? {
                                    ...item,
                                    case_sensitive: event.target.checked,
                                  }
                                : item,
                            ),
                          )
                        }
                      />
                      区分大小写
                    </label>
                    <button
                      type="button"
                      className="icon-button"
                      aria-label={`删除${kind === "blacklist" ? "黑名单" : "白名单"}第${index + 1}组第${position + 1}条规则`}
                      disabled={group.rules.length === 1}
                      onClick={() =>
                        change(
                          kind,
                          index,
                          group.rules.filter((item) => item.id !== rule.id),
                        )
                      }
                    >
                      <Trash2 aria-hidden="true" />
                    </button>
                  </div>
                ))}
                <div className="media-actions">
                  <button
                    type="button"
                    className="button"
                    disabled={group.rules.length >= 10}
                    onClick={() =>
                      change(kind, index, [...group.rules, newRule()])
                    }
                  >
                    添加 AND 条件
                  </button>
                  <button
                    type="button"
                    className="button"
                    onClick={() =>
                      set(
                        kind,
                        value[kind].filter((item) => item.id !== group.id),
                      )
                    }
                  >
                    删除此组
                  </button>
                </div>
              </fieldset>
            ))
          )}
        </section>
      ))}
    </div>
  );
}
