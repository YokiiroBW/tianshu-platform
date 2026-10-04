import { requestId } from "../../app/requestId";
import { useEffect, useRef, useState } from "react";
import {
  integrationPost,
  readFailure,
  writeFailure,
} from "../../app/integrationApi";
import { useDraftId, useLifeAction, useLifeResource } from "./useLifeRuntime";
import { ContentAcquire } from "./ContentAcquire";
import { ContentViewer } from "./ContentViewer";
import { ResourceFeedback, RuntimeFeedback } from "./RuntimeFeedback";
import { originalImage, showState, type LifeAccess } from "./runtimeApi";
import type { content_ref, outfits_record } from "./runtimeTypes";

export function PrivateImage({
  access,
  mediaId,
  caption,
}: {
  access: LifeAccess;
  mediaId: string;
  caption: string;
}) {
  const [url, setUrl] = useState(""),
    [error, setError] = useState(""),
    [open, setOpen] = useState(false),
    [retry, setRetry] = useState(0),
    [extension, setExtension] = useState("png");
  useEffect(() => {
    setUrl("");
    setError("");
    if (!open) return;
    const control = new AbortController();
    let objectUrl = "";
    void originalImage(access, mediaId, control.signal)
      .then((blob) => {
        if (control.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setUrl(objectUrl);
        setExtension(
          blob.type === "image/jpeg"
            ? "jpg"
            : blob.type === "image/webp"
              ? "webp"
              : "png",
        );
      })
      .catch((cause) => {
        if (!control.signal.aborted) setError(readFailure(cause));
      });
    return () => {
      control.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [access.actor, access.csrf, mediaId, open, retry]);
  return (
    <div className="life-private-image">
      {!open ? (
        <button className="button" onClick={() => setOpen(true)}>
          打开原图
        </button>
      ) : url ? (
        <>
          <a href={url} target="_blank" rel="noreferrer">
            <img src={url} alt={caption} />
          </a>
          <a className="button" href={url} download={`原图.${extension}`}>
            保存原图
          </a>
        </>
      ) : error ? (
        <>
          <p className="error-text" role="alert">
            {error}
          </p>
          <button className="button" onClick={() => setRetry((x) => x + 1)}>
            重试读取原图
          </button>
        </>
      ) : (
        <p role="status">正在取得经授权的原图…</p>
      )}
    </div>
  );
}

export function WardrobePanel({ access }: { access: LifeAccess }) {
  const outfits = useLifeResource(access, "outfits"),
    state = useLifeResource(access, "state");
  const action = useLifeAction(access, () => {
    outfits.refresh();
    state.refresh();
  });
  const draft = useDraftId("outfit");
  const [editing, setEditing] = useState<outfits_record | null>(null),
    [description, setDescription] = useState(""),
    [prompt, setPrompt] = useState("");
  const [reference, setReference] = useState<outfits_record["reference"]>(null);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    const receipt = await action.run(
      "outfit.put",
      {
        id: editing?.id || draft.id,
        description,
        prompt,
        reference,
        activities: editing?.activities ?? [],
        ...(reference && typeof reference === "object"
          ? { source_scope: {}, query: {} }
          : {}),
      },
      editing?.version ?? 0,
    );
    if (receipt && receipt.result.state !== "unknown") {
      draft.reset();
      setEditing(null);
      setDescription("");
      setPrompt("");
      setReference(null);
    }
  }
  const current = state.items[0]?.outfit_ref;
  return (
    <>
      <section className="panel">
        <h3>衣柜与当前穿搭</h3>
        <ResourceFeedback {...outfits} empty={!outfits.items.length} />
        <div className="life-records">
          {outfits.items.map((row) => (
            <article className="life-record" key={row.id}>
              <header>
                <strong>{row.description}</strong>
                {current === row.id && (
                  <span className="status-pill">当前穿搭</span>
                )}
              </header>
              <p>{row.prompt}</p>
              <div className="life-action-row">
                <button
                  className="button"
                  onClick={() => {
                    setEditing(row);
                    setDescription(row.description);
                    setPrompt(row.prompt);
                    setReference(row.reference);
                  }}
                >
                  编辑服装
                </button>
                <button
                  className="button"
                  disabled={action.busy || current === row.id}
                  onClick={() =>
                    void action.run(
                      "outfit.select",
                      { id: row.id },
                      state.items[0]?.actor_version ?? null,
                    )
                  }
                >
                  设为当前穿搭
                </button>
              </div>
            </article>
          ))}
        </div>
        {outfits.cursor && (
          <button className="button" onClick={() => void outfits.more()}>
            继续读取衣柜
          </button>
        )}
        <h4>{editing ? "编辑服装" : "添加服装"}</h4>
        <ContentAcquire
          key={editing?.id ?? draft.id}
          access={access}
          imagesOnly
          onAcquired={(ref) => {
            if (ref.kind === "image") setReference({ ...ref, kind: "image" });
          }}
        />
        {reference && typeof reference === "object" && (
          <ContentViewer
            key={reference.object_id + reference.version}
            access={access}
            contentRef={reference}
          />
        )}
        {reference && (
          <button className="button" onClick={() => setReference(null)}>
            移除此服装参考
          </button>
        )}
        <form className="life-form" onSubmit={save}>
          <label>
            服装描述
            <input
              required
              maxLength={1000}
              value={description}
              onChange={(e) => setDescription(e.target.value)}
            />
          </label>
          <label>
            外观细节
            <textarea
              required
              maxLength={4000}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
            />
          </label>
          <button className="button primary" disabled={action.busy}>
            保存服装
          </button>
          {editing && (
            <button
              className="button"
              type="button"
              onClick={() => {
                setEditing(null);
                setDescription("");
                setPrompt("");
                setReference(null);
              }}
            >
              取消编辑
            </button>
          )}
        </form>
        <RuntimeFeedback {...action} />
      </section>
      <IdentityReference access={access} />
      <ImageStudio access={access} outfits={outfits.items} />
    </>
  );
}

function IdentityReference({ access }: { access: LifeAccess }) {
  const records = useLifeResource(access, "image_reference"),
    action = useLifeAction(access, records.refresh);
  const row = records.items[0];
  const [ref, setRef] = useState<content_ref | null>(null);
  return (
    <section className="panel">
      <h3>角色形象参考</h3>
      <ResourceFeedback {...records} empty={!records.items.length} />
      <p>
        当前参考：
        {row?.content_ref
          ? `已配置 · 原件版本 ${row.content_ref.version}`
          : "尚未配置"}
      </p>
      <ContentAcquire access={access} imagesOnly onAcquired={setRef} />
      {(ref ?? row?.content_ref) && (
        <ContentViewer
          key={`${(ref ?? row?.content_ref)?.object_id}:${(ref ?? row?.content_ref)?.version}`}
          access={access}
          contentRef={(ref ?? row?.content_ref)!}
        />
      )}
      <div className="life-action-row">
        <button
          className="button primary"
          disabled={action.busy || !ref || !row}
          onClick={async () => {
            const answer = await action.run(
              "actor.image-reference.configure",
              { content_ref: ref, scope: {}, query: {} },
              row?.version ?? 0,
            );
            if (answer && answer.result.state !== "unknown") setRef(null);
          }}
        >
          设为角色形象参考
        </button>
        <button
          className="button"
          disabled={action.busy || !row?.content_ref}
          onClick={() =>
            void action.run(
              "actor.image-reference.configure",
              { content_ref: null, scope: null, query: null },
              row?.version ?? 0,
            )
          }
        >
          清除形象参考
        </button>
      </div>
      <RuntimeFeedback {...action} />
    </section>
  );
}

function ImageStudio({
  access,
  outfits,
}: {
  access: LifeAccess;
  outfits: outfits_record[];
}) {
  const album = useLifeResource(access, "album"),
    jobs = useLifeResource(access, "image_jobs"),
    backend = useLifeResource(access, "image_backend");
  const action = useLifeAction(access, () => {
    jobs.refresh();
    album.refresh();
  });
  const draft = useDraftId("image");
  const albumDrafts = useRef(new Map<string, string>());
  const [scene, setScene] = useState(""),
    [outfit, setOutfit] = useState(""),
    [reference, setReference] = useState("");
  const [width, setWidth] = useState(768),
    [height, setHeight] = useState(1024),
    [steps, setSteps] = useState(20),
    [negative, setNegative] = useState("");
  useEffect(() => {
    if (!jobs.items.some((row) => ["queued", "running"].includes(row.state)))
      return;
    const timer = window.setInterval(() => {
      if (!document.hidden) jobs.refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [jobs.items]);
  async function generate(event: React.FormEvent) {
    event.preventDefault();
    const source = album.items.find((x) => x.id === reference);
    const answer = await action.run("image.request", {
      id: draft.id,
      activity_id: null,
      outfit_id: outfit || null,
      scene: scene || null,
      edit_source_id: null,
      scope: source?.scope ?? null,
      ...(source ? { edit_source_ref: source.content_ref, query: {} } : {}),
      parameters: { width, height, steps, negative },
    });
    if (answer && answer.result.state !== "unknown") draft.reset();
  }
  return (
    <div className="life-runtime-grid">
      <section className="panel">
        <h3>图像创作</h3>
        <p className="muted">
          此次服装和参考原图用于本次创作，当前穿搭仍由衣柜单独选择。
        </p>
        <ResourceFeedback {...backend} empty={!backend.items.length} />
        <p>
          图像后端：{showState(backend.items[0]?.state ?? "not_configured")}
        </p>
        <form className="life-form" onSubmit={generate}>
          <label>
            场景
            <textarea
              value={scene}
              onChange={(e) => setScene(e.target.value)}
              maxLength={2000}
            />
          </label>
          <label>
            此次服装
            <select value={outfit} onChange={(e) => setOutfit(e.target.value)}>
              <option value="">沿用当前穿搭</option>
              {outfits.map((row) => (
                <option key={row.id} value={row.id}>
                  {row.description}
                </option>
              ))}
            </select>
          </label>
          <label>
            形象参考或原图续作
            <select
              value={reference}
              onChange={(e) => setReference(e.target.value)}
            >
              <option value="">新图创作</option>
              {album.items
                .filter((row) => row.state === "available")
                .map((row) => (
                  <option key={row.id} value={row.id}>
                    {row.caption}
                  </option>
                ))}
            </select>
          </label>
          <div className="life-form-row">
            <label>
              宽度
              <input
                type="number"
                min={256}
                max={2048}
                step={64}
                value={width}
                onChange={(e) => setWidth(Number(e.target.value))}
              />
            </label>
            <label>
              高度
              <input
                type="number"
                min={256}
                max={2048}
                step={64}
                value={height}
                onChange={(e) => setHeight(Number(e.target.value))}
              />
            </label>
            <label>
              步数
              <input
                type="number"
                min={1}
                max={100}
                value={steps}
                onChange={(e) => setSteps(Number(e.target.value))}
              />
            </label>
          </div>
          <label>
            排除细节
            <input
              value={negative}
              onChange={(e) => setNegative(e.target.value)}
              maxLength={4000}
            />
          </label>
          <button className="button primary" disabled={action.busy}>
            提交{reference ? "原图续作" : "创作"}
          </button>
        </form>
        <RuntimeFeedback {...action} />
      </section>
      <section className="panel">
        <h3>原任务</h3>
        <button className="button" onClick={jobs.refresh}>
          查询原任务
        </button>
        <ResourceFeedback {...jobs} empty={!jobs.items.length} />
        {jobs.items.map((job) => (
          <article key={job.id} className="life-record">
            <header>
              <strong>
                创作 ·{" "}
                {job.created_at
                  ? new Date(job.created_at * 1000).toLocaleString("zh-CN")
                  : "时间未记录"}
              </strong>
              <span className="status-pill">{showState(job.state)}</span>
            </header>
            {job.error_code && <p className="error-text">{job.error_code}</p>}
            {["queued", "running", "unknown"].includes(job.state) && (
              <button
                className="button"
                disabled={action.busy}
                onClick={() =>
                  void action.run("image.cancel", { id: job.id }, job.version)
                }
              >
                取消原任务
              </button>
            )}
            {job.artifacts.map((item, index) => (
              <div key={item.media_id}>
                <PrivateImage
                  access={access}
                  mediaId={item.media_id}
                  caption="本次创作原图"
                />
                <button
                  className="button"
                  disabled={
                    action.busy ||
                    album.items.some(
                      (row) =>
                        row.media_id === item.media_id &&
                        row.state === "available",
                    )
                  }
                  onClick={() => {
                    const key = `${job.id}:${index}`;
                    if (!albumDrafts.current.has(key))
                      albumDrafts.current.set(key, `album:${requestId()}`);
                    void action.run(
                      "album.attach",
                      {
                        id: albumDrafts.current.get(key),
                        job_id: job.id,
                        artifact_index: index,
                        activity_id: null,
                        caption: "创作原图",
                        scope: null,
                      },
                      0,
                    );
                  }}
                >
                  收进相册
                </button>
              </div>
            ))}
          </article>
        ))}
      </section>
      <section className="panel">
        <h3>持久相册</h3>
        <ResourceFeedback {...album} empty={!album.items.length} />
        {album.items.map((row) => (
          <article className="life-record" key={row.id}>
            <h4>{row.caption}</h4>
            <p>
              {showState(row.state)} · 版本 {row.version}
            </p>
            {row.state === "available" && (
              <PrivateImage
                access={access}
                mediaId={row.media_id}
                caption={row.caption}
              />
            )}
            <div className="life-action-row">
              <button
                className="button"
                disabled={row.state !== "available"}
                onClick={() => setReference(row.id)}
              >
                用于原图续作
              </button>
              <button
                className="button"
                disabled={action.busy}
                onClick={() =>
                  void action.run(
                    "album.remove",
                    { id: row.id, reason: "用户移除相册记录" },
                    row.version,
                  )
                }
              >
                移出相册
              </button>
            </div>
          </article>
        ))}
        {album.cursor && (
          <button className="button" onClick={() => void album.more()}>
            继续读取相册
          </button>
        )}
      </section>
      <ImageBackend
        access={access}
        backend={backend.items[0] ?? null}
        refreshed={backend.refresh}
      />
    </div>
  );
}

function ImageBackend({
  access,
  backend,
  refreshed,
}: {
  access: LifeAccess;
  backend: import("./runtimeTypes").image_backend_record | null;
  refreshed: () => void;
}) {
  const [url, setUrl] = useState(""),
    [checkpoint, setCheckpoint] = useState(""),
    [enabled, setEnabled] = useState(true),
    [token, setToken] = useState("");
  const [status, setStatus] = useState<{
      revision: number | null;
      credential_configured: boolean;
    } | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState("");
  const current = useRef<AbortController | null>(null),
    pending = useRef<{ semantic: string; id: string } | null>(null);
  useEffect(() => () => current.current?.abort(), []);
  useEffect(() => {
    setUrl(backend?.base_url ?? "");
    setCheckpoint(backend?.checkpoint ?? "");
    setEnabled(backend?.state !== "disabled");
  }, [backend]);
  useEffect(() => {
    const control = new AbortController();
    setToken("");
    setStatus(null);
    void integrationPost<typeof status>(
      "life/image-backend/status",
      { actor_id: access.actor },
      access.csrf,
      control.signal,
    )
      .then(setStatus)
      .catch((cause) => {
        if (!control.signal.aborted) setError(readFailure(cause));
      });
    return () => control.abort();
  }, [access.actor, access.csrf]);
  async function save(event: React.FormEvent) {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError("");
    const control = new AbortController();
    current.current = control;
    const body = {
      actor_id: access.actor,
      value: { base_url: url, profile: "standard_sd", checkpoint, enabled },
      credential: token
        ? { action: "replace", value: token }
        : { action: "keep" },
      catalog_revision: status?.revision ?? 0,
      expected_version: backend?.version ?? 0,
    };
    const semantic = JSON.stringify(body);
    if (pending.current?.semantic !== semantic)
      pending.current = { semantic, id: requestId() };
    try {
      await integrationPost(
        "life/image-backend/save",
        { ...body, client_id: pending.current.id },
        access.csrf,
        control.signal,
      );
      if (control.signal.aborted) return;
      pending.current = null;
      setToken("");
      setStatus(
        await integrationPost<typeof status>(
          "life/image-backend/status",
          { actor_id: access.actor },
          access.csrf,
          control.signal,
        ),
      );
      refreshed();
    } catch (cause) {
      if (!control.signal.aborted) setError(writeFailure(cause));
    } finally {
      if (!control.signal.aborted) setBusy(false);
    }
  }
  return (
    <section className="panel">
      <h3>图像后端配置</h3>
      <p className="muted">
        使用现有 ComfyUI
        的标准工作流。保存后查询实际模型列表；未安装服务时保持未配置状态。
      </p>
      <form className="life-form" onSubmit={save}>
        <label>
          后端地址
          <input
            type="url"
            required
            value={url}
            onChange={(e) => setUrl(e.target.value)}
            placeholder="http://服务器:8188"
          />
        </label>
        <label>
          模型检查点
          <input
            required
            list="image-checkpoints"
            maxLength={128}
            value={checkpoint}
            onChange={(e) => setCheckpoint(e.target.value)}
          />
        </label>
        <datalist id="image-checkpoints">
          {backend?.models.map((model) => (
            <option key={model} value={model} />
          ))}
        </datalist>
        <label>
          访问凭据
          <input
            type="password"
            autoComplete="new-password"
            value={token}
            onChange={(e) => setToken(e.target.value)}
            placeholder={
              status?.credential_configured
                ? "已保存，留空保留"
                : "无认证的后端可留空"
            }
          />
        </label>
        <label className="life-check">
          <input
            type="checkbox"
            checked={enabled}
            onChange={(e) => setEnabled(e.target.checked)}
          />
          启用此后端
        </label>
        <button className="button primary" disabled={busy}>
          保存并应用配置
        </button>
        <button className="button" type="button" onClick={refreshed}>
          查询实际后端
        </button>
      </form>
      {error && (
        <p role="alert" className="error-text">
          {error}
        </p>
      )}
      {backend?.error_code && (
        <p className="error-text">后端查询：{backend.error_code}</p>
      )}
    </section>
  );
}
