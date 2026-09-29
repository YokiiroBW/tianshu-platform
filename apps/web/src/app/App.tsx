import { Suspense, useEffect, useRef, useState } from "react";
import { Menu, Palette, Sparkles, WifiOff } from "lucide-react";
import { modules, pages, resolveRoute } from "./modules";
import {
  AuthProvider,
  AccountPage,
  SessionStatus,
  authRoute,
  loginHref,
  returnTarget,
  useAuth,
} from "./Auth";
import { Drawer } from "../components/Drawer";
import { PageBoundary } from "../components/PageBoundary";
import { StatePanel } from "../components/StatePanel";
import { UnavailablePage } from "../components/UnavailablePage";

type Preferences = {
  theme: "system" | "light" | "dark";
  motion: boolean;
  transparency: boolean;
};
const defaults: Preferences = {
  theme: "system",
  motion: false,
  transparency: false,
};
function readPreferences(): Preferences {
  try {
    const saved = JSON.parse(
      localStorage.getItem("tianshu.appearance") ?? "null",
    );
    return {
      theme: ["system", "light", "dark"].includes(saved?.theme)
        ? saved.theme
        : "system",
      motion: saved?.motion === true,
      transparency: saved?.transparency === true,
    };
  } catch {
    return defaults;
  }
}

export function App() {
  return (
    <AuthProvider>
      <AppShell />
    </AuthProvider>
  );
}

