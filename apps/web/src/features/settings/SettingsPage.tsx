import { useState } from "react";
import { Search } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import { ModelsPanel } from "./ModelsPanel";

const prerequisites = [
  {
    name: "陪伴与生活",
    detail: "角色、文字对话、小屋与日记。",
    requirement: "需要陪伴服务与访问授权。",
  },
  {
    name: "记忆与研究资料",
    detail: "人物、共同经历与资料检索。",
    requirement: "需要记忆服务与可见范围。",
  },
  {
    name: "资产库",
    detail: "图片、附件与归档结果。",
    requirement: "需要资产服务与受管资源范围。",
  },
  {
    name: "家庭与容器",
    detail: "设备、节点与容器观测。",
    requirement: "需要连接器与明确的测试目标。",
  },
  {
    name: "模型与用量",
    detail: "模型分工、可用能力与实际用量。",
    requirement: "需要模型网关与提供商配置。",
  },
];

export default function SettingsPage({ section }: { section: number }) {
  const [query, setQuery] = useState("");
  if (section === 2) return <ModelsPanel />;
  if (section !== 1)
    return (
      <StatePanel
        kind="unconfigured"
        title="任务来源尚未接入"
        action={
          <a className="button" href="#/settings/1">
            查看接入准备
          </a>
        }
      >
        <p>连接后将按任务责任方显示实际进展、阶段与失败原因。</p>
      </StatePanel>
    );
  const filtered = prerequisites.filter((item) =>
    `${item.name}${item.detail}`.includes(query.trim()),
  );
  return (
    <section className="panel connections">
      <div className="section-heading">
        <h2>接入准备</h2>
        <span className="badge">尚未接入服务</span>
      </div>
      <p>以下是各工作区需要的连接。配置入口将在服务接入后开放。</p>
      <label className="search-label" htmlFor="connection-search">
        筛选连接类型
      </label>
      <div className="search-field">
        <Search aria-hidden="true" />
        <input
          id="connection-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="按名称或用途查找"
        />
      </div>
      <p className="sr-only" role="status">
        找到 {filtered.length} 种连接类型
      </p>
      {filtered.length ? (
        <ul className="connection-list">
          {filtered.map((item) => (
            <li key={item.name}>
              <div>
                <h3>{item.name}</h3>
                <p>{item.detail}</p>
                <p>{item.requirement}</p>
              </div>
              <StatusRail tone="gray" label="未配置" />
            </li>
          ))}
        </ul>
      ) : (
        <StatePanel
          kind="empty"
          title="没有匹配的连接类型"
          action={
            <button className="button" onClick={() => setQuery("")}>
              清除筛选
            </button>
          }
        >
          <p>试试其他名称，或清除筛选查看全部。</p>
        </StatePanel>
      )}
    </section>
  );
}
