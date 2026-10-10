"""Function catalogue and bindings, owned by the existing provider catalogue."""

from .contracts import require


FUNCTIONS = (
    ("chat", "主对话", "日常聊天、角色表达与最终回复", True),
    ("tools", "工具处理", "工具选择、参数整理与结果理解", False),
    ("code", "代码", "代码编写、分析与排错", False),
    ("search", "网页搜索", "搜索资料、阅读网页与整理来源", False),
    ("writing", "日常与日记", "角色日常、生活经历与日记生成", True),
    ("memory", "记忆整理", "记忆提取、摘要与归纳", False),
)
FUNCTION_IDS = frozenset(item[0] for item in FUNCTIONS)


def validate_function(value):
    require(isinstance(value, str) and value in FUNCTION_IDS, "invalid_input", 400)
    return value


class ModelFunctions:
    def __init__(self, catalog):
        self.catalog = catalog

    def view(self, db, providers, default):
        bindings = {
            row["function_id"]: dict(row)
            for row in db.execute("SELECT * FROM model_function_bindings")
        }
        providers = {item["provider_id"]: item for item in providers}
        result = []
        for identity, name, description, connected in FUNCTIONS:
            # Main chat is the existing default pointer, not a second competing setting.
            binding = default if identity == "chat" else bindings.get(identity, {})
            provider_id = binding.get("provider_id")
            selected_id = provider_id or default["provider_id"]
            selected = providers.get(selected_id)
            revision = (
                binding.get("provider_revision") if provider_id else default["provider_revision"]
            )
            result.append(
                {
                    "function_id": identity,
                    "name": name,
                    "description": description,
                    "connected": connected,
                    "revision": binding.get("revision", 0),
                    "provider_id": provider_id,
                    "provider_revision": binding.get("provider_revision"),
                    "effective_provider_id": selected_id,
                    "effective_provider_revision": revision,
                    "configured": bool(
                        selected
                        and selected["revision"] == revision
                        and self.catalog._selectable(selected)
                    ),
                }
            )
        return result

    def set_binding(
        self, *, client_id, function_id, provider_id, expected_revision, expected_binding_revision
    ):
        validate_function(function_id)
        require(
            type(expected_binding_revision) is int and expected_binding_revision >= 0,
            "invalid_input",
            400,
        )
        require(provider_id is not None or expected_revision is None, "invalid_input", 400)
        if function_id == "chat":
            result = self.catalog.set_default(
                client_id=client_id,
                provider_id=provider_id,
                expected_revision=expected_revision,
                expected_default_revision=expected_binding_revision,
            )
            return {"function_id": function_id, "revision": result["revision"]}

        def action(db):
            row = db.execute(
                "SELECT revision FROM model_function_bindings WHERE function_id=?", (function_id,)
            ).fetchone()
            revision = row[0] if row else 0
            require(revision == expected_binding_revision, "revision_conflict", 409)
            if provider_id is not None:
                provider, _ = self.catalog._row(db, provider_id, expected_revision)
                require(self.catalog._selectable(provider), "provider_not_tested", 409)
            db.execute(
                "INSERT INTO model_function_bindings VALUES (?,?,?,?) "
                "ON CONFLICT(function_id) DO UPDATE SET provider_id=excluded.provider_id, "
                "provider_revision=excluded.provider_revision, revision=excluded.revision",
                (function_id, provider_id, expected_revision, revision + 1),
            )
            return {"function_id": function_id, "revision": revision + 1}

        return self.catalog._mutate(
            client_id,
            "function",
            [function_id, provider_id, expected_revision, expected_binding_revision],
            action,
        )
