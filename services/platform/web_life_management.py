"""One explicit generation retry; life facts remain behind the independent read service."""

from .contracts import Fault, require
from .transport import management_call


class WebLifeManagement:
    def __init__(self, platform, console):
        self.p, self.console = platform, console

    def available(self):
        principal = self.p.auth.principals.get(self.console.config["principal"], {})
        return bool(
            self.p.settings.get("core") is not None
            and self.console.life.code() == "ready"
            and {"role.manage", "life.read"} <= set(principal.get("actions", []))
        )

    def guard(self, session):
        require(self.console.session_valid(session), "session_expired", 401)
        require(self.available(), "forbidden", 403)

    async def retry(self, body, session):
        await self.p.local_work.run(self.guard, session)
        require(
            isinstance(body, dict)
            and set(body)
            == {
                "actor_id",
                "plan_id",
                "phase_id",
                "expected_version",
            },
            "invalid_input",
            400,
        )
        self.p.contracts.check("life-read#retry_request", body)
        # The management token cannot substitute for the independent actor read grant.
        # Check the same read route before mutation and again before returning its receipt.
        actor = {"actor_id": body["actor_id"]}
        await self.console.life.route("/api/web/life/today", actor, session)
        result = await management_call(
            self.p.settings["core"],
            "/internal/v1/life-generation/retry",
            body,
        )
        await self.p.local_work.run(self.guard, session)
        await self.console.life.route("/api/web/life/today", actor, session)
        try:
            self.p.contracts.check("life-read#retry_response", result)
        except Fault:
            raise Fault("invalid_upstream", 502) from None
        require(
            result["actor_id"] == body["actor_id"] and result["plan_id"] == body["plan_id"],
            "invalid_upstream",
            502,
        )
        return result
