"""Synthetic SDK boundary around the actual controlled NoneBot host; local only."""


def run():
    import argparse
    import asyncio
    import json
    from pathlib import Path
    import sys
    import time

    args = argparse.ArgumentParser()
    args.add_argument("--export", required=True)
    args.add_argument("--data", required=True)
    args.add_argument("--port", type=int, required=True)
    options = args.parse_args()
    root = Path(options.export).resolve()
    sys.path[:0] = [
        str(root / "integrations/nonebot"),
        str(root / "vendor/tianshu_storage-0.1.3-py3-none-any.whl"),
    ]

    import nonebot
    import uvicorn
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from nonebot.adapters.onebot.v11 import Adapter, Bot, GroupMessageEvent
    from nonebot.message import handle_event
    from tianshu_nonebot.onebot import create_host

    app = create_host(
        host="127.0.0.1",
        port=options.port,
        data_dir=Path(options.data),
        access_token="synthetic-joint-only",
    )
    import tianshu_nonebot.adapter_plugin as host

    assert host.participation_host.available(), "actual controlled NoneBot graph unavailable"
    adapter = nonebot.get_adapter(Adapter)
    bot = Bot(adapter, "10001")
    state = {"calls": [], "lose_send": False, "lose_claim": False}
    server = None

    async def login():
        return {"user_id": 10001, "nickname": "synthetic joint bot"}

    async def channel_send(**kwargs):
        state["calls"].append({"group_id": kwargs["group_id"], "text": str(kwargs["message"])})
        if state["lose_send"]:
            state["lose_send"] = False
            raise OSError("synthetic channel response lost after effect")
        return {"message_id": 90000 + len(state["calls"])}

    bot.get_login_info = login
    bot.send_group_msg = channel_send

    @app.middleware("http")
    async def lose_claim_response(request, call_next):
        response = await call_next(request)
        if request.url.path.endswith("/participation/claim") and state["lose_claim"]:
            state["lose_claim"] = False
            return JSONResponse(
                {"code": "dependency_unavailable", "retryable": True}, status_code=503
            )
        return response

    @app.get("/fixture/info")
    async def info():
        return {
            "instance_id": host.service.instance_id,
            "access_key": host.service.access_key,
            "controlled": host.participation_host.available(),
            "calls": state["calls"],
        }

    @app.post("/fixture/event")
    async def event(request: Request):
        body = await request.json()
        temporary = None
        if body.get("foreign"):
            temporary = nonebot.on_message(priority=6, block=False)

            @temporary.handle()
            async def foreign_handler():
                pass
        elif body.get("unknown"):

            async def unknown_rule():
                return False

            temporary = nonebot.on_message(rule=unknown_rule, priority=6, block=False)
        message = [{"type": "text", "data": {"text": "Synthetic native event " + str(body["id"])}}]
        incoming = GroupMessageEvent(
            time=int(time.time()),
            self_id=10001,
            post_type="message",
            sub_type="normal",
            message_type="group",
            group_id=20001,
            user_id=10002,
            message_id=body["id"],
            message=message,
            raw_message="Synthetic native event",
            font=0,
            sender={"user_id": 10002},
        )
        try:
            await handle_event(bot, incoming)
        finally:
            if temporary:
                temporary.destroy()
        row = host.service.db.execute(
            "SELECT p.document FROM participation_state p JOIN observations o ON o.id=p.event_id WHERE o.native_id=?",
            (str(body["id"]),),
        ).fetchone()
        return {"ownership": json.loads(row[0]) if row else None}

    @app.post("/fixture/failure")
    async def failure(request: Request):
        body = await request.json()
        assert set(body) <= {"lose_send", "lose_claim"}
        state.update(body)
        return {"configured": True}

    @app.post("/fixture/snapshot")
    async def snapshot(request: Request):
        body = await request.json()
        row = host.service.db.execute(
            "SELECT id,acked FROM observations WHERE native_id=?", (str(body["id"]),)
        ).fetchone()
        bindings = (
            []
            if row is None
            else [
                json.loads(item[0])
                for item in host.service.db.execute(
                    "SELECT document FROM participation_bindings WHERE event_id=?", (row[0],)
                )
            ]
        )
        return {
            "found": row is not None,
            "acked": bool(row[1]) if row else False,
            "bindings": bindings,
            "calls": state["calls"],
        }

    @app.post("/fixture/shutdown")
    async def shutdown():
        server.should_exit = True
        return {"stopping": True}

    async def serve():
        nonlocal server
        adapter.bot_connect(bot)
        server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=options.port, log_level="error", lifespan="on"
            )
        )
        await server.serve()

    asyncio.run(serve())


if __name__ == "__main__":
    run()
