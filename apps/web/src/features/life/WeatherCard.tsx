import { useEffect, useRef, useState, type FormEvent } from "react";
import {
  CloudSun,
  Cloud,
  CloudRain,
  CloudSnow,
  Sun,
  Moon,
  CloudLightning,
  Settings2,
  X,
  Search,
  RefreshCw,
} from "lucide-react";
import {
  integrationPost,
  IntegrationError,
  readFailure,
} from "../../app/integrationApi";
import { requestId } from "../../app/requestId";
import "./weather.css";

type Location = {
  id: string;
  name: string;
  adm1: string;
  adm2: string;
  country: string;
  tz: string;
  utc_offset: string;
};
type WeatherView = {
  revision: number;
  can_manage: boolean;
  configured: boolean;
  credential_configured: boolean;
  host: string | null;
  location: Location | null;
  server_time: number;
  code: string;
  weather?: {
    temp: string;
    feels_like: string;
    text: string;
    icon: string;
    wind_scale: string;
    observed_at: string | null;
    attributions?: string[];
  } | null;
  fetched_at?: string | null;
  stale?: boolean;
};
export type WeatherClock = {
  timezone: string;
  serverTime: number;
  receivedAt: number;
};
const problems: Record<string, string> = {
  invalid_weather_host:
    "请填写和风控制台的 API Host，例如 abcxyz.re.qweatherapi.com，不要附带接口路径或参数。",
  weather_store_unavailable: "服务端尚未启用天气连接存储。",
  weather_unauthorized: "和风天气凭据未通过验证，请检查 API Key 和 API Host。",
  weather_unavailable: "和风天气暂时无法连接，请稍后刷新。",
  weather_invalid_response: "和风天气未返回完整数据，请稍后刷新。",
  weather_stale: "当前显示上次读数，等待天气更新。",
  weather_not_configured: "尚未连接和风天气，请先设置 API Host 和凭据。",
  weather_location_required: "请选择天气位置。",
  weather_location_missing: "请选择天气位置。",
  weather_credentials_rejected: "和风天气凭据未通过验证，请检查 API Key。",
  weather_upstream_unavailable: "天气服务暂时无法连接，请稍后刷新。",
  weather_not_available: "天气连接暂不可用。",
  external_not_configured: "服务端尚未配置连接存储。",
  revision_conflict: "配置已更新，请重新打开设置后再保存。",
};
function failure(cause: unknown) {
  return cause instanceof IntegrationError && problems[cause.code]
    ? problems[cause.code]
    : readFailure(cause);
}
function WeatherIcon({ code }: { code?: string }) {
  const n = Number(code);
  const Icon =
    n === 100
      ? Sun
      : n === 150
        ? Moon
        : n >= 400 && n < 500
          ? CloudSnow
          : n === 302 || n === 303
            ? CloudLightning
            : n >= 300 && n < 400
              ? CloudRain
              : n >= 104 && n < 150
                ? Cloud
                : CloudSun;
  return <Icon aria-hidden="true" />;
}
function locationLabel(location: Location) {
  return [...new Set([location.adm2, location.name].filter(Boolean))].join(
    " · ",
  );
}