function AppShell() {
  const auth = useAuth();
  const [hash, setHash] = useState(window.location.hash);
  const acceptedHash = useRef(window.location.hash);
  const [drawer, setDrawer] = useState<"navigation" | "appearance" | null>(
    null,
  );
  const [preferences, setPreferences] = useState(readPreferences);
  const [storageUnavailable, setStorageUnavailable] = useState(false);
  const [offline, setOffline] = useState(!navigator.onLine);
  const main = useRef<HTMLElement>(null);
  const route = resolveRoute(hash);
  const current = route?.module;
  const section = route?.section ?? 0;
  const account = authRoute(hash);
  const accountTitle = hash.startsWith("#/setup") ? "首次设置" : "登录";
  const protectedPage =
    !!current &&
    [
      "companion",
      "home",
      "resources",
      "settings",
      "memory",
      "projects",
    ].includes(current.id);
  const setupNeeded =
    auth.session?.onboarding?.state === "create_admin" ||
    (auth.session?.authenticated &&
      auth.session.onboarding?.state === "claim_admin");
  useEffect(() => {
    if (auth.loading || auth.error || !auth.session) return;
    if (setupNeeded && current?.id !== "room" && !hash.startsWith("#/setup")) {
      window.location.hash = `#/setup?next=${encodeURIComponent(account ? returnTarget() : hash || "#/workbench")}`;
    } else if (protectedPage && !auth.session.authenticated)
      window.location.hash = loginHref(hash);
  }, [
    auth.loading,
    auth.error,
    auth.session,
    setupNeeded,
    protectedPage,
    current,
    hash,
    account,
  ]);

  useEffect(() => {
    const change = () => {
      const next = window.location.hash;
      if (next !== acceptedHash.current) {
        const leaving = new CustomEvent("tianshu:before-route", {
          cancelable: true,
          detail: { from: acceptedHash.current, to: next },
        });
        if (!window.dispatchEvent(leaving)) {
          // A history traversal already changed the URL. Restore the editor route without
          // triggering a second hashchange or losing the mounted form.
          window.history.pushState(
            null,
            "",
            window.location.pathname +
              window.location.search +
              acceptedHash.current,
          );
          return;
        }
        acceptedHash.current = next;
      }
      setHash(next);
      setDrawer(null);
    };
    const online = () => setOffline(!navigator.onLine);
    window.addEventListener("hashchange", change);
    window.addEventListener("online", online);
    window.addEventListener("offline", online);
    return () => {
      window.removeEventListener("hashchange", change);
      window.removeEventListener("online", online);
      window.removeEventListener("offline", online);
    };
  }, []);
  useEffect(() => {
    document.title = `${account ? accountTitle : (current?.sections[section] ?? current?.label ?? "页面不存在")} · 天枢`;
    const frame = requestAnimationFrame(() =>
      main.current?.focus({ preventScroll: true }),
    );
    return () => cancelAnimationFrame(frame);
  }, [hash, current, section]);
  useEffect(() => {
    const media = matchMedia("(prefers-color-scheme: dark)");
    const apply = () => {
      document.documentElement.dataset.theme =
        preferences.theme === "system"
          ? media.matches
            ? "dark"
            : "light"
          : preferences.theme;
    };
    apply();
    document.documentElement.dataset.motion = preferences.motion
      ? "reduce"
      : "system";
    document.documentElement.dataset.transparency = preferences.transparency
      ? "reduce"
      : "system";
    try {
      localStorage.setItem("tianshu.appearance", JSON.stringify(preferences));
      setStorageUnavailable(false);
    } catch {
      setStorageUnavailable(true);
    }
    media.addEventListener("change", apply);
    return () => media.removeEventListener("change", apply);
  }, [preferences]);

  const navigation = (
    <nav aria-label="工作区导航">
      {["个人空间", "工作空间", "管理"].map((group) => (
        <div className="nav-group" key={group}>
          <p className="nav-label">{group}</p>
          {modules
            .filter((item) => item.group === group)
            .map((item) => (
              <a
                key={item.id}
                className="nav-item"
                href={`#/${item.id}`}
                aria-current={current?.id === item.id ? "page" : undefined}
                onClick={() => setDrawer(null)}
              >
                <item.icon aria-hidden="true" />
                <span>{item.label}</span>
              </a>
            ))}
        </div>
      ))}
    </nav>
  );
  let page;
  if (account) page = <AccountPage />;
  else if (
    (protectedPage || setupNeeded) &&
    (auth.loading ||
      auth.error ||
      !auth.session?.authenticated ||
      setupNeeded) &&
    current?.id !== "room"
  )
    page = <SessionStatus />;
  else if (!current)
    page = (
      <StatePanel
        kind="empty"
        title="找不到这个页面"
        action={
          <a className="button" href="#/workbench">
            返回工作台
          </a>
        }
      >
        <p>这个入口不存在，或地址不完整。请从导航选择工作区。</p>
      </StatePanel>
    );
  else if (current.id === "workbench") page = <pages.workbench />;
  else if (current.id === "room") page = <pages.room />;
  else if (current.id === "companion" && section === 0)
    page = <pages.companion />;
  else if (current.id === "companion" && section === 2)
    page = <pages.personas />;
  else if (current.id === "companion" && section === 1) page = <pages.life />;
  else if (current.id === "home" && section === 0) page = <pages.home />;
  else if (current.id === "memory" && section !== 2)
    page = <pages.memory section={section} />;
  else if (current.id === "resources" && section === 0)
    page = <pages.knowledge />;
  else if (current.id === "resources" && section === 1)
    page = <pages.resources />;
  else if (current.id === "projects" && section === 0)
    page = <pages.knowledge />;
  else if (current.id === "projects" && section === 1)
    page = <pages.experience />;
  else if (current.id === "settings")
    page = <pages.settings section={section} />;
  else page = <UnavailablePage module={current} section={section} />;

  return (
    <>
      <a
        className="skip-link"
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          main.current?.focus();
        }}
      >
        跳到主要内容
      </a>
      <div className="app-shell">
        <aside className="sidebar glass">
          <a className="brand" href="#/workbench">
            <span className="brand-symbol">
              <Sparkles aria-hidden="true" />
            </span>
            <span>
              <strong>天枢</strong>
              <small>个人空间</small>
            </span>
          </a>
          {navigation}
          <div className="sidebar-note">
            本地工作区
            <br />
            <span>各页面显示实际接入状态</span>
          </div>
        </aside>
        <div className="main-column">
          <header className="topbar glass">
            <button
              className="icon-button mobile-menu"
              aria-label="打开导航"
              aria-haspopup="dialog"
              aria-expanded={drawer === "navigation"}
              onClick={() => setDrawer("navigation")}
            >
              <Menu aria-hidden="true" />
            </button>
            <p className="breadcrumb">
              <span>天枢</span>
              <span aria-hidden="true">/</span>
              <strong>
                {account ? accountTitle : (current?.label ?? "页面不存在")}
              </strong>
            </p>
            <div className="account-control">
              {auth.session?.authenticated ? (
                <>
                  <span>{auth.session.username}</span>
                  <button className="button" onClick={() => void auth.logout()}>
                    退出登录
                  </button>
                </>
              ) : (
                <a
                  className="button"
                  href={setupNeeded ? "#/setup" : loginHref(hash)}
                >
                  {setupNeeded ? "首次设置" : "登录"}
                </a>
              )}
            </div>
            <button
              className="icon-button"
              aria-label="外观设置"
              aria-haspopup="dialog"
              onClick={() => setDrawer("appearance")}
            >
              <Palette aria-hidden="true" />
            </button>
          </header>
          {offline && (
            <div className="offline-notice" role="status">
              <WifiOff aria-hidden="true" />
              浏览器当前离线。已加载的页面仍可浏览。
            </div>
          )}
          <main id="main" ref={main} tabIndex={-1}>
            <div className="page-heading">
              <div>
                <p className="eyebrow">
                  {account
                    ? "账号与首次使用"
                    : (current?.description ?? "检查页面地址")}
                </p>
                <h1>
                  {account ? accountTitle : (current?.label ?? "页面不存在")}
                </h1>
              </div>
            </div>
            {current && current.sections.length > 0 && (
              <nav className="section-nav" aria-label={`${current.label}页面`}>
                {current.sections.map((label, index) => (
                  <a
                    key={label}
                    href={`#/${current.id}/${index}`}
                    aria-current={section === index ? "page" : undefined}
                  >
                    {label}
                  </a>
                ))}
              </nav>
            )}
            <PageBoundary key={hash}>
              <Suspense
                fallback={
                  <StatePanel kind="loading" title="正在加载页面">
                    <p>请稍候。</p>
                  </StatePanel>
                }
              >
                {page}
              </Suspense>
            </PageBoundary>
          </main>
          <footer>
            天枢 · 个人空间<span>服务状态以各页面为准</span>
          </footer>
        </div>
      </div>
      <Drawer
        title="导航"
        open={drawer === "navigation"}
        onClose={() => setDrawer(null)}
      >
        {navigation}
      </Drawer>
      <Drawer
        title="外观设置"
        open={drawer === "appearance"}
        onClose={() => setDrawer(null)}
      >
        <p className="muted">仅影响此浏览器的显示方式。</p>
        <fieldset className="appearance-options">
          <legend>主题</legend>
          {(["system", "light", "dark"] as const).map((value, index) => (
            <label key={value}>
              <input
                type="radio"
                name="theme"
                value={value}
                checked={preferences.theme === value}
                onChange={() =>
                  setPreferences({ ...preferences, theme: value })
                }
              />
              {["跟随系统", "浅色", "深色"][index]}
            </label>
          ))}
        </fieldset>
        <label className="preference-row">
          <span>
            减少动态<small>系统要求减少动态时始终生效</small>
          </span>
          <input
            type="checkbox"
            checked={preferences.motion}
            onChange={(event) =>
              setPreferences({ ...preferences, motion: event.target.checked })
            }
          />
        </label>
        <label className="preference-row">
          <span>
            减少透明度<small>使用更实的导航与浮层背景</small>
          </span>
          <input
            type="checkbox"
            checked={preferences.transparency}
            onChange={(event) =>
              setPreferences({
                ...preferences,
                transparency: event.target.checked,
              })
            }
          />
        </label>
        {storageUnavailable && (
          <p role="status">浏览器未允许保存偏好，本次页面内仍然有效。</p>
        )}
      </Drawer>
    </>
  );
}
