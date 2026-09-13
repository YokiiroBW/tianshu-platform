import { useEffect, useRef, useState } from "react";
import { Sun, LampDesk, Lamp, Wind, SlidersHorizontal } from "lucide-react";
import {
  formatHour,
  localHour,
  resolveRoom,
  type Overrides,
  type PreviewInput,
  type RoomValues,
} from "./environment";
import { mountRoom, type RenderStatus } from "./renderer";
import "./room.css";

const objects = [
  { key: "window", label: "窗户", hint: "向外开启 · 关闭仍透光", icon: Wind },
  { key: "sheer", label: "纱帘", hint: "柔化日光 · 半透明", icon: Sun },
  {
    key: "blackout",
    label: "遮光帘",
    hint: "遮挡窗外与直射光",
    icon: SlidersHorizontal,
  },
  { key: "desk", label: "书桌灯", hint: "照亮桌面与近处地板", icon: LampDesk },
  { key: "bedside", label: "床头灯", hint: "照亮床边与枕边", icon: Lamp },
] as const;

export default function RoomPage() {
  const [hour, setHour] = useState<number | null>(14);
  const [clockHour, setClockHour] = useState(localHour);
  const [overrides, setOverrides] = useState<Overrides>({});
  const [simple, setSimple] = useState(false);
  const [retry, setRetry] = useState(0);
  const [status, setStatus] = useState<RenderStatus>("loading");
  const canvasHost = useRef<HTMLDivElement>(null);
  const renderer = useRef<ReturnType<typeof mountRoom> | null>(null);
  const input = useRef<PreviewInput>({ hour, overrides });
  const shownHour = hour ?? clockHour;
  const values = resolveRoom(shownHour, overrides);
  const unavailable = status === "error" || status === "lost";
  useEffect(() => {
    input.current = { hour, overrides };
    renderer.current?.update();
  }, [hour, overrides]);
  useEffect(() => {
    if (!canvasHost.current) return;
    // The effect owns its canvas as well as the GPU context. StrictMode's
    // setup/cleanup/setup gets a fresh canvas instead of a deliberately lost one.
    const surface = document.createElement("canvas");
    surface.setAttribute("role", "img");
    surface.setAttribute(
      "aria-label",
      "固定视角小屋：左侧窗户和双层帘，阅读姿态占位、前景桌椅，右侧床与两盏独立灯。使用下方普通控件调整。",
    );
    surface.setAttribute("aria-describedby", "room-caption");
    canvasHost.current.append(surface);
    setStatus("loading");
    try {
      renderer.current = mountRoom(
        surface,
        () => input.current,
        simple,
        setStatus,
      );
    } catch {
      setStatus("error");
    }
    return () => {
      renderer.current?.dispose();
      renderer.current = null;
      surface.remove();
    };
  }, [simple, retry]);
  useEffect(() => {
    if (hour !== null) return;
    let timer = 0;
    const sync = () => {
      clearTimeout(timer);
      if (document.hidden) return;
      setClockHour(localHour());
      timer = window.setTimeout(sync, 1000);
    };
    sync();
    document.addEventListener("visibilitychange", sync);
    return () => {
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", sync);
    };
  }, [hour]);
  function manual(key: keyof RoomValues, value: number) {
    setOverrides((previous) => ({ ...previous, [key]: value }));
  }
  function restore(key: (typeof objects)[number]["key"]) {
    setOverrides((previous) => {
      const next = { ...previous };
      delete next[key];
      if (key === "desk") delete next.deskWarmth;
      if (key === "bedside") delete next.bedsideWarmth;
      return next;
    });
  }
  return (
    <section className="room-preview" aria-labelledby="room-title">
      <header className="room-heading">
        <div>
          <p className="eyebrow">光、微风与阅读角</p>
          <h2 id="room-title">小屋环境预览</h2>
        </div>
        <span className="badge">仅本地预览</span>
      </header>
      <p className="room-boundary">
        虚构小屋 ·
        未连接核心生活服务或真实家庭设备。调整仅在此页面保持，离开后重置。
      </p>
      <figure className="room-figure">
        <div className="room-stage" aria-busy={status === "loading"}>
          <div className="room-canvas" ref={canvasHost} hidden={unavailable} />
          {status !== "ready" && (
            <div
              className="room-fallback"
              role={unavailable ? "alert" : "status"}
            >
              <h3>
                {status === "loading"
                  ? "正在准备小屋画面"
                  : status === "lost"
                    ? "图形画面暂时中断"
                    : "当前无法显示 3D 画面"}
              </h3>
              <p>
                {status === "lost"
                  ? "等待浏览器恢复图形资源，也可以重试。预览设置仍在。"
                  : status === "error"
                    ? "浏览器可能未提供 WebGL 2。可以重试，或继续使用下方环境控件查看设定。"
                    : "首次进入需要准备场景与光影。"}
              </p>
              {unavailable && (
                <button
                  className="button"
                  onClick={() => setRetry((n) => n + 1)}
                >
                  重试画面
                </button>
              )}
            </div>
          )}
        </div>
        <figcaption id="room-caption">
          <span>阅读姿态 · 静态几何占位</span>
          <span>{formatHour(shownHour)} · 晴天曲线示意</span>
        </figcaption>
      </figure>
      <div className="room-time">
        <div className="room-time-heading">
          <label htmlFor="room-time">
            预览时间 <strong>{formatHour(shownHour)}</strong>
          </label>
          <span className="muted">
            {hour === null
              ? "跟随本机时钟 · 上海时区"
              : "时间已固定 · 不推进生活事件"}
          </span>
        </div>
        <input
          id="room-time"
          type="range"
          min="0"
          max="1439"
          step="1"
          value={Math.floor(shownHour * 60)}
          aria-valuetext={formatHour(shownHour)}
          onChange={(e) => setHour(Number(e.target.value) / 60)}
        />
        <div className="room-time-actions">
          <div className="room-presets" aria-label="时间快捷预览">
            {[
              [8, "清晨"],
              [14, "午后"],
              [18.5, "黄昏"],
              [23, "夜晚"],
            ].map(([value, label]) => (
              <button
                key={label}
                className="button"
                aria-pressed={hour === value}
                onClick={() => setHour(Number(value))}
              >
                {label}
              </button>
            ))}
          </div>
          <button
            className="button"
            aria-pressed={hour === null}
            onClick={() => {
              setClockHour(localHour());
              setHour(null);
            }}
          >
            跟随本机时间
          </button>
        </div>
      </div>
      <details className="room-controls" open>
        <summary>
          环境控制 <span className="muted">窗、双层帘与两盏灯</span>
        </summary>
        <p className="room-control-note">
          调整后该物件保持手动；点击“恢复自动”才重新跟随本地昼夜示意规则。开度
          100% 表示完全拉开。
        </p>
        <div className="room-object-list">
          {objects.map(({ key, label, hint, icon: Icon }) => {
            const lamp = key === "desk" || key === "bedside";
            const warmthKey = key === "desk" ? "deskWarmth" : "bedsideWarmth";
            const held =
              overrides[key] !== undefined ||
              (lamp && overrides[warmthKey] !== undefined);
            return (
              <fieldset className="room-object" key={key}>
                <legend>
                  <Icon aria-hidden="true" />
                  {label}
                </legend>
                <div className="room-object-meta">
                  <span>{hint}</span>
                  <span className="room-mode">
                    {held ? "手动保持" : "本地自动"}
                  </span>
                </div>
                <label htmlFor={`room-${key}`}>
                  {lamp ? "亮度" : "开度"}
                  <strong>{Math.round(values[key] * 100)}%</strong>
                </label>
                <input
                  id={`room-${key}`}
                  aria-label={`${label}${lamp ? "亮度" : "开度"}`}
                  type="range"
                  min="0"
                  max="100"
                  value={Math.round(values[key] * 100)}
                  onChange={(e) => manual(key, Number(e.target.value) / 100)}
                />
                {lamp && (
                  <>
                    <label htmlFor={`room-${warmthKey}`}>
                      光色
                      <strong>
                        {Math.round(5000 - values[warmthKey] * 2300)} K
                      </strong>
                    </label>
                    <input
                      id={`room-${warmthKey}`}
                      aria-label={`${label}光色`}
                      type="range"
                      min="0"
                      max="100"
                      value={Math.round(values[warmthKey] * 100)}
                      aria-valuetext={`约 ${Math.round(5000 - values[warmthKey] * 2300)} K`}
                      onChange={(e) =>
                        manual(warmthKey, Number(e.target.value) / 100)
                      }
                    />
                  </>
                )}
                <div className="room-object-actions">
                  <button
                    className="button"
                    onClick={() => manual(key, values[key] > 0 ? 0 : 1)}
                  >
                    {values[key] > 0 ? "关闭" : lamp ? "开启" : "打开"}
                    {label}
                  </button>
                  <button
                    className="button"
                    disabled={!held}
                    aria-label={`恢复${label}自动`}
                    onClick={() => restore(key)}
                  >
                    恢复自动
                  </button>
                </div>
              </fieldset>
            );
          })}
        </div>
        <label className="room-quality">
          <input
            type="checkbox"
            checked={simple}
            onChange={(e) => setSimple(e.target.checked)}
          />
          简化画质<span className="muted">降低分辨率与阴影精度</span>
        </label>
      </details>
      <details className="room-notes">
        <summary>关于此预览与素材</summary>
        <p>
          房间、家具、角色与衣物均为本项目程序生成的几何占位。阅读姿态固定；未实现角色待机、行走、换装或衣物动画。窗扇开合、双层帘开合和风摆、日光与两灯局部光照已实现。
        </p>
        <p>
          昼夜为虚构的 06:00–19:00
          晴天曲线，无地点、真实天气或精确天文数据。纱帘采用简化透光和网格形变，不是布料或全局光照物理模拟。减少动态遵循系统与外观设置，停止风摆和开合过渡，保留目标状态。
        </p>
        <p>
          尚未验证关闭网页后持续生活、跨浏览器一致性或真实核心状态。这里不会保存房间状态、发送生活命令或操作
          HA 设备。
        </p>
      </details>
      <a className="text-link room-companion" href="#/companion">
        前往陪伴
      </a>
    </section>
  );
}
