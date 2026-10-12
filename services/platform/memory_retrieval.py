"""Platform-owned retrieval settings; model execution stays in the gateway."""

from types import SimpleNamespace
from urllib.parse import urlsplit

from .contracts import require, digest, Fault

PATH = "/internal/v1/memory-retrieval/execute"
PREFIX = "/api/web/memory-retrieval/"
DEFAULT = {"mode": "keyword", "rerank": False, "candidates": 40}


def validate_policy(value):
    require(isinstance(value, dict) and set(value) == set(DEFAULT), "invalid_input", 400)
    require(
        value["mode"] in {"keyword", "vector", "hybrid"}
        and type(value["rerank"]) is bool
        and type(value["candidates"]) is int
        and 1 <= value["candidates"] <= 100,
        "invalid_input",
        400,
    )


def validate_connection(value):
    require(
        isinstance(value, dict) and set(value) == {"base_url", "model_id"}, "invalid_input", 400
    )
    url, model = value["base_url"], value["model_id"]
    require(
        isinstance(url, str)
        and 1 <= len(url) <= 2048
        and all(32 < ord(c) < 127 for c in url)
        and "%" not in url
        and "\\" not in url,
        "invalid_input",
        400,
    )
    try:
        parsed = urlsplit(url)
        require(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and not parsed.query
            and not parsed.fragment
            and (parsed.port is None or 0 < parsed.port < 65536)
            and not any(part in {".", ".."} for part in parsed.path.split("/")),
            "invalid_input",
            400,
        )
    except ValueError:
        require(False, "invalid_input", 400)
    require(
        isinstance(model, str) and 1 <= len(model) <= 512 and all(32 < ord(c) < 127 for c in model),
        "invalid_input",
        400,
    )


