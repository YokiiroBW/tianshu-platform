import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import qrcode from "qrcode-generator";
import { QrCode, RefreshCw } from "lucide-react";
import { StatusRail } from "../../components/StatusRail";
import { StatePanel } from "../../components/StatePanel";
import { requestId } from "../../app/requestId";
import type { Account, QrSession } from "./types";
import type { MediaController } from "./useMediaController";
import { timestamp, tone, word } from "./wording";

export function AccountsPanel({ media }: { media: MediaController }) {
  const [label, setLabel] = useState("我的 B 站账号");
  const [cookie, setCookie] = useState("");
  const [qr, setQr] = useState<QrSession | null>(null);
  const [qrState, setQrState] = useState("waiting");
  const [reading, setReading] = useState("");
  const generation = useRef(0);
  const controller = useRef(media);
  controller.current = media;
  const pollClient = useRef(requestId());
  const qrImage = useMemo(() => {
    if (!qr) return null;
    try {
      const code = qrcode(0, "M");
      code.addData(qr.url);
      code.make();
      return code.createDataURL(5, 20);
    } catch {
      return null;
    }
  }, [qr]);
  useEffect(
    () => () => {
      generation.current += 1;
      controller.current.cancel("account");
      controller.current.cancel("qr-start");
      controller.current.cancel("qr-poll");
    },
    [],
  );
  useEffect(() => {
    if (!qr || ["ready", "expired"].includes(qrState)) return;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const poll = async () => {
      if (stopped || document.hidden) return;
      if (Date.now() >= qr.expires_at * 1000) {
        setQrState("expired");
        return;
      }
      const result = await controller.current.request<{
        state: string;
        account: Account | null;
      }>("qr-poll", "accounts/qr/poll", {
        qr_id: qr.qr_id,
        client_id: pollClient.current,
      });
      if (stopped) return;
      if (result) {
        setQrState(result.state);
        if (result.state === "ready") {
          setQr(null);
          controller.current.setNotice(
            "B 站账号已连接，视频可用画质会按该账号读取。",
          );
          void controller.current.refresh();
          return;
        }
        if (result.state === "expired") return;
      }
      timer = setTimeout(poll, result ? 5000 : 15_000);
    };
    const visible = () => {
      clearTimeout(timer);
      controller.current.cancel("qr-poll");
      if (!document.hidden) void poll();
    };
    void poll();
    document.addEventListener("visibilitychange", visible);
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.current.cancel("qr-poll");
      document.removeEventListener("visibilitychange", visible);
    };
  }, [qr, qrState]);
  const startQr = async () => {
    generation.current += 1;
    const mine = generation.current;
    setQr(null);
    setQrState("waiting");
    setReading("qr-start");
    media.clearError();
    const result = await media.request<QrSession>(
      "qr-start",
      "accounts/qr/start",
      { label: label.trim() },
    );
    if (mine !== generation.current) return;
    if (result) {
      pollClient.current = requestId();
      setQr(result);
    }
    setReading("");
  };
  const importAccount = async (event: FormEvent) => {
    event.preventDefault();
    const submitted = cookie.trim();
    setCookie("");
    const result = await media.command("accounts/import", {
      label: label.trim(),
      cookie: submitted,
    });
    if (result)
      media.setNotice("B 站会话已校验并保存，输入框中的会话内容已清空。");
  };
  const check = async (account: Account) => {
    generation.current += 1;
    const mine = generation.current;
    setReading(account.account_id);
    media.clearError();
    const result = await media.request("account", "accounts/check", {
      account_id: account.account_id,
    });
    if (mine !== generation.current) return;
    if (result) {
      media.setNotice("已重新校验 B 站会话，请查看账号最新状态。");
      void media.refresh();
    }
    setReading("");
  };
  const revoke = async (account: Account) => {
    const result = await media.command("accounts/revoke", {
      account_id: account.account_id,
      expected_revision: account.revision,
    });
    if (result)
      media.setNotice("B 站会话已撤销，受影响的订阅和任务会要求重新登录。");
  };
  const accounts = media.view?.accounts ?? [];
  const targets = media.view?.targets ?? [];
  return (
    <section className="media-stack" aria-label="账号与媒体库">
      <div className="media-section-head">
        <div>
          <h2>连接自己的 B 站账号</h2>
          <p className="muted">
            公开视频可不登录解析；登录后的画质取决于该账号对每条视频的实际访问权。
          </p>
        </div>
      </div>
      <div className="media-account-layout">
        <div className="media-card media-stack">
          <label>
            账号显示名称
            <input
              value={label}
              required
              maxLength={120}
              onChange={(event) => setLabel(event.target.value)}
            />
          </label>
          <div>
            <h3>扫码登录</h3>
            <p className="muted">用 B 站手机端扫码并确认登录。</p>
          </div>
          <button
            className="button primary"
            disabled={!label.trim() || !!reading || !!media.busy}
            onClick={() => void startQr()}
          >
            <QrCode aria-hidden="true" />
            {reading === "qr-start"
              ? "正在生成…"
              : qr
                ? "重新生成二维码"
                : "生成登录二维码"}
          </button>
          {qr && (
            <div className="media-qr">
              <StatusRail
                tone={qrState === "expired" ? "gray" : "yellow"}
                label={word(qrState)}
              />
              {qrState !== "expired" && qrImage ? (
                <img src={qrImage} alt="B 站登录二维码" />
              ) : qrState !== "expired" ? (
                <p role="alert">二维码无法生成，请重新获取。</p>
              ) : null}
              <p className="muted">有效至 {timestamp(qr.expires_at)}</p>
              {qrState === "expired" && <p>请重新生成二维码后扫码。</p>}
            </div>
          )}
          <details className="media-cookie">
            <summary>使用本人会话恢复登录</summary>
            <form
              className="media-stack"
              onSubmit={(event) => void importAccount(event)}
            >
              <p className="muted">
                在自己的 B 站已登录浏览器中取得 Cookie
                后导入。内容只用于这次提交，保存后不在页面显示。
              </p>
              <label>
                本人 B 站 Cookie
                <textarea
                  value={cookie}
                  rows={4}
                  required
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(event) => setCookie(event.target.value)}
                />
              </label>
              <button
                className="button"
                disabled={!cookie.trim() || !label.trim() || !!media.busy}
              >
                校验并导入会话
              </button>
            </form>
          </details>
        </div>
        <div className="media-stack">
          {!accounts.length ? (
            <StatePanel kind="empty" title="尚未连接 B 站账号">
              <p>可以扫码登录，也可以导入自己的现有会话。</p>
            </StatePanel>
          ) : (
            accounts.map((account) => (
              <article
                key={account.account_id}
                className="media-card media-stack"
              >
                <div className="media-section-head">
                  <h3>{account.label}</h3>
                  <StatusRail
                    tone={tone(account.state)}
                    label={word(account.state)}
                  />
                </div>
                <p className="muted">
                  最后校验 {timestamp(account.checked_at)}
                </p>
                {account.code && (
                  <p className="media-warning">{word(account.code)}</p>
                )}
                <div className="media-actions">
                  <button
                    className="button"
                    disabled={
                      !!reading || !!media.busy || account.state === "revoked"
                    }
                    onClick={() => void check(account)}
                  >
                    <RefreshCw aria-hidden="true" />
                    {reading === account.account_id ? "正在校验…" : "校验会话"}
                  </button>
                  <button
                    className="button"
                    disabled={!!media.busy || account.state === "revoked"}
                    onClick={() => void revoke(account)}
                  >
                    撤销会话
                  </button>
                </div>
              </article>
            ))
          )}
        </div>
      </div>
      <div className="media-section-head">
        <h2>已登记媒体库</h2>
      </div>
      {!targets.length ? (
        <StatePanel kind="unconfigured" title="尚未登记目标媒体库">
          <p>
            管理员登记 AssetLibrary 发布目标后，才可选择目标库创建下载和订阅。
          </p>
        </StatePanel>
      ) : (
        <div className="media-targets">
          {targets.map((target) => (
            <article className="media-card media-stack" key={target.target_id}>
              <div className="media-section-head">
                <h3>{target.label}</h3>
                <StatusRail
                  tone={tone(target.state)}
                  label={word(target.state)}
                />
              </div>
              <p className="muted">发布由资产库接收并校验成品。</p>
              <div>
                <h4>媒体服务器</h4>
                {target.servers.length ? (
                  target.servers.map((server) => (
                    <p key={server.server_id}>
                      {server.label} · {server.kind}
                    </p>
                  ))
                ) : (
                  <p className="muted">
                    未配置 Emby 或 Jellyfin。任务只标记发布完成。
                  </p>
                )}
              </div>
            </article>
          ))}
        </div>
      )}
    </section>
  );
}
