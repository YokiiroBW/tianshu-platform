"""Synthetic Home Assistant REST surface for isolated tests; never a real instance.

The fixture speaks the documented REST shapes (`GET /api/states/<entity_id>`,
`POST /api/services/<domain>/<service>`) and can be switched into failure modes so the
connector's honest behaviour is exercised against a real HTTP peer: redirects, rejected
credentials, a dropped connection, an unreadable or oversized receipt, and a service call
that HA reports as changed while the device state never moves.
"""

import asyncio
import socket

from aiohttp import web

TOKEN = "synthetic-ts017-home-assistant-service-token"
ENV = {"TS017_HA_TOKEN": TOKEN}
STATES = {
    "light.study": {"state": "off", "attributes": {}},
    "switch.kettle": {"state": "off", "attributes": {}},
    "sensor.living_temperature": {
        "state": "23.5",
        "attributes": {"unit_of_measurement": "°C"},
    },
}
TARGETS = {"turn_on": "on", "turn_off": "off"}
OVERSIZED = b'[{"padding":"' + b"x" * 1_100_000 + b'"}]'
HOME_KEY = web.AppKey("home", object)


def reserve():
    """One currently free loopback port; closed again so the caller can bind it."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SyntheticHome:
    """A tiny HA stand-in: real HTTP, real state table, explicit failure switches."""

    def __init__(self, states=None, token=TOKEN):
        self.token = token
        self.states = {key: dict(value) for key, value in (states or STATES).items()}
        self.mode = "normal"
        # Endpoint-only modes keep the other side healthy while one path misbehaves.
        self.service_mode = None
        self.read_mode = None
        self.delay = 3.0
        self.hold = asyncio.Event()
        self.read_hold = asyncio.Event()
        self.redirect_target = "/login"
        self.log = []

    def document(self, entity_id):
        entity = self.states[entity_id]
        return {
            "entity_id": entity_id,
            "state": entity["state"],
            "attributes": dict(entity["attributes"]),
            "last_changed": "2026-09-15T00:00:00Z",
            "last_updated": "2026-09-15T00:00:00Z",
            "context": {"id": "synthetic"},
        }

    def authorized(self, request):
        return request.headers.get("Authorization", "") == "Bearer " + self.token

    async def _gate(self, request, mode):
        if request.path.startswith("/api/"):
            self.log.append(
                {
                    "method": request.method,
                    "path": request.path,
                    "authorized": self.authorized(request),
                }
            )
        if mode == "redirect":
            raise web.HTTPFound(self.redirect_target)
        if mode == "server_error":
            raise web.HTTPInternalServerError()
        if mode == "unauthorized" or not self.authorized(request):
            raise web.HTTPUnauthorized()

    async def _hold(self, event):
        """Held open until the test releases it, so asynchronous windows are deterministic."""
        async with asyncio.timeout(30):
            await event.wait()

    async def probe(self, request):
        await self._gate(request, self.mode)
        return web.json_response({"message": "API running."})

    async def state(self, request):
        mode = self.read_mode or self.mode
        await self._gate(request, mode)
        # `slow` and `hold` stay endpoint-specific: only an explicit read mode delays reads.
        if self.read_mode == "slow":
            await asyncio.sleep(self.delay)
        if self.read_mode == "hold":
            await self._hold(self.read_hold)
        entity_id = request.match_info["entity_id"]
        if entity_id not in self.states:
            raise web.HTTPNotFound()
        if mode == "oversized":
            return web.Response(body=OVERSIZED, content_type="application/json")
        if mode == "unreadable":
            return web.Response(body=b"<html>not json</html>", content_type="application/json")
        if mode == "wrong_entity":
            other = next(key for key in self.states if key != entity_id)
            return web.json_response(self.document(other))
        if mode == "malformed":
            document = self.document(entity_id)
            document["state"] = "not a real state"
            return web.json_response(document)
        return web.json_response(self.document(entity_id))

    async def service(self, request):
        mode = self.service_mode or self.mode
        await self._gate(request, mode)
        if mode == "slow":
            await asyncio.sleep(self.delay)
        if mode == "hold":
            await self._hold(self.hold)
        domain = request.match_info["domain"]
        action = request.match_info["service"]
        body = await request.json()
        entity_id = body.get("entity_id") if isinstance(body, dict) else None
        target = TARGETS.get(action)
        known = isinstance(entity_id, str) and entity_id in self.states
        # HA applies the service only for the entity's own domain.
        applied = known and domain == entity_id.split(".")[0] and mode != "unapplied"
        if applied and target:
            self.states[entity_id]["state"] = target
        if mode == "oversized":
            return web.Response(body=OVERSIZED, content_type="application/json")
        if mode == "unreadable":
            return web.Response(body=b"not json", content_type="application/json")
        # A 200 receipt names what HA believes changed, whether or not the device moved.
        changed = []
        if known:
            document = self.document(entity_id)
            if target:
                document["state"] = target
            changed.append(document)
        return web.json_response(changed)

    async def set_mode(self, request):
        body = await request.json()
        self.mode = body.get("mode", "normal")
        self.service_mode = body.get("service_mode")
        self.read_mode = body.get("read_mode")
        if "delay" in body:
            self.delay = float(body["delay"])
        if "redirect_target" in body:
            self.redirect_target = body["redirect_target"]
        for event, mode in (
            (self.hold, self.service_mode or self.mode),
            (self.read_hold, self.read_mode or self.mode),
        ):
            event.set() if mode != "hold" else event.clear()
        return web.json_response(
            {"mode": self.mode, "service_mode": self.service_mode, "read_mode": self.read_mode}
        )

    async def release(self, request):
        self.hold.set()
        self.read_hold.set()
        return web.json_response({"released": True})

    async def set_state(self, request):
        body = await request.json()
        entity_id = body["entity_id"]
        self.states[entity_id]["state"] = body["state"]
        return web.json_response({"entity_id": entity_id, "state": body["state"]})

    async def read_log(self, request):
        return web.json_response(
            {"mode": self.mode, "requests": list(self.log), "states": dict(self.states)}
        )

    async def clear_log(self, request):
        self.log.clear()
        return web.json_response({"cleared": True})


def home_app(states=None, token=TOKEN):
    home = SyntheticHome(states, token)
    app = web.Application()
    app[HOME_KEY] = home
    app.router.add_get("/api/", home.probe)
    app.router.add_get("/api/states/{entity_id}", home.state)
    app.router.add_post("/api/services/{domain}/{service}", home.service)
    app.router.add_post("/fixture/mode", home.set_mode)
    app.router.add_post("/fixture/release", home.release)
    app.router.add_post("/fixture/state", home.set_state)
    app.router.add_get("/fixture/log", home.read_log)
    app.router.add_post("/fixture/log/clear", home.clear_log)
    return app


async def start_home(app, port=0):
    """Bind the synthetic HA on a real loopback port; 0 asks the OS for a free one."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    return runner, "http://127.0.0.1:" + str(runner.addresses[0][1])
