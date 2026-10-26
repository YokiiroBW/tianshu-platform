"""Same-origin plugin console adapter, reusing the authenticated Platform operator."""

from ..contracts import Fault, require
from .control import openapi, validate
import secrets
import time
from .views import POLICY

PREFIX = "/api/web/plugins/"


class WebPlugins:
    def __init__(self, host, console):
        self.host, self.console = host, console
        self.tickets = {}
        self.view_policy = POLICY

    async def page(self, ticket, session):
        from .views import document

        await self.host.local(self.guard, session)
        entry = self.tickets.pop(ticket, None)
        require(
            entry and entry["session"] is session and entry["expires"] > time.monotonic(),
            "not_found",
            404,
        )
        return await self.host.local(document, entry["package"], entry["view"], entry["nonce"])

    def guard(self, session):
        require(self.console.session_valid(session), "session_expired", 401)
        principal = self.console.platform.auth.principals[self.console.config["principal"]]
        require(
            principal["kind"] == "operator"
            and principal["service"] == "platform"
            and "config.publish" in principal["actions"],
            "operator_not_authorized",
            403,
        )
        return True

    async def route(self, operation, body, session):
        require(isinstance(body, dict), "invalid_input", 400)
        await self.host.local(self.guard, session)
        validate(operation, body)
        principal = self.console.config["principal"]

        def authorize():
            return self.guard(session)

        host, repo = self.host, self.host.repository
        if operation == "view":
            result = await host.view()
        elif operation == "schema":
            result = openapi()
        elif operation == "catalog":
            result = {"items": await host.catalog()}
        elif operation == "actors":
            result = {"items": await host.local(self.console.platform.actor_capabilities.directory)}
        elif operation == "view-content":
            instance = await host.instance(body["instance_id"])
            package = await host.package(instance["package_id"], instance["version"])
            view = next(
                (
                    item
                    for item in package["description"].get("views", [])
                    if item["id"] == body["page_id"]
                ),
                None,
            )
            require(view, "not_found", 404)
            self.tickets = {
                key: item
                for key, item in self.tickets.items()
                if item["expires"] > time.monotonic()
            }
            require(len(self.tickets) < 256, "too_many_requests", 429)
            ticket = secrets.token_urlsafe(32)
            self.tickets[ticket] = {
                "session": session,
                "package": package,
                "view": view,
                "nonce": body["nonce"],
                "expires": time.monotonic() + 60,
            }
            result = {"url": PREFIX + "page/" + ticket}
        elif operation == "install":
            result = await host.install(body["source"], body["client_request_id"])
        elif operation == "installation":
            result = await host.local(repo.get, "operations", body["id"])
            require(result, "not_found", 404)
        elif operation == "create":
            result = await host.create(body["package_id"], body["name"], body["client_request_id"])
        elif operation == "configure":
            result = await host.configure(
                body["instance_id"],
                body["config"],
                body.get("secrets", {}),
                body["expected_revision"],
            )
        elif operation in {"enable", "disable"}:
            result = await host.state(
                body["instance_id"], operation == "enable", body["expected_revision"]
            )
        elif operation == "bindings":
            await host.local(
                self.console.platform.actor_capabilities.validate_selection, body["bindings"]
            )
            result = await host.bindings(
                body["instance_id"], body["bindings"], body["expected_revision"]
            )
        elif operation == "invoke":
            result = await host.executions.invoke(
                body["instance_id"],
                body["action"],
                body["input"],
                body["client_request_id"],
                principal,
                authorize=authorize,
                binding_id=body.get("binding_id"),
            )
        elif operation == "invocations":
            result = {
                "items": [
                    host.executions.public(item)
                    for item in await host.local(repo.calls, body.get("instance_id"))
                ]
            }
        elif operation == "invocation":
            result = host.executions.public(await host.local(repo.call, body["id"]))
        elif operation == "cancel":
            result = await host.executions.cancel(body["id"])
        elif operation in {"reconcile", "resume"}:
            result = await host.executions.recover(
                body["id"], principal, authorize, resume=operation == "resume"
            )
        elif operation == "upgrade":
            result = await host.upgrade(
                body["instance_id"], body["version"], body["expected_revision"]
            )
        elif operation == "uninstall":
            result = await host.uninstall(body["package_id"])
        elif operation == "logs":
            result = {"items": await host.local(repo.logs, body["instance_id"])}
        elif operation == "schedules":
            if "operation" in body:
                result = await host.scheduler.manage(
                    body["instance_id"], body["operation"], body["value"]
                )
            else:
                result = {
                    "items": [
                        item
                        for item in await host.local(repo.list, "schedules")
                        if item["instance_id"] == body["instance_id"]
                    ]
                }
        else:
            raise Fault("not_found", 404)
        await host.local(self.guard, session)
        return result
