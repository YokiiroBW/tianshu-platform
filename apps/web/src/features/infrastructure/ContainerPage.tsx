import { useEffect, useRef, useState } from "react";
import { Boxes, RefreshCw } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import { useInfrastructureAction, useInfrastructureQuery } from "./api";
import { ContainerResourceMeter } from "./ContainerResourceMeter";
import {
  bytes,
  containerTone,
  explanation,
  Freshness,
  health,
  NodeRail,
  rate,
  states,
  stageLabel,
  time,
  UnconfiguredInfrastructure,
} from "./presentation";
import type {
  ContainerDetail,
  ContainerList,
  InfrastructureView,
  LogView,
} from "./types";

function Logs({ node, container }: { node: string; container: string }) {
  const query = useInfrastructureQuery<LogView>("logs", {
    node_id: node,
    container_id: container,
  });
  const action = useInfrastructureAction();
  const logs = query.data;
  const pending = logs && ["queued", "in_progress"].includes(logs.state);
  return (
    <section className="infra-section">
      <div className="infra-heading">
        <h3>最近日志</h3>
        <button
          className="button"
          disabled={action.pending || Boolean(pending)}
          onClick={async () => {
            if (
              await action.send("logs/refresh", {
                node_id: node,
                container_id: container,
              })
            )
              await query.reload();
          }}
        >
          读取最近日志
        </button>
      </div>
      <p className="infra-muted">
        最近 1 小时 · 最多 200 行 / 64
        KiB。此处是预览，完整日志以已登记的日志服务为准。
      </p>
      {(query.error || action.error) && (
        <p role="alert" className="infra-error">
          {action.error || query.error}
        </p>
      )}
      {pending && <p role="status">日志读取中</p>}
      {logs?.state === "failed" && (
        <p role="alert">{explanation(logs.reason_codes)}</p>
      )}
      {logs?.data && (
        <>
          <p>
            读取时间 · {time(logs.observed_at)}
            {logs.data.truncated && (
              <strong className="infra-warning"> · 已截断预览</strong>
            )}
          </p>
          <pre className="infra-logs" tabIndex={0} aria-label="容器日志预览">
            {logs.data.stdout || logs.data.stderr
              ? `${logs.data.stdout}${logs.data.stderr ? `\n[stderr]\n${logs.data.stderr}` : ""}`
              : "本次时间范围内没有日志行。"}
          </pre>
        </>
      )}
      {logs?.state === "not_requested" && (
        <p className="infra-muted">点击读取此容器的限定日志。</p>
      )}
    </section>
  );
}