class MemoryRetrieval:
    def __init__(self, platform, console):
        self.p, self.console = platform, console

    @property
    def catalog(self):
        return self.console.external.catalog

    def snapshot(self):
        if self.catalog is None:
            return 0, {}
        revision, rows = self.catalog.snapshot()
        selected = {
            key: rows[key]
            for key in ("retrieval", "retrieval:embed", "retrieval:rerank")
            if key in rows
        }
        providers = {item["provider_id"]: item for item in self.models()}
        for kind in ("embed", "rerank"):
            row = selected.get("retrieval:" + kind)
            if row and "provider_id" in row["value"]:
                model = providers.get(row["value"]["provider_id"])
                row["value"] = {
                    **row["value"],
                    "catalog_revision": model["revision"] if model else None,
                }
        return revision, selected

    def models(self):
        catalog = getattr(self.p, "provider_catalog", None)
        return catalog.view()["providers"] if catalog is not None else []

    def connection_view(self, rows, kind):
        row = rows.get("retrieval:" + kind, {})
        value = row.get("value", {})
        if "provider_id" in value:
            model = next(
                (item for item in self.models() if item["provider_id"] == value["provider_id"]),
                None,
            )
            return {
                "provider_id": value["provider_id"],
                "base_url": model["base_url"] if model else "",
                "model_id": model["model_id"] if model else "",
                "has_key": bool(
                    model
                    and model["has_key"]
                    and model["enabled"]
                    and model["source_enabled"]
                    and model["kind"] == kind
                ),
            }
        return {
            "base_url": value.get("base_url", ""),
            "model_id": value.get("model_id", ""),
            "has_key": bool(row.get("credential")),
        }

    def settings(self, rows):
        value = rows.get("retrieval", {}).get("value", DEFAULT)
        policy = {key: value[key] for key in DEFAULT}
        validate_policy(policy)
        return {
            **policy,
            "revision": digest({key: row["value"] for key, row in rows.items()}),
            "embedding_revision": digest(rows.get("retrieval:embed", {}).get("value", {})),
        }

    def view(self):
        revision, rows = self.snapshot()
        return {
            "revision": revision,
            "can_manage": self.catalog is not None,
            "policy": {key: self.settings(rows)[key] for key in DEFAULT},
            "connections": {kind: self.connection_view(rows, kind) for kind in ("embed", "rerank")},
            "models": [
                {
                    "provider_id": item["provider_id"],
                    "name": item["source_name"] + " / " + item["name"],
                    "model_id": item["model_id"],
                    "kind": item["kind"],
                    "available": bool(
                        item["enabled"] and item["source_enabled"] and item["has_key"]
                    ),
                }
                for item in self.models()
                if item["kind"] in {"embed", "rerank"}
            ],
        }

    def gate(self, session):
        require(self.console.session_valid(session), "session_expired", 401)
        principal = self.p.auth.principals.get(self.console.config["principal"], {})
        require(
            principal.get("kind") == "operator"
            and "external.manage" in principal.get("actions", []),
            "forbidden",
            403,
        )

    def save(self, body, session):
        self.gate(session)
        require(self.catalog is not None, "external_not_configured", 503)
        require(
            set(body) == {"kind", "value", "credential", "expected_revision", "client_id"},
            "invalid_input",
            400,
        )
        kind, value, credential = body["kind"], body["value"], body["credential"]
        require(kind in {"policy", "embed", "rerank"}, "invalid_input", 400)
        if kind == "policy":
            validate_policy(value)
            require(credential == {"action": "keep"}, "invalid_input", 400)
            _, rows = self.snapshot()
            needed = (["embed"] if value["mode"] != "keyword" else []) + (
                ["rerank"] if value["rerank"] else []
            )
            for name in needed:
                try:
                    self.connection(rows, name)
                except Fault:
                    require(False, "retrieval_not_configured", 409)
        elif isinstance(value, dict) and set(value) == {"provider_id"}:
            require(credential == {"action": "keep"}, "invalid_input", 400)
            provider = next(
                (item for item in self.models() if item["provider_id"] == value["provider_id"]),
                None,
            )
            require(
                provider is not None
                and provider["kind"] == kind
                and provider["enabled"]
                and provider["source_enabled"]
                and provider["has_key"],
                "retrieval_not_configured",
                409,
            )
            # Replace the old standalone secret with a reference, never a copied key.
            credential = {"action": "clear"}
        else:
            validate_connection(value)
            require(
                isinstance(credential, dict)
                and credential.get("action") in {"keep", "replace", "clear"},
                "invalid_input",
                400,
            )
            require(
                set(credential)
                == ({"action", "value"} if credential["action"] == "replace" else {"action"}),
                "invalid_input",
                400,
            )
            if credential["action"] == "replace":
                key = credential["value"]
                require(
                    isinstance(key, str)
                    and 1 <= len(key) <= 4096
                    and all(33 <= ord(c) <= 126 for c in key),
                    "invalid_input",
                    400,
                )
        self.catalog.save(
            "retrieval" if kind == "policy" else "retrieval:" + kind,
            value,
            credential,
            {"action": "clear"},
            body["expected_revision"],
            body["client_id"],
        )
        return self.view()

    async def route(self, path, body, session):
        await self.p.local_work.run(self.gate, session)
        name = path.removeprefix(PREFIX)
        require(isinstance(body, dict), "invalid_input", 400)
        if name == "state":
            require(body == {}, "invalid_input", 400)
            return await self.p.local_work.run(self.view)
        if name == "save":
            return await self.p.local_work.run(self.save, body, session)
        require(
            name == "test" and set(body) == {"kind"} and body["kind"] in {"embed", "rerank"},
            "invalid_input",
            400,
        )
        _, rows = await self.p.local_work.run(self.snapshot)
        operation = body["kind"]
        payload = (
            {"texts": ["记忆连接测试"]}
            if operation == "embed"
            else {"query": "睡眠", "documents": ["晚上不喝咖啡", "喜欢散步"]}
        )
        await self.execute(rows, operation, payload)
        await self.p.local_work.run(self.gate, session)
        return {"outcome": "succeeded"}

    def connection(self, rows, operation):
        row = rows.get("retrieval:" + operation)
        if row and "provider_id" in row["value"]:
            value = row["value"]
            catalog = getattr(self.p, "provider_catalog", None)
            require(
                catalog is not None and value.get("catalog_revision") is not None,
                "retrieval_not_configured",
                503,
            )
            context = catalog.execution_context(value["provider_id"], value["catalog_revision"])
            require(context.kind == operation, "retrieval_not_configured", 503)
            return {
                key: getattr(context, key)
                for key in (
                    "provider_id",
                    "revision",
                    "protocol",
                    "base_url",
                    "model_id",
                    "api_key",
                )
            }
        require(
            row and row["credential"],
            "retrieval_not_configured",
            503,
        )
        value = row["value"]
        return dict(
            provider_id="memory-" + operation,
            revision=value["connection_revision"],
            protocol="openai-chat-completions",
            base_url=value["base_url"].rstrip("/"),
            model_id=value["model_id"],
            api_key=row["credential"],
        )

    async def execute(self, rows, operation, payload):
        require(self.console.providers.config is not None, "retrieval_not_configured", 503)
        context = SimpleNamespace(**self.connection(rows, operation))
        return await self.console.providers.gateway_request(operation, context, payload=payload)

    async def internal(self, header, body):
        with self.p.store.connect() as db:
            _, principal = self.p.auth.authenticate(header, db, "memory.retrieval")
            require(principal["kind"] == "service" and principal["service"] == "gateway")
        require(isinstance(body, dict), "invalid_input", 400)
        operation = body.get("operation")
        _, rows = await self.p.local_work.run(self.snapshot)
        settings = self.settings(rows)
        if operation == "settings":
            require(set(body) == {"operation"}, "invalid_input", 400)
            return settings
        require(
            operation == "runtime"
            and set(body) == {"operation", "kind", "revision"}
            and body["kind"] in {"embed", "rerank"},
            "invalid_input",
            400,
        )
        require(body["revision"] == settings["revision"], "revision_conflict", 409)
        return {"connection": self.connection(rows, body["kind"])}
