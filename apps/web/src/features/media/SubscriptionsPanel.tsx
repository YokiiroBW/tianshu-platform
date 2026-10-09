import { useEffect, useRef, useState, type FormEvent } from "react";
import { Plus, RefreshCw } from "lucide-react";
import { StatePanel } from "../../components/StatePanel";
import { StatusRail } from "../../components/StatusRail";
import { AccountField, QualityField, TargetField } from "./Fields";
import { RuleEditor } from "./RuleEditor";
import { RuleResults } from "./RuleResults";
import { ReasonNote } from "./ReasonNote";
import {
  bestQuality,
  emptyRules,
  type Preview,
  type RuleResult,
  type Rules,
  type Source,
  type Subscription,
} from "./types";
import type { MediaController } from "./useMediaController";
import { qualityWord, sourceWords, timestamp, tone, word } from "./wording";

type Draft = {
  subscription_id: string | null;
  expected_revision: number;
  label: string;
  source: Source;
  account_id: string;
  target_id: string;
  initial_sync: "future_only" | "history";
  quality: ReturnType<typeof bestQuality>;
  rules: Rules;
  interval_seconds: number;
};
function draft(item?: Subscription): Draft {
  return item
    ? {
        subscription_id: item.subscription_id,
        expected_revision: item.revision,
        label: item.label,
        source: { ...item.source },
        account_id: item.account_id ?? "",
        target_id: item.target_id,
        initial_sync: item.initial_sync,
        quality: { ...item.quality },
        rules: structuredClone({
          ...item.rules,
          revision: item.rules.revision + 1,
        }),
        interval_seconds: item.interval_seconds,
      }
    : {
        subscription_id: null,
        expected_revision: 0,
        label: "",
        source: { kind: "favorite", id: "" },
        account_id: "",
        target_id: "",
        initial_sync: "future_only",
        quality: bestQuality(),
        rules: emptyRules(),
        interval_seconds: 900,
      };
}

