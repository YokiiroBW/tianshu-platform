import { randomUUID } from "node:crypto";
const now = () => Date.now() / 1000;
const names = ["澄汐", "林栖"];
const providers = [
  {
    id: "provider:calm",
    name: "日常模型",
    model: "Chat model",
    revision: 1,
    available: true,
  },
  {
    id: "provider:story",
    name: "创作模型",
    model: "Writing model",
    revision: 1,
    available: true,
  },
];
const functions = [
  ["chat", "主对话", "日常交流与角色表达"],
  ["writing", "日常与日记", "生活、日记与写作"],
  ["memory", "记忆整理", "记忆提取与回顾"],
  ["participation", "群聊参与", "兴趣判断与普通群聊参与"],
  ["search", "网页搜索", "搜索、阅读与来源整理"],
  ["tools", "工具处理", "工具选择与结果理解"],
  ["code", "代码", "代码分析与编写"],
];
const roles = names.map((name, index) => ({
  actor_id: `actor:preview-${index}`,
  name,
  version: 1,
  profile_id: null,
  profile_version: null,
  profile_revision: null,
  provider_id: null,
  provider_revision: null,
  enabled: true,
  capabilities: ["dialogue", "memory.read", "memory.write"],
  state: "active",
  stage: "complete",
  error_code: null,
  client_id: randomUUID(),
  legacy: false,
  model_version: 0,
  model_functions: {},
}));
const turns = new Map();
const servers = [
  {
    id: "onebot:preview-a",
    name: "日常机器人",
    token: "synthetic-preview-token-only",
    account_id: "100001",
    connected: true,
    observation_id: null,
    observation_state: null,
    ws_path: "/onebot/v11/ws/preview-a",
  },
  {
    id: "onebot:preview-b",
    name: "新的机器人",
    token: "synthetic-preview-token-only",
    account_id: null,
    connected: false,
    observation_id: null,
    observation_state: null,
    ws_path: "/onebot/v11/ws/preview-b",
  },
];
function roleModels(actor) {
  const role = roles.find((r) => r.actor_id === actor);
  return {
    actor_id: actor,
    name: role?.name,
    version: role?.model_version ?? 0,
    providers,
    functions: functions.map(([function_id, name, description]) => ({
      function_id,
      name,
      description,
      connected: !["code", "tools", "search"].includes(function_id),
      provider_id: role?.model_functions[function_id] ?? null,
      provider_revision: role?.model_functions[function_id] ? 1 : null,
      effective_provider_id:
        role?.model_functions[function_id] ?? "provider:calm",
      configured: true,
      source: role?.model_functions[function_id]
        ? "role_function"
        : "system_default",
    })),
  };
}
const preview = {
  available: false,
  code: "not_supported",
  reason: "本地排版封面预览",
};
const library = {
  library_id: "library:notes",
  display_name: "阅读与资料",
  availability: "online",
  access_level: "read",
  category: "documents",
  index_available: true,
  original_available: "unverified",
  preview,
};
const entryNames = [
  "设计的日常.pdf",
  "旅行手记.epub",
  "角色与故事.md",
  "城市的声音.pdf",
  "阅读摘录.txt",
  "配色与光影.pdf",
];
const entries = entryNames.map((name, index) => ({
  entry_id: `entry:${index}`,
  library_id: library.library_id,
  relative_path: name,
  name,
  kind: "file",
  directory: false,
  content_length: String(1024 * (index + 1)),
  last_write_time_utc: new Date().toISOString(),
  original_available: "unverified",
  preview,
}));
const page = {
  size: 20,
  returned: entries.length,
  has_more: false,
  next_cursor: null,
};
function answer(path, b) {
  const actor = b.actor_id ?? b.actor ?? roles[0]?.actor_id,
    role = roles.find((r) => r.actor_id === actor),
    name = role?.name ?? "角色";
  const common = { schema_version: 1, actor_id: actor, fictional: true };
  if (path === "session")
    return {
      authenticated: true,
      csrf: "workspace-synthetic",
      username: "设计预览",
      conversations: [
        {
          id: "conversation:preview",
          label: "私人对话",
          actors: roles.map((r) => r.actor_id),
        },
      ],
      dialogue: { available: true, code: "ok", model: "本地演示" },
    };
  if (path === "roles/view")
    return {
      roles,
      legacy_roles: [],
      profiles: [],
      providers,
      default_available: true,
      capabilities: ["dialogue", "memory.read", "memory.write"],
    };
  if (path === "roles/delete") {
    const index = roles.findIndex((r) => r.actor_id === actor);
    if (index < 0) throw Error("not_found");
    const [removed] = roles.splice(index, 1);
    return { role: { ...removed, deleted: true, state: "disabled" } };
  }
  if (path === "roles/apply") {
    const existing = b.actor_id
      ? roles.find((item) => item.actor_id === b.actor_id)
      : null;
    const row = existing ?? {
      ...roles[0],
      actor_id: `actor:${randomUUID()}`,
      model_version: 0,
      model_functions: {},
    };
    Object.assign(row, b, {
      actor_id: row.actor_id,
      state: b.enabled ? "active" : "disabled",
      version: (row.version ?? 0) + 1,
    });
    if (!existing) roles.push(row);
    return { role: row };
  }
  if (path === "roles/models/view") return roleModels(actor);
  if (path === "roles/models/apply") {
    role.model_functions[b.function_id] = b.provider_id;
    role.model_version++;
    return roleModels(actor);
  }
  if (path === "snapshot") {
    const snapshot = {
      state: "ready",
      submissions: [],
      snapshot: {
        observed_at: new Date().toISOString(),
        collectors: [],
        active_turns: [],
        history: turns.get(actor) ?? [
          {
            turn: {
              turn_id: `turn:${actor}`,
              turn_sequence: 1,
              version: 1,
              phase: "sent",
              delivery_state: "sent",
              unresolved_delivery: false,
            },
            messages: [
              {
                message_id: "message:hello",
                revision: 1,
                parts: [{ kind: "text", text: "今天过得怎么样？" }],
                sent_at: new Date(Date.now() - 600000).toISOString(),
                state: "active",
              },
            ],
            replies: [
              {
                reply_id: "reply:hello",
                segment_sequence: 1,
                segment_count: 1,
                state: "sent",
                content_state: "available",
                text: `刚把窗边的书读完一章，记了几句喜欢的话。你呢，今天有没有什么想聊的？ · ${name}`,
              },
            ],
          },
        ],
        next_before_turn_sequence: null,
      },
    };
    turns.set(actor, snapshot.snapshot.history);
    return snapshot;
  }
  if (path === "messages") {
    const history = turns.get(actor) ?? [];
    const id = randomUUID();
    history.push({
      turn: {
        turn_id: id,
        turn_sequence: history.length + 1,
        version: 1,
        phase: "sent",
        delivery_state: "sent",
        unresolved_delivery: false,
      },
      messages: [
        {
          message_id: id,
          revision: 1,
          parts: [{ kind: "text", text: b.text }],
          sent_at: new Date().toISOString(),
          state: "active",
        },
      ],
      replies: [
        {
          reply_id: `reply:${id}`,
          segment_sequence: 1,
          segment_count: 1,
          state: "sent",
          content_state: "available",
          text: `我听见啦。这里是 ${name} 的本地聊天交互演示，没有调用模型。`,
        },
      ],
    });
    turns.set(actor, history);
    return { message_id: id, state: "accepted", result: null };
  }
  if (path === "life/state")
    return {
      available: true,
      can_retry: false,
      code: "ready",
      peer: {
        configured: true,
        code: "ok",
        verified_at: new Date().toISOString(),
      },
    };
  if (path === "life/actors")
    return {
      schema_version: 1,
      fictional: true,
      items: roles.map((r) => ({
        actor_id: r.actor_id,
        label: r.name,
        actor_version: 1,
        world_id: "world:preview",
        room_id: "room:preview",
      })),
      next_after_actor_id: null,
    };
  if (path === "life/snapshot")
    return {
      ...common,
      actor_version: 1,
      world_id: "world:preview",
      room_id: "room:preview",
      timezone: "Asia/Shanghai",
      activity: actor === roles[0]?.actor_id ? "窗边阅读" : "整理旅行笔记",
      mood: "calm",
      outfit_ref: null,
      changed_at: now() - 300,
      observed_at: now(),
      state_basis: "last_persisted",
    };
  if (path === "life/today")
    return {
      ...common,
      day: "2026-10-29",
      timezone: "Asia/Shanghai",
      enabled: true,
      observed_at: now(),
      state_basis: "last_persisted",
      plan: {
        plan_id: `plan:${actor}`,
        version: 1,
        state: "active",
        generation_state: "completed",
        generated_by: "gateway",
        current_phase_id: "phase:3",
        entries: [
          [480, "慢慢开始一天", "早餐后整理房间，给窗边的小植物浇水。"],
          [600, "阅读与记录", "读一段书，把想法记在便签里。"],
          [840, "午后的散步", "沿着熟悉的小路走一走。"],
          [1200, "夜间阅读", "灯光落在书页上，今天想把这章读完。"],
        ].map(([minute, activity, detail], i) => ({
          phase_id: `phase:${i}`,
          minute,
          activity,
          detail,
          state: i === 3 ? "current" : "elapsed",
          generation_state: "completed",
        })),
      },
    };
  if (path === "life/timeline")
    return { ...common, day: b.day, items: [], next_after: null };
  if (path === "life/diaries")
    return {
      ...common,
      items: [29, 28, 26, 23, 21, 18].map((day) => ({
        diary_id: `diary:${actor}:${day}`,
        actor_id: actor,
        day: `2026-10-${day}`,
        state: "published",
        version: 1,
        published_revision_id: `revision:${day}`,
        fictional: true,
        captured: {
          recipe_id: "diary",
          recipe_version: 1,
          material_version: "1",
        },
      })),
      next_after: null,
    };
  if (path === "life/revision")
    return {
      ...common,
      diary_id: b.diary_id,
      diary_version: 1,
      revision_id: b.revision_id,
      content: `今天没有发生什么特别的事。\n\n午后读了几页书，想起一段还没写完的故事。窗外的天一点点暗下去，我决定把剩下的留给明天。\n\n这是一篇用于验证阅读布局的合成日记。——${name}`,
      created_at: now() - 600,
      captured: {
        recipe_id: "diary",
        recipe_version: 1,
        material_version: "1",
        config_version: 1,
      },
    };
  if (path === "life/runtime/read") {
    let items = [];
    if (b.resource === "state")
      items = [
        {
          actor_id: actor,
          activity: "窗边阅读",
          mood: "calm",
          timezone: "Asia/Shanghai",
          changed_at: now() - 300,
          outfit_ref: null,
        },
      ];
    if (b.resource === "affect")
      items = [
        {
          actor_id: actor,
          valence: 0.35,
          feelings: [
            {
              id: "feeling:a",
              kind: "warm",
              reason: "读到喜欢的段落",
              intensity: 0.4,
              sources: [],
            },
          ],
          observed_at: now(),
          relationship_effect: "none",
        },
      ];
    if (b.resource === "autonomy")
      items = [
        {
          id: "goal:a",
          actor_id: actor,
          version: 1,
          enabled: true,
          goal_id: "goal:a",
          title: "给自己的小书架留一本新故事",
          reason: "想把最近的阅读和见闻写下来",
          state: "active",
          next_due_at: null,
          updated_at: now(),
          current_step: null,
          step_state: null,
          error_code: null,
          sources: [],
        },
      ];
    if (b.resource === "activities")
      items = [
        {
          id: "activity:a",
          actor_id: actor,
          version: 1,
          title: "把故事写到下一页",
          state: "active",
          checkpoint: {
            note: "整理好开头，下一步写两个人初次相遇的场景。",
            step: 2,
            position: 2,
            unit: "step",
          },
          resume_condition: null,
          sources: [],
          result_refs: [],
          scope: null,
          started_at: now() - 1200,
          updated_at: now() - 300,
          next_due_at: null,
        },
      ];
    if (b.resource === "works")
      items = [
        "雨停之前",
        "窗台上的十月",
        "散步的地图",
        "风经过的地方",
        "未寄出的信",
      ].map((title, index) => ({
        id: `work:${index}`,
        version: 1,
        actor_id: actor,
        title,
        outline: "从日常的片刻里，慢慢长出一个故事。",
        state: index < 2 ? "published" : "draft",
        characters: [],
        chapter_ids: [],
      }));
    return {
      schema_version: 2,
      request_id: "preview:read",
      actor_id: actor,
      resource: b.resource,
      items,
      next_cursor: null,
      fictional: true,
    };
  }
  if (path === "skills/read")
    return {
      ...common,
      request_id: "preview:skills",
      result: {
        actor_version: 1,
        catalog_version: "preview-catalog",
        skills: [],
        sources: [],
      },
    };
  if (path === "weather/current")
    return {
      revision: 0,
      can_manage: false,
      configured: false,
      credential_configured: false,
      host: null,
      location: null,
      server_time: now(),
      weather: null,
      fetched_at: null,
      stale: false,
      code: "weather_not_configured",
    };
  if (path === "bot-adapters/view")
    return { available: true, unlocked: true, actors: [], connections: [] };
  if (path === "bots/view")
    return {
      available: true,
      slots: [],
      connections: [],
      management: { code: "ok", unlocked: true },
    };
  if (path === "bot-observation/view")
    return { available: true, unlocked: true, connections: [] };
  if (path === "bot-adapters/onebot-servers")
    return { available: true, ws_port: 18765, servers };
  if (path === "bot-adapters/onebot-create") {
    servers.push({
      id: `onebot:${randomUUID()}`,
      name: b.name,
      token: "synthetic-new-token-only",
      account_id: null,
      connected: false,
      observation_id: null,
      ws_path: "/onebot/v11/ws/preview-new",
    });
    return { available: true, ws_port: 18765, servers };
  }
  if (path === "assets/state" || path === "assets/connection")
    return {
      available: true,
      code: "ready",
      enabled: true,
      connection: "library-preview",
      scope_ttl_seconds: 1200,
      connections: [
        {
          connection_id: "library-preview",
          label: "本地资料示例",
          credential_registered: true,
        },
      ],
      preview,
      page_size: { default: 20, maximum: 100 },
      sort: { fields: ["name"], directions: ["asc"] },
      kinds: ["file", "directory"],
      scopes: [],
      search_scopes: ["library"],
      reselect: "",
      cancel: "",
      reads: [],
    };
  if (path === "assets/libraries")
    return {
      operation: "libraries.list",
      connection: "library-preview",
      libraries: [library],
      page,
      preview,
    };
  if (path === "assets/browse")
    return {
      operation: "entries.browse",
      connection: "library-preview",
      library,
      parent_relative_path: "",
      entries,
      page,
      options: {
        sort_by: "name",
        sort_direction: "asc",
        kind: "",
        name_filter: "",
      },
      preview,
    };
  if (path === "assets/entry")
    return {
      operation: "entries.get",
      connection: "library-preview",
      library,
      entry: entries.find((e) => e.entry_id === b.entry_id) ?? entries[0],
      preview,
    };
  if (path === "knowledge/state")
    return {
      available: true,
      code: "ready",
      projects: [{ project_id: "project:reading", label: "阅读与灵感" }],
      peer: {
        configured: true,
        code: "ok",
        verified_at: new Date().toISOString(),
      },
    };
  if (path === "knowledge/documents")
    return {
      project_id: b.project_id,
      operation: "documents",
      result: {
        project_id: b.project_id,
        project_revision: 1,
        items: entryNames.map((title, index) => ({
          document_id: title,
          kind: index % 2 ? "阅读笔记" : "研究资料",
          version: 1,
          indexed_state: "ready",
          source_validation: "verified",
        })),
        next_cursor: null,
        omissions: [],
      },
    };
  if (path === "knowledge/document")
    return {
      project_id: b.project_id,
      operation: "document",
      result: {
        project_id: b.project_id,
        project_revision: 1,
        document_id: b.document_id,
        version: 1,
        hash: "preview",
        blocks: [
          {
            reference: {
              block_id: "block:a",
              document_id: b.document_id,
              version: 1,
              hash: "preview",
            },
            text: "这是本地合成资料，用来检查书架与阅读详情的布局。",
            spans: [],
          },
        ],
        next_cursor: null,
        omissions: [],
      },
    };
  if (path === "personas/profiles")
    return {
      profiles: [],
      targets: roles.map((r) => ({
        id: r.actor_id,
        kind: "role",
        name: r.name,
        description: "独立角色档案",
        version: 1,
        state: "published",
        published_revision: "revision:preview",
        draft_revision: null,
        last_applied_profile_revision: null,
      })),
      permissions: { create: true, edit: true, apply: true },
    };
  if (path === "personas/view")
    return {
      item: {
        id: b.id,
        kind: "role",
        name: roles.find((r) => r.actor_id === b.id)?.name ?? "角色",
        description: "独立角色档案",
        version: 1,
        state: "published",
        published_revision: "revision:preview",
        draft_revision: null,
        last_applied_profile_revision: null,
        content: {
          persona: "喜欢安静的阅读与散步，对生活中的小事保持好奇。",
          tone: "平静、自然",
          style: "具体而简洁",
          address: "",
        },
      },
    };
  throw Error("not_configured");
}
export function mockApi() {
  return {
    name: "role-workspace-local-fixtures",
    configureServer(server) {
      server.middlewares.use((req, res, next) => {
        const path = new URL(req.url, "http://localhost").pathname;
        if (!path.startsWith("/api/web/")) return next();
        let raw = "";
        req.on("data", (chunk) => (raw += chunk));
        req.on("end", () => {
          res.setHeader("content-type", "application/json");
          res.setHeader("cache-control", "no-store");
          try {
            res.end(
              JSON.stringify(answer(path.slice(9), raw ? JSON.parse(raw) : {})),
            );
          } catch (error) {
            res.statusCode = 503;
            res.end(JSON.stringify({ code: error.message }));
          }
        });
      });
    },
    transformIndexHtml(html) {
      return html.replace(
        "<body>",
        '<body><div class="preview-label" style="position:fixed;bottom:0;left:0;right:0;z-index:99999;padding:7px;text-align:center;background:#efe9d8;color:#786548;font:12px system-ui">角色工作区 · 本地设计预览 · 数据及聊天均为合成，不连接生产 QQ 或模型</div>',
      );
    },
  };
}
