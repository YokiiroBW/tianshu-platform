// Offline browser regression: real built UI, non-secure HTTP origin, synthetic API.
const fs = require("node:fs");
const path = require("node:path");
const assert = require("node:assert/strict");
const { chromium } = require("@playwright/test");
const dist = path.resolve(__dirname, "../../apps/web/dist");

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const role = {
      id: "actor:synthetic",
      kind: "role",
      name: "actor:synthetic",
      description: "",
      version: 1,
      state: "published",
      published_revision: "r1",
      draft_revision: null,
      content: {
        persona: "Existing role baseline",
        tone: "",
        style: "",
        address: "",
      },
    };
    const before = JSON.stringify(role);
    let profile = null;
    let lost = false;
    const creates = [];
    const ledger = new Map();
    await page.route("**/*", async (route) => {
      const url = new URL(route.request().url());
      assert.equal(url.hostname, "persona-test.invalid");
      if (url.pathname.startsWith("/api/")) {
        let result = {};
        const body =
          route.request().method() === "POST"
            ? route.request().postDataJSON()
            : {};
        if (url.pathname === "/api/web/session")
          result = {
            authenticated: true,
            csrf: "synthetic-csrf",
            username: "synthetic",
          };
        else if (url.pathname.endsWith("/personas/profiles"))
          result = {
            profiles: profile ? [profile] : [],
            targets: [role],
            permissions: { create: true, edit: true, apply: true },
          };
        else if (url.pathname.endsWith("/personas/view"))
          result = { item: body.id === role.id ? role : profile };
        else if (url.pathname.endsWith("/personas/create")) {
          creates.push(body);
          if (!ledger.has(body.client_id)) {
            profile = {
              ...role,
              ...body,
              id: "persona-profile:" + "a".repeat(32),
              kind: "profile",
              published_revision: null,
              draft_revision: "r2",
            };
            ledger.set(body.client_id, { item: profile });
          }
          result = ledger.get(body.client_id);
          if (!lost) {
            lost = true;
            return route.abort("connectionreset");
          }
        } else if (url.pathname.endsWith("/personas/save")) {
          assert.equal(body.expected, profile.version);
          profile = { ...profile, ...body, version: profile.version + 1 };
          result = { item: profile };
        }
        return route.fulfill({
          contentType: "application/json",
          body: JSON.stringify(result),
        });
      }
      const file = path.join(
        dist,
        url.pathname === "/" ? "index.html" : url.pathname,
      );
      assert(file.startsWith(dist));
      if (!fs.existsSync(file)) return route.fulfill({ status: 404, body: "" });
      const contentType =
        { ".js": "text/javascript", ".css": "text/css", ".html": "text/html" }[
          path.extname(file)
        ] || "application/octet-stream";
      return route.fulfill({ contentType, body: fs.readFileSync(file) });
    });
    await page.goto("http://persona-test.invalid/#/companion/2");
    assert.equal(await page.evaluate(() => isSecureContext), false);
    assert.equal(
      await page.evaluate(() => typeof crypto.randomUUID),
      "undefined",
    );
    await page.getByRole("button", { name: "新建人格", exact: true }).click();
    const text = "# 合成人格\n温和、清楚地回答。\n".repeat(240);
    await page.getByLabel("名称", { exact: true }).fill("合成测试");
    await page.locator(".persona-main-text").fill(text);
    await page.getByRole("button", { name: "保存草稿", exact: true }).click();
    await page.getByText("请求中断，请重新连接。", { exact: true }).waitFor();
    assert.equal(await page.locator(".persona-main-text").inputValue(), text);
    await page.getByRole("button", { name: "保存草稿", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "草稿已保存" }).waitFor();
    assert.equal(creates.length, 2);
    assert.deepEqual(creates[0], creates[1]);
    assert.equal(ledger.size, 1);
    assert.match(
      creates[0].client_id,
      /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/,
    );
    await page.reload();
    await page.getByRole("button", { name: "编辑", exact: true }).click();
    assert.equal(await page.locator(".persona-main-text").inputValue(), text);
    await page.locator(".persona-main-text").fill(text + "\nUpdated");
    await page.getByRole("button", { name: "保存草稿", exact: true }).click();
    await page.getByRole("status").filter({ hasText: "草稿已保存" }).waitFor();
    assert.equal(profile.content.persona, text + "\nUpdated");
    assert.equal(JSON.stringify(role), before);
    console.log(
      JSON.stringify({
        status: "http_persona_create_save_reopen_retry_verified",
        bytes: Buffer.byteLength(text),
        creates: 1,
        retry_same_id: true,
        existing_role_unchanged: true,
        real_backend: false,
        production_requests: 0,
      }),
    );
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