function SubscriptionEditor({
  item,
  media,
  close,
}: {
  item?: Subscription;
  media: MediaController;
  close: () => void;
}) {
  const [value, setValue] = useState(() => draft(item));
  const [step, setStep] = useState(0);
  const [sample, setSample] = useState("");
  const [testing, setTesting] = useState(false);
  const [results, setResults] = useState<RuleResult[] | null>(null);
  const version = useRef(0);
  const heading = useRef<HTMLHeadingElement>(null);
  const controller = useRef(media);
  controller.current = media;
  useEffect(() => {
    heading.current?.focus();
    return () => {
      controller.current.cancel("rule-preview");
      version.current += 1;
    };
  }, []);
  const patch = (next: Partial<Draft>) => {
    version.current += 1;
    media.cancel("rule-preview");
    setTesting(false);
    setResults(null);
    setValue((current) => ({ ...current, ...next }));
  };
  const test = async () => {
    if (!sample.trim()) return;
    version.current += 1;
    const mine = version.current;
    setTesting(true);
    setResults(null);
    media.clearError();
    const preview = await media.request<Preview>("rule-preview", "resolve", {
      url: sample.trim(),
      account_id: value.account_id || null,
    });
    if (mine !== version.current) return;
    if (preview) {
      const response = await media.request<{ results: RuleResult[] }>(
        "rule-preview",
        "rules/preview",
        { preview_id: preview.preview_id, rules: value.rules },
      );
      if (mine !== version.current) return;
      if (response) setResults(response.results);
    }
    setTesting(false);
  };
  const ruleError =
    results?.some((result) => result.requires_rule_attention) ?? false;
  const emptyRule = [...value.rules.blacklist, ...value.rules.whitelist].some(
    (group) =>
      group.rules.some(
        (rule) =>
          !rule.value ||
          Array.from(rule.value).length > (rule.op === "regex" ? 512 : 4096),
      ),
  );
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (step < 2) {
      setStep(step + 1);
      return;
    }
    const result = await media.command<{ subscription: Subscription }>(
      "subscriptions/save",
      { ...value, account_id: value.account_id || null },
    );
    if (result) {
      media.setNotice("订阅已保存。完整建立成员基线后，才会处理新发现的视频。");
      close();
    }
  };
  const target = media.view?.targets.find(
    (entry) => entry.target_id === value.target_id,
  );
  const account = media.view?.accounts.find(
    (entry) => entry.account_id === value.account_id,
  );
  return (
    <section
      className="media-card media-subscription-editor"
      aria-label={item ? "编辑订阅" : "新建订阅"}
    >
      <div className="media-section-head">
        <h3 ref={heading} tabIndex={-1}>
          {item ? `编辑订阅 · ${item.label}` : "新建订阅"}
        </h3>
        <button className="button" type="button" onClick={close}>
          关闭编辑
        </button>
      </div>
      <ol className="media-steps" aria-label="订阅创建步骤">
        {["账号与来源", "规则与画质", "媒体库与预览"].map((title, index) => (
          <li key={title} aria-current={step === index ? "step" : undefined}>
            <span>{index + 1}</span>
            {title}
          </li>
        ))}
      </ol>
      <form onSubmit={(event) => void submit(event)} className="media-stack">
        {step === 0 && (
          <>
            <div className="media-form-grid">
              <label>
                订阅名称
                <input
                  required
                  value={value.label}
                  maxLength={120}
                  onChange={(event) => patch({ label: event.target.value })}
                />
              </label>
              <AccountField
                accounts={media.view?.accounts ?? []}
                value={value.account_id}
                onChange={(account_id) => patch({ account_id })}
              />
            </div>
            <div className="media-form-grid">
              <label>
                来源类型
                <select
                  value={value.source.kind}
                  onChange={(event) =>
                    patch({
                      source: {
                        kind: event.target.value as Source["kind"],
                        id: "",
                      },
                    })
                  }
                >
                  {Object.entries(sourceWords)
                    .filter(([kind]) =>
                      media.view?.capabilities.source_kinds.includes(kind),
                    )
                    .map(([kind, title]) => (
                      <option key={kind} value={kind}>
                        {title}
                      </option>
                    ))}
                </select>
              </label>
              <label>
                来源链接或 ID
                <input
                  required
                  value={value.source.id}
                  maxLength={4096}
                  placeholder={`粘贴${sourceWords[value.source.kind]}的 B 站链接`}
                  onChange={(event) =>
                    patch({
                      source: { ...value.source, id: event.target.value },
                    })
                  }
                />
              </label>
            </div>
            <p className="muted">
              粘贴来源页面的 B 站链接即可，也可填写已知来源
              ID。保存后会显示识别出的来源标识。
            </p>
            <fieldset className="media-history">
              <legend>首次同步</legend>
              <label className="media-check">
                <input
                  type="radio"
                  name="history"
                  checked={value.initial_sync === "future_only"}
                  onChange={() => patch({ initial_sync: "future_only" })}
                />
                仅处理后续新增
              </label>
              <label className="media-check">
                <input
                  type="radio"
                  name="history"
                  checked={value.initial_sync === "history"}
                  onChange={() => patch({ initial_sync: "history" })}
                />
                导入来源中的历史视频
              </label>
              <p className="muted">
                两种模式都会先完整扫描成员。视频发布时间早，不影响它作为新增收藏被发现。
              </p>
            </fieldset>
            <label>
              扫描间隔（分钟）
              <input
                type="number"
                min={1}
                max={10080}
                step={1}
                required
                value={value.interval_seconds / 60}
                onChange={(event) =>
                  patch({ interval_seconds: Number(event.target.value) * 60 })
                }
              />
            </label>
          </>
        )}
        {step === 1 && (
          <>
            <QualityField
              value={value.quality}
              onChange={(quality) => patch({ quality })}
            />
            <RuleEditor
              value={value.rules}
              onChange={(rules) => patch({ rules })}
              disabled={!!media.busy}
            />
            <div className="media-rule-test">
              <label>
                试算视频链接
                <input
                  type="url"
                  value={sample}
                  placeholder="粘贴一条视频链接，查看规则命中原因"
                  onChange={(event) => {
                    version.current += 1;
                    media.cancel("rule-preview");
                    setTesting(false);
                    setResults(null);
                    setSample(event.target.value);
                  }}
                />
              </label>
              <button
                type="button"
                className="button"
                disabled={
                  !sample.trim() || testing || emptyRule || !!media.busy
                }
                onClick={() => void test()}
              >
                {testing ? "正在试算…" : "试算规则"}
              </button>
            </div>
            {results && (
              <RuleResults results={results} revision={value.rules.revision} />
            )}
            {ruleError && (
              <p role="alert" className="media-warning">
                规则存在执行错误。请修正后重新试算，不能以未命中继续自动下载。
              </p>
            )}
          </>
        )}
        {step === 2 && (
          <>
            <TargetField
              targets={media.view?.targets ?? []}
              value={value.target_id}
              onChange={(target_id) => patch({ target_id })}
            />
            <div className="media-summary">
              <h4>保存前预览</h4>
              <dl className="media-facts">
                <div>
                  <dt>来源</dt>
                  <dd>
                    {sourceWords[value.source.kind]} · {value.source.id}
                  </dd>
                </div>
                <div>
                  <dt>账号</dt>
                  <dd>{account?.label ?? "公开访问"}</dd>
                </div>
                <div>
                  <dt>首次同步</dt>
                  <dd>
                    {value.initial_sync === "history"
                      ? "导入历史"
                      : "仅后续新增"}
                  </dd>
                </div>
                <div>
                  <dt>画质</dt>
                  <dd>
                    {value.quality.mode === "best"
                      ? "实际可用最高画质"
                      : `指定 ${qualityWord(value.quality.quality_id)}`}{" "}
                    · {value.quality.allow_fallback ? "允许降级" : "不降级"}
                  </dd>
                </div>
                <div>
                  <dt>规则</dt>
                  <dd>
                    黑名单 {value.rules.blacklist.length} 组 / 白名单{" "}
                    {value.rules.whitelist.length} 组
                  </dd>
                </div>
                <div>
                  <dt>目标库</dt>
                  <dd>{target?.label ?? "尚未选择"}</dd>
                </div>
                <div>
                  <dt>媒体服务器</dt>
                  <dd>
                    {target?.servers.length
                      ? target.servers.map((server) => server.label).join("、")
                      : "未配置，任务仅标记发布完成"}
                  </dd>
                </div>
              </dl>
            </div>
          </>
        )}
        <div className="media-actions">
          {step > 0 && (
            <button
              type="button"
              className="button"
              onClick={() => setStep(step - 1)}
            >
              上一步
            </button>
          )}
          <button
            type="submit"
            className="button primary"
            disabled={
              !!media.busy || testing || ruleError || (step === 1 && emptyRule)
            }
          >
            {media.busy === "subscriptions/save"
              ? "正在保存…"
              : step === 2
                ? "保存订阅"
                : "下一步"}
          </button>
        </div>
      </form>
    </section>
  );
}

