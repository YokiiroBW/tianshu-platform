import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";

const source = stripTypeScriptTypes(
  readFileSync(
    new URL("../src/app/sessionTransport.ts", import.meta.url),
    "utf8",
  ),
);

test("session transport leaves successful bodies unread and still revalidates lost sessions", async ({
  page,
}) => {
  await page.goto("about:blank");
  const result = await page.evaluate(async (source) => {
    const { webFetch } = await import(
      URL.createObjectURL(new Blob([source], { type: "text/javascript" }))
    );
    const originalFetch = window.fetch;
    let clones = 0;
    let lost = 0;
    const listener = () => lost++;
    window.addEventListener("tianshu:session-lost", listener);
    const responses = [
      new Response(JSON.stringify({ items: ["original body"] }), {
        headers: { "content-type": "application/json" },
      }),
      new Response(JSON.stringify({ code: "session_expired" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      }),
      new Response(JSON.stringify({ authenticated: false }), {
        headers: { "content-type": "application/json" },
      }),
      new Response(JSON.stringify({ code: "unauthorized" }), {
        status: 401,
        headers: { "content-type": "application/json" },
      }),
    ];
    window.fetch = async () => {
      const response = responses.shift()!;
      const clone = response.clone.bind(response);
      response.clone = () => {
        clones++;
        return clone();
      };
      return response;
    };
    try {
      const success = await webFetch("/api/web/memory/query");
      const ordinaryClones = clones;
      const unread = !success.bodyUsed;
      const body = await success.json();
      await webFetch("/api/web/life/read");
      await webFetch("/api/web/session");
      await webFetch("/api/web/life/read", { signal: AbortSignal.abort() });
      return { ordinaryClones, unread, body, lost, clones };
    } finally {
      window.fetch = originalFetch;
      window.removeEventListener("tianshu:session-lost", listener);
    }
  }, source);
  expect(result).toEqual({
    ordinaryClones: 0,
    unread: true,
    body: { items: ["original body"] },
    lost: 2,
    clones: 2,
  });
});