/** QWeather credentials stay in the Platform encrypted connection catalog. */
export function WeatherCard({
  actor,
  csrf,
  onClockChange,
}: {
  actor: string;
  csrf: string;
  onClockChange: (clock: WeatherClock | null) => void;
}) {
  const [view, setView] = useState<WeatherView | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [host, setHost] = useState("");
  const [key, setKey] = useState("");
  const [query, setQuery] = useState("");
  const [locations, setLocations] = useState<Location[]>([]);
  const [searched, setSearched] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState("");
  const [notice, setNotice] = useState("");
  const dialog = useRef<HTMLDialogElement>(null);
  const read = useRef<AbortController | null>(null);
  const mutation = useRef<AbortController | null>(null);

  function accept(next: WeatherView) {
    if (next.location) {
      try {
        new Intl.DateTimeFormat("zh-CN", {
          timeZone: next.location.tz,
        }).format();
      } catch {
        const message = "天气服务返回的时区无效，请重新选择位置。";
        setError(message);
        setFormError(message);
        return false;
      }
    }
    setView(next);
    if (next.location && Number.isFinite(next.server_time)) {
      onClockChange({
        timezone: next.location.tz,
        serverTime: next.server_time,
        receivedAt: performance.now(),
      });
    } else onClockChange(null);
    return true;
  }
  async function refresh() {
    read.current?.abort();
    const controller = new AbortController();
    read.current = controller;
    setLoading(true);
    setError("");
    try {
      const next = await integrationPost<WeatherView>(
        "weather/current",
        { actor_id: actor },
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) accept(next);
    } catch (cause) {
      if (!controller.signal.aborted) setError(failure(cause));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }
  useEffect(() => {
    setView(null);
    onClockChange(null);
    void refresh();
    return () => {
      read.current?.abort();
      mutation.current?.abort();
    };
  }, [actor, csrf]);
  useEffect(() => {
    if (open) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh();
    }, 5 * 60_000);
    const resume = () => {
      if (!document.hidden) void refresh();
    };
    document.addEventListener("visibilitychange", resume);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", resume);
    };
  }, [open, actor, csrf]);
  useEffect(() => {
    if (open) dialog.current?.showModal();
    else dialog.current?.close();
  }, [open]);

  function openSettings() {
    read.current?.abort();
    setLoading(false);
    setHost(view?.host ?? "");
    setKey("");
    setQuery("");
    setLocations([]);
    setSearched(false);
    setNotice("");
    setFormError("");
    setOpen(true);
  }
  function closeSettings() {
    mutation.current?.abort();
    setSaving(false);
    setKey("");
    setOpen(false);
  }
  async function operation<T>(
    path: string,
    body: object,
  ): Promise<T | undefined> {
    mutation.current?.abort();
    const controller = new AbortController();
    mutation.current = controller;
    setSaving(true);
    setFormError("");
    setNotice("");
    try {
      const result = await integrationPost<T>(
        `weather/${path}`,
        { actor_id: actor, ...body },
        csrf,
        controller.signal,
      );
      if (!controller.signal.aborted) return result;
    } catch (cause) {
      if (!controller.signal.aborted) {
        setFormError(failure(cause));
        if (cause instanceof IntegrationError && cause.status === 409)
          void refresh();
      }
    } finally {
      if (!controller.signal.aborted) setSaving(false);
    }
  }
  async function configure(event: FormEvent) {
    event.preventDefault();
    if (!view) return;
    const next = await operation<WeatherView>("configure", {
      host: host.trim(),
      credential: key ? { action: "replace", value: key } : { action: "keep" },
      expected_revision: view.revision,
      client_id: requestId(),
    });
    if (next) {
      setKey("");
      if (!accept(next)) return;
      setNotice("连接配置已保存。选择位置后会读取实际天气。");
      setLocations([]);
      setSearched(false);
      void refresh();
    }
  }
  async function search(event: FormEvent) {
    event.preventDefault();
    setLocations([]);
    setSearched(false);
    const result = await operation<{ items: Location[] }>("locations", {
      query: query.trim(),
    });
    if (result) {
      setLocations(result.items);
      setSearched(true);
    }
  }
  async function choose(location: Location) {
    if (!view) return;
    const next = await operation<WeatherView>("location", {
      location_id: location.id,
      expected_revision: view.revision,
      client_id: requestId(),
    });
    if (next) {
      if (!accept(next)) return;
      closeSettings();
      void refresh();
    }
  }
  const weather = view?.weather;
  const stale = Boolean(weather && (view?.stale || error));
  let observed = "";
  const weatherTime = weather?.observed_at || view?.fetched_at;
  if (weatherTime && Number.isFinite(Date.parse(weatherTime))) {
    observed = new Date(weatherTime!).toLocaleString("zh-CN", {
      timeZone: view?.location?.tz,
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
    });
  }
  return (
    <div className="life-weather" aria-label="实际天气" aria-busy={loading}>
      <div className={`life-weather-art ${weather ? "" : "is-empty"}`}>
        <WeatherIcon code={weather?.icon} />
      </div>
      <div className="life-weather-copy">
        <div className="life-weather-reading">
          {weather ? (
            <>
              <strong>
                {weather.temp}
                <small>°C</small>
              </strong>
              <span>{weather.text}</span>
            </>
          ) : (
            <strong className="life-weather-empty">
              {loading && !view
                ? "读取天气…"
                : error
                  ? "天气读取失败"
                  : !view?.configured
                    ? "天气未连接"
                    : !view.location
                      ? "选择天气位置"
                      : "天气暂不可用"}
            </strong>
          )}
        </div>
        {weather ? (
          <p>
            体感 {weather.feels_like}°C · 风力 {weather.wind_scale} 级
          </p>
        ) : (
          <p>
            {view?.location
              ? locationLabel(view.location)
              : "连接真实天气与当地时间"}
          </p>
        )}
        {view?.location && weather && (
          <p className="life-weather-place">
            {locationLabel(view.location)} · {stale ? "上次读数" : "更新于"}{" "}
            {observed || "时间未知"}
          </p>
        )}
        {(stale || error) && (
          <p className="life-weather-warning" role="status">
            {error || "天气更新失败，当前显示上次读数。"}
          </p>
        )}
        {!weather &&
          view?.code &&
          problems[view.code] &&
          view.code !== "weather_not_configured" && (
            <p>{problems[view.code]}</p>
          )}
        <div className="life-weather-actions">
          {view?.can_manage && (
            <button type="button" onClick={openSettings}>
              <Settings2 size={13} aria-hidden="true" />
              {view.configured ? "位置与天气" : "设置天气"}
            </button>
          )}
          {(error || (view?.configured && !weather)) && (
            <button
              type="button"
              disabled={loading}
              onClick={() => void refresh()}
            >
              <RefreshCw size={13} aria-hidden="true" />
              重试天气
            </button>
          )}
          {view?.configured && (
            <a
              href="https://www.qweather.com/"
              target="_blank"
              rel="noreferrer"
            >
              和风天气
            </a>
          )}
        </div>
        {weather?.attributions?.length ? (
          <p className="life-weather-attribution">
            {weather.attributions.join(" · ")}
          </p>
        ) : null}
      </div>
      <dialog
        className="life-weather-dialog"
        ref={dialog}
        aria-labelledby="weather-settings-title"
        onClose={closeSettings}
        onClick={(event) => {
          if (event.target === event.currentTarget) dialog.current?.close();
        }}
      >
        <header>
          <div>
            <span className="life-eyebrow">真实天气 · 当地时间</span>
            <h2 id="weather-settings-title">位置与天气</h2>
          </div>
          <button
            type="button"
            className="life-icon-button"
            aria-label="关闭天气设置"
            onClick={() => dialog.current?.close()}
          >
            <X />
          </button>
        </header>
        <p className="muted">
          按这位角色所选位置显示天气和当地时间，日程仍按角色原有时区推进。
        </p>
        <form onSubmit={configure} className="life-weather-form">
          <label>
            和风天气 API Host
            <input
              value={host}
              required
              maxLength={255}
              placeholder="例如 abcxyz.re.qweatherapi.com"
              autoComplete="off"
              onChange={(event) => setHost(event.target.value)}
            />
          </label>
          <label>
            API Key
            <input
              type="password"
              value={key}
              required={!view?.credential_configured}
              maxLength={4096}
              autoComplete="new-password"
              placeholder={
                view?.credential_configured
                  ? "已保存，留空保留原凭据"
                  : "输入和风天气 API Key"
              }
              onChange={(event) => setKey(event.target.value)}
            />
          </label>
          <p className="muted">
            连接供所有角色共用；位置分别保存。凭据仅提交到天枢服务端，不会回显。
          </p>
          <button className="button" disabled={saving}>
            保存连接
          </button>
        </form>
        <form onSubmit={search} className="life-weather-search">
          <label>
            搜索城市或区县
            <input
              value={query}
              maxLength={100}
              required
              placeholder="例如：上海、杭州西湖"
              onChange={(event) => setQuery(event.target.value)}
            />
          </label>
          <button
            className="button"
            disabled={saving || !view?.configured || !query.trim()}
          >
            <Search size={18} />
            搜索位置
          </button>
        </form>
        {view?.location && (
          <p className="muted">
            当前：{locationLabel(view.location)} · {view.location.tz}
          </p>
        )}
        {formError && (
          <p className="life-weather-warning" role="alert">
            {formError}
          </p>
        )}
        {notice && <p role="status">{notice}</p>}
        {searched && locations.length === 0 && (
          <p role="status">没有找到位置，请尝试城市名或经纬度。</p>
        )}
        <ul className="life-weather-results">
          {locations.map((location) => (
            <li key={location.id}>
              <button
                type="button"
                disabled={saving}
                onClick={() => void choose(location)}
              >
                <strong>{locationLabel(location)}</strong>
                <span>
                  {location.country} · {location.adm1} · {location.tz}
                </span>
              </button>
            </li>
          ))}
        </ul>
        <a
          href="https://dev.qweather.com/docs/configuration/api-config/"
          target="_blank"
          rel="noreferrer"
        >
          查看和风天气接入说明 ↗
        </a>
      </dialog>
    </div>
  );
}
