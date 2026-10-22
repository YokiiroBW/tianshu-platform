"""Authenticated access-settings operations; no listener or deployment authority."""

import asyncio

from .contracts import Fault, require
from .web_account import valid_password_length


class WebAccessSettings:
    def __init__(self, console, access):
        self.console, self.access = console, access
        self.lock = asyncio.Lock()

    async def route(self, path, body, session):
        if path == "/api/web/access/view":
            require(body == {}, "invalid_input", 400)
            return await asyncio.to_thread(self.access.view)
        require(path == "/api/web/access/save", "not_found", 404)
        require(self.access.current is not None, "web_not_configured", 503)
        require(set(body) == {"password", "value", "revision"}, "invalid_input", 400)
        password = body["password"]
        require(valid_password_length(password), "unauthorized", 401)
        console = self.console
        async with self.lock, console.login_lock:
            console.failures = [t for t in console.failures if t > console.clock() - 60]
            require(len(console.failures) < 5, "too_many_requests", 429)
            if not await asyncio.to_thread(console.verify_password, password):
                console.failures.append(console.clock())
                raise Fault("unauthorized", 401)
            fingerprint, _ = await console.platform.local_work.run(console.authority)
            require(
                console.session_live(session) and session["fingerprint"] == fingerprint,
                "session_expired",
                401,
            )
            # Serial with other saves, and no yield between the final authority check and write.
            return self.access.save(body["value"], body["revision"])