export function SubscriptionsPanel({ media }: { media: MediaController }) {
  const [editing, setEditing] = useState<Subscription | "new" | null>(null);
  const [limit, setLimit] = useState(10);
  const [query, setQuery] = useState("");
  const [filter, setFilter] = useState("all");
  const trigger = useRef<HTMLElement | null>(null);
  const subscriptions = media.view?.subscriptions ?? [];
  const search = query.trim().toLocaleLowerCase();
  const accounts = new Map(
    media.view?.accounts.map((item) => [item.account_id, item.label]),
  );
  const targets = new Map(
    media.view?.targets.map((item) => [item.target_id, item.label]),
  );
  const attention = (item: Subscription) =>
    item.state === "auth_required" || item.state === "rule_error";
  const matches = subscriptions.filter((item) => {
    const statusMatch =
      filter === "all" ||
      (filter === "attention"
        ? attention(item)
        : filter === "uninitialized"
          ? !item.baseline_ready
          : item.state === filter);
    const searchable = [
      item.label,
      item.source.id,
      sourceWords[item.source.kind],
      accounts.get(item.account_id ?? "") ?? "公开访问",
      targets.get(item.target_id) ?? item.target_id,
    ];
    return (
      statusMatch &&
      (!search ||
        searchable.some((value) => value.toLocaleLowerCase().includes(search)))
    );
  });
  const filtered = !!search || filter !== "all";
  const clearFilters = () => {
    setQuery("");
    setFilter("all");
    setLimit(10);
  };
  const edit = (item: Subscription | "new", element: HTMLElement) => {
    trigger.current = element;
    setEditing(item);
  };
  const close = () => {
    setEditing(null);
    requestAnimationFrame(() => trigger.current?.focus());
  };
  const control = async (item: Subscription) => {
    const action = item.state === "active" ? "pause" : "resume";
    const result = await media.command("subscriptions/control", {
      subscription_id: item.subscription_id,
      expected_revision: item.revision,
      action,
    });
    if (result)
      media.setNotice(
        action === "pause"
          ? "订阅已暂停，已入队任务保留；取消任务请到下载任务。"
          : "订阅已恢复，后续扫描将按当前规则处理。",
      );
  };
  const scan = async (item: Subscription, recompute: boolean) => {
    const result = await media.command<{ queued: number }>(
      "subscriptions/scan",
      { subscription_id: item.subscription_id, recompute },
    );
    if (result)
      media.setNotice(
        `完整扫描已返回，本次入队 ${result.queued} 个任务。${recompute ? "历史成员按当前规则重新试算，已有成品仍按质量去重。" : ""}`,
      );
  };
  return (
    <section className="media-stack" aria-label="订阅管理">
      <div className="media-section-head">
        <div>
          <h2>订阅你关注的来源</h2>
          <p className="muted">
            以来源成员判断新增，规则变更默认仅影响以后的扫描。
          </p>
        </div>
        <button
          className="button primary"
          disabled={!!editing || !!media.busy}
          onClick={(event) => edit("new", event.currentTarget)}
        >
          <Plus aria-hidden="true" />
          新建订阅
        </button>
      </div>
      <dl className="media-subscription-stats" aria-label="已读取的订阅状态">
        <div>
          <dt>已读取</dt>
          <dd>{subscriptions.length} 个订阅</dd>
        </div>
        <div>
          <dt>运行中</dt>
          <dd>
            {subscriptions.filter((item) => item.state === "active").length}
          </dd>
        </div>
        <div>
          <dt>已暂停</dt>
          <dd>
            {subscriptions.filter((item) => item.state === "paused").length}
          </dd>
        </div>
        <div>
          <dt>需要处理</dt>
          <dd>{subscriptions.filter(attention).length}</dd>
        </div>
        <div>
          <dt>待初始化</dt>
          <dd>{subscriptions.filter((item) => !item.baseline_ready).length}</dd>
        </div>
      </dl>
      {!!subscriptions.length && (
        <div className="media-subscription-search media-form-grid">
          <label>
            查找订阅
            <input
              type="search"
              value={query}
              placeholder="名称、来源 ID、账号或媒体库"
              onChange={(event) => {
                setQuery(event.target.value);
                setLimit(10);
              }}
            />
          </label>
          <label>
            订阅状态
            <select
              value={filter}
              onChange={(event) => {
                setFilter(event.target.value);
                setLimit(10);
              }}
            >
              <option value="all">全部状态</option>
              <option value="active">运行中</option>
              <option value="paused">已暂停</option>
              <option value="attention">需要处理</option>
              <option value="auth_required">需要重新登录</option>
              <option value="rule_error">规则需要修正</option>
              <option value="uninitialized">待初始化</option>
            </select>
          </label>
          <div className="media-actions media-subscription-match">
            <p className="muted">
              {filtered
                ? `找到 ${matches.length} 个订阅`
                : `已显示 ${Math.min(limit, matches.length)} / ${matches.length} 个订阅`}
            </p>
            {filtered && (
              <button className="button" onClick={clearFilters}>
                清除查找与筛选
              </button>
            )}
          </div>
        </div>
      )}
      {editing && (
        <SubscriptionEditor
          key={editing === "new" ? "new" : editing.subscription_id}
          item={editing === "new" ? undefined : editing}
          media={media}
          close={close}
        />
      )}
      {!subscriptions.length && !editing && (
        <StatePanel kind="empty" title="还没有订阅">
          <p>添加收藏夹、合集、系列或 UP 主投稿来源，即可持续发现新增视频。</p>
        </StatePanel>
      )}
      {!!subscriptions.length && !matches.length && (
        <StatePanel
          kind="empty"
          title="没有匹配的订阅"
          action={
            <button className="button" onClick={clearFilters}>
              查看全部订阅
            </button>
          }
        >
          <p>尝试其他名称或来源，或清除当前查找与状态筛选。</p>
        </StatePanel>
      )}
      <div className="media-subscriptions">
        {matches.slice(0, limit).map((item) => (
          <article className="media-card" key={item.subscription_id}>
            <div className="media-section-head">
              <div>
                <h3>{item.label}</h3>
                <p className="muted">
                  {sourceWords[item.source.kind]} · {item.source.id}
                </p>
              </div>
              <StatusRail tone={tone(item.state)} label={word(item.state)} />
            </div>
            <dl className="media-facts">
              <div>
                <dt>成员基线</dt>
                <dd>
                  {item.baseline_ready
                    ? `完整 · ${item.counts.members} 个成员`
                    : "尚未完整建立"}
                </dd>
              </div>
              <div>
                <dt>已入队</dt>
                <dd>{item.counts.queued} 个任务</dd>
              </div>
              <div>
                <dt>最后成功扫描</dt>
                <dd>
                  {item.last_scan_at == null
                    ? "尚未成功扫描"
                    : timestamp(item.last_scan_at)}
                </dd>
              </div>
              <div>
                <dt>下次计划</dt>
                <dd>
                  {item.state === "active"
                    ? item.next_scan_at > 0
                      ? timestamp(item.next_scan_at)
                      : "等待后台安排扫描"
                    : item.state === "auth_required"
                      ? "重新登录后继续"
                      : item.state === "rule_error"
                        ? "修正规则后继续"
                        : "恢复订阅后安排"}
                </dd>
              </div>
              <div>
                <dt>扫描间隔</dt>
                <dd>
                  每{" "}
                  {item.interval_seconds % 60 === 0
                    ? `${item.interval_seconds / 60} 分钟`
                    : `${item.interval_seconds} 秒`}
                </dd>
              </div>
              <div>
                <dt>目标媒体库</dt>
                <dd>{targets.get(item.target_id) ?? item.target_id}</dd>
              </div>
            </dl>
            {!item.baseline_ready && (
              <p className="media-warning">
                初始化未完成，订阅尚未建立可判断新增的完整基线。
              </p>
            )}
            {item.state === "auth_required" && (
              <p className="media-warning">
                当前账号需要重新登录。
                <a href="#/subscriptions/3">更新 B 站登录</a>后，再恢复订阅。
              </p>
            )}
            {item.state === "rule_error" && (
              <p className="media-warning">
                请编辑并试算规则，修正后再恢复订阅。
              </p>
            )}
            <ReasonNote code={item.code} />
            <div className="media-actions">
              <button
                className="button"
                disabled={!!editing || !!media.busy}
                onClick={(event) => edit(item, event.currentTarget)}
              >
                编辑
              </button>
              <button
                className="button"
                disabled={!!media.busy}
                onClick={() => void control(item)}
              >
                {item.state === "active" ? "暂停订阅" : "恢复订阅"}
              </button>
              <button
                className="button"
                disabled={!!media.busy}
                onClick={() => void scan(item, false)}
              >
                <RefreshCw aria-hidden="true" />
                立即扫描
              </button>
              <button
                className="button"
                disabled={!!media.busy}
                onClick={() => void scan(item, true)}
              >
                按当前规则重算历史
              </button>
            </div>
          </article>
        ))}
      </div>
      {matches.length > limit && (
        <button className="button" onClick={() => setLimit(limit + 10)}>
          显示更多订阅
        </button>
      )}
    </section>
  );
}
