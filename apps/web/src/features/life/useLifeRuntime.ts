import { requestId } from "../../app/requestId";
import { useEffect, useRef, useState } from "react";
import {
  IntegrationError,
  readFailure,
  writeFailure,
} from "../../app/integrationApi";
import { manageLife, readLife, type LifeAccess } from "./runtimeApi";
import type { manage_response, Records, Resource } from "./runtimeTypes";

export function useLifeResource<K extends keyof Records & Resource>(
  access: LifeAccess,
  resource: K,
  objectId: string | null = null,
  chapterView?: "current" | "published",
) {
  const [items, setItems] = useState<Records[K][]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [epoch, setEpoch] = useState(0);
  const current = useRef<AbortController | null>(null);
  const identity = useRef("");
  function failed(cause: unknown) {
    if (
      cause instanceof IntegrationError &&
      [
        "forbidden",
        "unauthorized",
        "session_expired",
        "scope_changed",
        "stale_evidence",
        "not_found",
        "upstream_forbidden",
        "operator_not_authorized",
        "life_read_required",
      ].includes(cause.code)
    ) {
      setItems([]);
      setCursor(null);
    }
    setError(readFailure(cause));
  }
  useEffect(() => {
    const control = new AbortController();
    current.current?.abort();
    current.current = control;
    const key = JSON.stringify([
      access.actor,
      access.csrf,
      resource,
      objectId,
      chapterView,
    ]);
    if (identity.current !== key) {
      setItems([]);
      setCursor(null);
      identity.current = key;
    }
    setError("");
    if (!access.actor || !access.csrf) return;
    setLoading(true);
    void readLife(access, resource, control.signal, objectId, null, chapterView)
      .then((result) => {
        if (!control.signal.aborted) {
          setItems(result.items);
          setCursor(result.next_cursor);
        }
      })
      .catch((cause) => {
        if (!control.signal.aborted) {
          failed(cause);
        }
      })
      .finally(() => {
        if (!control.signal.aborted) setLoading(false);
      });
    return () => control.abort();
  }, [access.actor, access.csrf, resource, objectId, chapterView, epoch]);
  async function more() {
    if (!cursor || loading) return;
    const control = current.current ?? new AbortController();
    setLoading(true);
    setError("");
    try {
      const answer = await readLife(
        access,
        resource,
        control.signal,
        objectId,
        cursor,
        chapterView,
      );
      if (!control.signal.aborted) {
        setItems((old) => [...old, ...answer.items]);
        setCursor(answer.next_cursor);
      }
    } catch (cause) {
      if (!control.signal.aborted) failed(cause);
    } finally {
      if (!control.signal.aborted) setLoading(false);
    }
  }
  const visible =
    identity.current ===
    JSON.stringify([
      access.actor,
      access.csrf,
      resource,
      objectId,
      chapterView,
    ]);
  return {
    items: visible ? items : [],
    cursor,
    loading,
    error,
    refresh: () => setEpoch((old) => old + 1),
    more,
  };
}

export function useLifeAction(access: LifeAccess, updated: () => void) {
  const [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [receipt, setReceipt] = useState<manage_response | null>(null);
  const active = useRef<AbortController | null>(null);
  const pending = useRef<{ semantic: string; id: string } | null>(null);
  useEffect(() => {
    active.current?.abort();
    pending.current = null;
    setReceipt(null);
    setError("");
    setBusy(false);
    return () => active.current?.abort();
  }, [access.actor, access.csrf]);
  async function run(
    operation: string,
    value: object,
    version: number | null = null,
  ) {
    if (active.current && !active.current.signal.aborted) return null;
    const semantic = JSON.stringify([operation, value, version]);
    const id =
      pending.current?.semantic === semantic ? pending.current.id : requestId();
    pending.current = { semantic, id };
    const control = new AbortController();
    active.current = control;
    setBusy(true);
    setError("");
    setReceipt(null);
    try {
      const answer = await manageLife(
        access,
        operation,
        value,
        version,
        id,
        control.signal,
      );
      if (control.signal.aborted) return null;
      if (answer.result.state !== "unknown") pending.current = null;
      setReceipt(answer);
      updated();
      return answer;
    } catch (cause) {
      if (!control.signal.aborted) setError(writeFailure(cause));
      return null;
    } finally {
      if (active.current === control) active.current = null;
      if (!control.signal.aborted) setBusy(false);
    }
  }
  return { busy, error, receipt, run };
}

// Keep object identity through an uncertain response; only a confirmed save starts a new draft.
export function useDraftId(prefix: string) {
  const id = useRef(`${prefix}:${requestId()}`);
  return {
    id: id.current,
    reset: () => {
      id.current = `${prefix}:${requestId()}`;
    },
  };
}
