import { useEffect, useRef, useState } from "react";
import { Brain, RefreshCw, LockKeyhole } from "lucide-react";
import { useMemoryReadScope } from "./useMemoryReadScope";
import { integrationPost, readFailure } from "../../app/integrationApi";
import {
  type Person,
  type Role,
  type Records,
  type RecordGroup,
  dateText,
} from "./types";
const categories: Record<string, string> = {
  profile: "人物画像",
  preference: "偏好",
  preferences: "偏好",
  fact: "事实",
  identity: "身份",
  relationship: "关系",
  interest: "兴趣",
  habit: "习惯",
};
export function SharedPortrait({
  person,
  role,
  available,
  csrf,
}: {
  person: Person;
  role: Role | null;
  available: boolean;
  csrf: string;
}) {
  const [records, setRecords] = useState<RecordGroup[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [verifiedAt, setVerifiedAt] = useState("");
  const [scopeVersion, setScopeVersion] = useState<number | null>(null);
  const active = useRef<AbortController | null>(null);
  const readable = Boolean(
    available && role?.available && person.profile?.person_id,
  );
  async function read(after: string | null = null) {
    if (!readable || !role) return;
    active.current?.abort();
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    if (!after) {
      setRecords([]);
      setCursor(null);
      setVerifiedAt("");
    }
    try {
      const result = await integrationPost<Records>(
        "memory/records",
        {
          role_id: role.id,
          role_version: role.version,
          subject: { kind: "person", person_id: person.profile!.person_id },
          limit: 20,
          cursor: after,
        },
        csrf,
        controller.signal,
      );
      if (controller.signal.aborted) return;
      setRecords((before) =>
        after ? [...before, ...result.items] : result.items,
      );
      setCursor(result.next_cursor);
      setVerifiedAt(result.verified_at);
      setScopeVersion(result.scope_version);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setRecords([]);
        setCursor(null);
        setVerifiedAt("");
        setError(readFailure(cause));
      }
    } finally {
      if (!controller.signal.aborted) setBusy(false);
    }
  }
  function suspend() {
    active.current?.abort();
    setRecords([]);
    setCursor(null);
    setVerifiedAt("");
    setScopeVersion(null);
    setBusy(false);
  }
  useMemoryReadScope({
    csrf,
    role,
    enabled: readable,
    scopeVersion,
    onSuspend: suspend,
    onResume: () => void read(),
    onFailure: (cause) => {
      suspend();
      setError(readFailure(cause));
    },
  });
  useEffect(() => {
    void read();
    return () => active.current?.abort();
  }, [csrf, role?.id, role?.version, person.qqId, readable]);
  return (
    <section aria-busy={busy}>
      <div className="people-content-heading">
        <div>
          <h3>
            <Brain size={19} aria-hidden="true" />
            角色可读的共享画像
          </h3>
          <p>这里是关于这个人的共享事实，不是该用户的私有长期记忆。</p>
        </div>
        {readable && (
          <button
            className="button"
            disabled={busy}
            onClick={() => void read()}
          >
            <RefreshCw size={15} aria-hidden="true" />
            刷新画像
          </button>
        )}
      </div>
      {!readable && (
        <p className="people-empty">
          {!person.profile
            ? "人物身份尚未确认；收到消息不代表已经建立画像。"
            : !role
              ? "请选择查看角色。"
              : !available || !role.available
                ? "当前角色的记忆读取尚不可用。"
                : "共享画像尚不可读取。"}
        </p>
      )}
      {error && (
        <p className="people-warning" role="alert">
          {error}
        </p>
      )}
      {busy && records.length === 0 && (
        <p role="status" className="people-empty">
          正在读取共享画像…
        </p>
      )}
      {readable && !busy && !error && verifiedAt && records.length === 0 && (
        <div className="people-empty">
          <Brain aria-hidden="true" />
          <h4>还没有形成共享画像</h4>
          <p>这个人已在用户目录中。简单的问候可能不会形成可保存的事实。</p>
        </div>
      )}
      <div className="people-memory-records">
        {records.map((group) => (
          <article key={group.semantic_group_id} className="people-info-card">
            <span className="people-chip">
              {categories[group.category] || group.category}
            </span>
            {group.units.map((unit) => (
              <div className="people-memory-unit" key={unit.record_id}>
                <p>{unit.statement}</p>
                {[
                  unit.conditions,
                  unit.negations,
                  unit.valid_time,
                  unit.uncertainty,
                  unit.reality,
                ].some(
                  (value) =>
                    value != null &&
                    JSON.stringify(value) !== "{}" &&
                    JSON.stringify(value) !== "[]",
                ) && (
                  <details>
                    <summary>查看事实的适用条件</summary>
                    <dl>
                      {[
                        ["条件", unit.conditions],
                        ["否定", unit.negations],
                        ["有效时间", unit.valid_time],
                        ["不确定性", unit.uncertainty],
                        ["现实依据", unit.reality],
                      ]
                        .filter(([, value]) => value != null)
                        .map(([name, value]) => (
                          <div key={String(name)}>
                            <dt>{String(name)}</dt>
                            <dd>
                              {typeof value === "string"
                                ? value
                                : JSON.stringify(value)}
                            </dd>
                          </div>
                        ))}
                    </dl>
                  </details>
                )}
              </div>
            ))}
          </article>
        ))}
      </div>
      {cursor && (
        <button
          className="button"
          disabled={busy}
          onClick={() => void read(cursor)}
        >
          加载更多画像记录
        </button>
      )}
      {verifiedAt && (
        <p className="people-footnote">核验于 {dateText(verifiedAt)}</p>
      )}
      <div className="people-private-note">
        <LockKeyhole size={17} aria-hidden="true" />
        <span>
          对方的私有记忆不在此页面开放。你的本人记忆可从“本人记忆”查看。
        </span>
      </div>
    </section>
  );
}