function Detail({
  nodeId,
  containerId,
}: {
  nodeId: string;
  containerId: string;
}) {
  const query = useInfrastructureQuery<ContainerDetail>("container", {
    node_id: nodeId,
    container_id: containerId,
  });
  const result = query.data;
  if (query.loading && !result)
    return (
      <section className="panel infra-pane infra-container-detail infra-detail-state">
        <StatePanel kind="loading" title="正在读取容器详情">
          <p>正在读取这个容器的后台观测。</p>
        </StatePanel>
      </section>
    );
  if (!result)
    return (
      <section className="panel infra-pane infra-container-detail infra-detail-state">
        <StatePanel kind="error" title="容器详情无法读取">
          <p>{query.error}</p>
          <p>容器可能已重建或移除，请从最新清单重新选择。</p>
        </StatePanel>
      </section>
    );
  const { container, stats } = result;
  const readableStats = stats?.availability === "ok" ? stats : null;
  const current = result.inventory.freshness === "current";
  return (
    <aside
      className="panel infra-pane infra-container-detail"
      aria-label="容器详情"
    >
      <header className="infra-detail-heading">
        <div className="infra-heading">
          <div>
            <p className="infra-eyebrow">容器详情</p>
            <h3 title={container.name ?? container.id}>
              {container.name ?? container.id.slice(0, 12)}
            </h3>
          </div>
          <Boxes size={22} aria-hidden="true" />
        </div>
        <StatusRail
          tone={containerTone(container, current)}
          label={`${current ? "" : "上次状态 · "}${states[container.state] ?? "未知"}`}
        />
        <p
          className={`infra-health tone-${current && container.health === "unhealthy" ? "red" : "gray"}`}
        >
          健康检查 · {health[container.health]}
        </p>
      </header>
      <div
        className="infra-detail-scroll"
        tabIndex={0}
        role="group"
        aria-label="容器详情内容"
      >
        {query.error && (
          <p role="alert" className="infra-error">
            {query.error}
          </p>
        )}
        <Freshness observation={result.inventory} />
        <dl className="infra-detail-list">
          <div>
            <dt>名称</dt>
            <dd>{container.name ?? "—"}</dd>
          </div>
          <div>
            <dt>节点 / 应用</dt>
            <dd>
              {result.node.label} / {container.compose?.project ?? "独立容器"}
            </dd>
          </div>
          <div>
            <dt>服务</dt>
            <dd>{container.compose?.service ?? "—"}</dd>
          </div>
          <div>
            <dt>镜像</dt>
            <dd className="infra-mono">{container.image_reference ?? "—"}</dd>
          </div>
          <div>
            <dt>容器身份</dt>
            <dd className="infra-mono">{container.id}</dd>
          </div>
          <div>
            <dt>启动时间</dt>
            <dd>{time(container.started_at)}</dd>
          </div>
          <div>
            <dt>网络模式</dt>
            <dd>{container.network_mode ?? "未知"}</dd>
          </div>
        </dl>
        <section className="infra-section">
          <h3>资源读数</h3>
          <Freshness observation={result.metrics} />
          <div className="infra-resource-readings">
            <ContainerResourceMeter kind="cpu" stats={stats} detailed />
            <ContainerResourceMeter kind="memory" stats={stats} detailed />
          </div>
          <dl className="infra-metrics infra-small-metrics">
            <div>
              <dt>网络接收 / 发送</dt>
              <dd>
                {rate(readableStats?.rates?.receive_bytes_per_second)} /{" "}
                {rate(readableStats?.rates?.transmit_bytes_per_second)}
              </dd>
              <dd className="infra-metric-note">
                <small>
                  {readableStats?.network?.attribution === "not_attributable"
                    ? "共享网络命名空间，不能归属单个容器"
                    : `累计 ${bytes(readableStats?.network?.receive_bytes)} / ${bytes(readableStats?.network?.transmit_bytes)}`}
                </small>
              </dd>
            </div>
            <div>
              <dt>磁盘读取 / 写入</dt>
              <dd>
                {rate(readableStats?.rates?.read_bytes_per_second)} /{" "}
                {rate(readableStats?.rates?.write_bytes_per_second)}
              </dd>
              <dd className="infra-metric-note">
                <small>
                  累计 {bytes(readableStats?.block_io?.read_bytes)} /{" "}
                  {bytes(readableStats?.block_io?.write_bytes)}
                </small>
              </dd>
            </div>
          </dl>
          {readableStats?.rates?.state &&
            readableStats.rates.state !== "ok" && (
              <p className="infra-muted">
                {explanation([readableStats.rates.state])}
              </p>
            )}
        </section>
        <section className="infra-section">
          <h3>端口</h3>
          {container.ports.length ? (
            <ul className="infra-compact-list">
              {container.ports.map((port, index) => (
                <li key={index} className="infra-mono">
                  {port.host_port == null
                    ? "未发布"
                    : `${port.host_ip ?? "所有地址"}:${port.host_port}`}{" "}
                  → {port.container_port}/{port.protocol}
                </li>
              ))}
            </ul>
          ) : (
            <p className="infra-muted">本次观测未列出发布端口。</p>
          )}
        </section>
        <section className="infra-section">
          <h3>挂载与持久数据</h3>
          {container.mounts.length ? (
            <ul className="infra-compact-list">
              {container.mounts.map((mount, index) => (
                <li key={index}>
                  <strong>
                    {mount.kind} ·{" "}
                    {mount.read_only == null
                      ? "读写权限未知"
                      : mount.read_only
                        ? "只读"
                        : "可写"}
                  </strong>
                  <span className="infra-mono">
                    {mount.source ?? "来源未知"} → {mount.target}
                  </span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="infra-muted">本次观测未列出挂载。</p>
          )}
        </section>
        <Logs
          key={`${nodeId}/${containerId}`}
          node={nodeId}
          container={containerId}
        />
      </div>
    </aside>
  );
}

function Workspace({ view }: { view: InfrastructureView }) {
  const preferred =
    view.nodes.find(
      (node) =>
        node.enabled &&
        (node.capabilities.includes("docker.inventory") ||
          view.catalog?.data?.nodes.some(
            (source) =>
              source.id === node.id &&
              source.capabilities.includes("docker.inventory"),
          )),
    )?.id ??
    view.nodes[0]?.id ??
    "";
  const [nodeId, setNodeId] = useState(preferred);
  const [application, setApplication] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [state, setState] = useState("");
  const [healthFilter, setHealthFilter] = useState("");
  const [page, setPage] = useState(0);
  const [selected, setSelected] = useState("");
  const listScroll = useRef<HTMLDivElement>(null);
  const query = useInfrastructureQuery<ContainerList>(
    "containers",
    {
      node_id: nodeId,
      query: search,
      application,
      state: state || null,
      health: healthFilter || null,
      page,
    },
    Boolean(nodeId),
  );
  const action = useInfrastructureAction();
  const result = query.data;
  useEffect(() => {
    if (!view.nodes.some((node) => node.id === nodeId)) setNodeId(preferred);
  }, [nodeId, preferred, view.nodes]);
  useEffect(() => {
    setSelected("");
    setPage(0);
    setApplication(null);
  }, [nodeId]);
  useEffect(() => {
    if (
      result &&
      !result.containers.some((container) => container.id === selected)
    )
      setSelected(result.containers[0]?.id ?? "");
  }, [result, selected]);
  useEffect(() => {
    listScroll.current?.scrollTo({ top: 0 });
  }, [nodeId, application, search, state, healthFilter, page]);
  const current = result?.inventory.freshness === "current";
  if (!view.nodes.length)
    return (
      <section className="panel infra-empty">
        <h2>先添加运行节点</h2>
        <p>从部署端目录选择 NAS 或 Linux 节点，后台会持续读取容器。</p>
        <a className="button" href="#/settings/11">
          添加节点
        </a>
      </section>
    );
  return (
    <div className="infra-workspace">
      <div className="infra-heading">
        <div>
          <h2>Docker 容器</h2>
          <p>按节点与应用查看运行、健康和资源读数</p>
        </div>
        <a className="button" href="#/settings/11">
          节点连接
        </a>
      </div>
      <div className="infra-docker-layout">
        <aside className="panel infra-pane infra-selector infra-docker-selector">
          <h3>运行节点</h3>
          <div
            className="infra-selector-nodes"
            aria-label="运行节点"
            role="group"
            tabIndex={0}
          >
            {view.nodes.map((node) => (
              <button
                className="infra-node-choice"
                key={node.id}
                aria-pressed={nodeId === node.id}
                onClick={() => setNodeId(node.id)}
              >
                <strong title={node.label}>{node.label}</strong>
                <NodeRail node={node} />
              </button>
            ))}
          </div>
          <h3>Compose 应用</h3>
          <div
            className="infra-selector-apps"
            aria-label="Compose 应用"
            role="group"
            tabIndex={0}
          >
            <button
              className="infra-app-choice"
              aria-pressed={application === null}
              onClick={() => {
                setApplication(null);
                setPage(0);
              }}
            >
              全部容器
            </button>
            {result?.applications.map((app) => (
              <button
                key={app.name}
                className="infra-app-choice"
                aria-pressed={application === app.name}
                onClick={() => {
                  setApplication(app.name);
                  setPage(0);
                }}
              >
                <span title={app.name || "独立容器"}>
                  {app.name || "独立容器"}
                </span>
                <small>{app.containers} 个容器</small>
              </button>
            ))}
          </div>
          <a href="#/home/2">查看节点与网络</a>
        </aside>
        <section
          className="panel infra-pane infra-container-list"
          aria-label="容器列表"
        >
          <div className="infra-heading">
            <h3>
              容器清单{" "}
              {result ? (
                <span className="infra-muted">{result.total} 个</span>
              ) : (
                ""
              )}
            </h3>
            <button
              className="button"
              disabled={
                action.pending ||
                !nodeId ||
                !view.nodes.find((node) => node.id === nodeId)?.enabled
              }
              onClick={async () => {
                if (await action.send("refresh", { node_id: nodeId }))
                  await query.reload();
              }}
              aria-label="刷新容器观测"
            >
              <RefreshCw size={16} aria-hidden="true" />
            </button>
          </div>
          <div className="infra-filters">
            <label>
              搜索容器
              <input
                value={search}
                placeholder="名称、镜像或容器 ID"
                onChange={(event) => {
                  setSearch(event.target.value);
                  setPage(0);
                }}
                maxLength={128}
              />
            </label>
            <label>
              运行状态
              <select
                value={state}
                onChange={(event) => {
                  setState(event.target.value);
                  setPage(0);
                }}
              >
                <option value="">全部运行状态</option>
                {Object.entries(states).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
            <label>
              健康检查
              <select
                value={healthFilter}
                onChange={(event) => {
                  setHealthFilter(event.target.value);
                  setPage(0);
                }}
              >
                <option value="">全部健康状态</option>
                {Object.entries(health).map(([value, label]) => (
                  <option key={value} value={value}>
                    {label}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div
            className="infra-container-scroll"
            ref={listScroll}
            tabIndex={0}
            role="group"
            aria-label="容器清单内容"
          >
            {(action.error || query.error) && (
              <p className="infra-error" role="alert">
                {action.error || query.error}
              </p>
            )}
            {query.loading && !result && <p role="status">正在读取容器清单…</p>}
            {result && (
              <>
                <Freshness observation={result.inventory} />
                {result.metrics.freshness !== "current" &&
                  result.metrics.observed_at && (
                    <p className="infra-muted">
                      资源为上次读数 · {time(result.metrics.observed_at)}
                    </p>
                  )}
                {result.node.refresh &&
                  ["queued", "in_progress"].includes(
                    result.node.refresh.state,
                  ) && (
                    <p role="status">
                      后台采集中 · {stageLabel(result.node.refresh.stage)}
                    </p>
                  )}
                <div className="infra-container-rows">
                  {result.containers.map((container) => (
                    <button
                      className="infra-container-row"
                      key={`${nodeId}/${container.id}`}
                      aria-pressed={selected === container.id}
                      aria-label={`查看容器 ${container.name ?? container.id}`}
                      onClick={() => setSelected(container.id)}
                    >
                      <div className="infra-row-identity">
                        <strong title={container.name ?? container.id}>
                          {container.name ?? container.id.slice(0, 12)}
                        </strong>
                        <div className="infra-row-state">
                          <StatusRail
                            tone={containerTone(container, Boolean(current))}
                            label={`${current ? "" : "上次 · "}${states[container.state]}`}
                            selected={selected === container.id}
                          />
                          <span
                            className={`infra-health tone-${current && container.health === "unhealthy" ? "red" : "gray"}`}
                          >
                            {health[container.health]}
                          </span>
                        </div>
                        <small title={container.image_reference ?? "镜像未知"}>
                          {container.image_reference ?? "镜像未知"}
                        </small>
                      </div>
                      <div className="infra-row-resources">
                        <ContainerResourceMeter
                          kind="cpu"
                          stats={container.stats}
                        />
                        <ContainerResourceMeter
                          kind="memory"
                          stats={container.stats}
                        />
                      </div>
                    </button>
                  ))}
                </div>
                {!result.containers.length && (
                  <div className="infra-empty">
                    <h4>
                      {result.inventory.freshness === "missing"
                        ? "等待容器观测"
                        : "当前筛选没有容器"}
                    </h4>
                    <p>
                      {result.inventory.freshness === "missing"
                        ? explanation(result.inventory.reason_codes)
                        : "可以调整节点、应用或状态筛选。"}
                    </p>
                  </div>
                )}
              </>
            )}
          </div>
          {result && result.total > result.page_size && (
            <nav className="infra-pagination" aria-label="容器分页">
              <button
                className="button"
                disabled={page === 0}
                onClick={() => setPage((value) => value - 1)}
              >
                上一页
              </button>
              <span>
                第 {page + 1} 页 · 共 {result.total} 个容器
              </span>
              <button
                className="button"
                disabled={!result.has_more}
                onClick={() => setPage((value) => value + 1)}
              >
                下一页
              </button>
            </nav>
          )}
        </section>
        {selected ? (
          <Detail
            key={`${nodeId}/${selected}`}
            nodeId={nodeId}
            containerId={selected}
          />
        ) : (
          <aside className="panel infra-pane infra-container-detail infra-empty">
            <h3>容器详情</h3>
            <p>选择容器查看端口、挂载与限定日志。</p>
          </aside>
        )}
      </div>
    </div>
  );
}

export default function ContainerPage() {
  const query = useInfrastructureQuery<InfrastructureView>("view", {});
  if (query.loading && !query.data)
    return (
      <StatePanel kind="loading" title="正在读取运行节点">
        <p>正在读取已添加的运行节点。</p>
      </StatePanel>
    );
  if (!query.data)
    return (
      <StatePanel kind="error" title="运行节点无法读取">
        <p>{query.error}</p>
      </StatePanel>
    );
  if (!query.data.configured) return <UnconfiguredInfrastructure />;
  return (
    <>
      {query.error && (
        <p className="infra-error" role="alert">
          {query.error}
        </p>
      )}
      <Workspace view={query.data} />
    </>
  );
}
