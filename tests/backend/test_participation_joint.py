"""Actual Platform + isolated controlled SDK/AdapterService; synthetic Core and QQ peers."""


def run():
    import argparse
    import asyncio
    import copy
    import hashlib
    from contextlib import closing
    import json
    import os
    from pathlib import Path
    import socket
    import subprocess
    import sys
    import tempfile
    import time
    from unittest.mock import patch

    import aiohttp

    args = argparse.ArgumentParser()
    args.add_argument("--export", required=True)
    args.add_argument("--sdk-python", required=True)
    args.add_argument("--adapter-sha", required=True)
    options = args.parse_args()
    root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(root), str(root / "tests/backend")]
    from fixtures import ENV, bearer, start_http
    from source_fixtures import core_fixture, input_request
    from test_bots import bot_settings
    from services.platform.contracts import canonical, digest, utc
    from services.platform.server import create_app
    from services.platform.service import Platform
    from services.platform.web_external_net import reviewed_pins
    from services.platform.participation.protocol import candidate_ref

    async def main():
        private = root / ".runtime"
        with tempfile.TemporaryDirectory(
            prefix="joint-", dir=private, ignore_cleanup_errors=True
        ) as folder:
            data = Path(folder)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            with (data / "sdk.log").open("w", encoding="utf-8") as log:
                child = subprocess.Popen(
                    [
                        options.sdk_python,
                        str(root / "tests/backend/test_participation_joint_host.py"),
                        "--export",
                        options.export,
                        "--data",
                        str(data / "host"),
                        "--port",
                        str(port),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    cwd=data,
                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                platform = runner = None
                try:
                    async with aiohttp.ClientSession(
                        timeout=aiohttp.ClientTimeout(total=10)
                    ) as http:
                        hosturl = f"http://127.0.0.1:{port}"
                        hostinfo = None
                        for _ in range(100):
                            if child.poll() is not None:
                                raise RuntimeError((data / "sdk.log").read_text(encoding="utf-8"))
                            try:
                                async with http.get(hosturl + "/fixture/info") as response:
                                    if response.status == 200:
                                        hostinfo = await response.json()
                                        break
                            except aiohttp.ClientError:
                                pass
                            await asyncio.sleep(0.1)
                        assert hostinfo and hostinfo["controlled"]

                        async def hostpost(path, body, *, authenticated=True):
                            headers = (
                                {"Authorization": "Bearer " + hostinfo["access_key"]}
                                if authenticated
                                else {}
                            )
                            async with http.post(
                                hosturl + path, json=body, headers=headers
                            ) as response:
                                value = await response.json()
                                assert response.status == 200, (path, response.status, value)
                                return value

                        rpc = "/tianshu/adapter/v1/"
                        await hostpost(
                            rpc + "observation/apply",
                            dict(
                                request_id="apply:joint",
                                account_id="10001",
                                revision=1,
                                enabled=True,
                                group_policy=dict(observe=True, mode="whitelist", list=["20001"]),
                                private_policy=dict(observe=True, mode="observe_only", list=[]),
                            ),
                        )
                        await hostpost(rpc + "observation/poll", dict(account_id="10001", limit=20))
                        (data / "platform").mkdir()
                        settings = bot_settings(str(data / "platform"))
                        settings["bot_adapter_self_service"] = dict(
                            directory=str(data / "adapters"),
                            allowed_cidrs=["127.0.0.0/8"],
                            actors=[dict(id="actor:a", label="synthetic")],
                        )
                        settings["core"] = dict(
                            base_url="https://127.0.0.1:9443", token_env="TS_ADAPTER_CORE_TOKEN"
                        )
                        with patch.dict(
                            os.environ, {**ENV, "TS_ADAPTER_CORE_TOKEN": "synthetic-joint-core"}
                        ):
                            platform = Platform(settings)
                            manager = platform.bot_observation
                            row = dict(
                                kind="observation",
                                id="obs:joint",
                                name="joint",
                                adapter="onebot",
                                address=hosturl,
                                access_key=hostinfo["access_key"],
                                allow_private_http=True,
                                ca_pem=None,
                                pins=await reviewed_pins(hosturl, ["127.0.0.0/8"]),
                                instance_id=hostinfo["instance_id"],
                                account_id="10001",
                                enabled=True,
                                revision=1,
                                host_revision=1,
                                observation_epoch=1,
                                archive_epoch=1,
                                read_enabled=True,
                                state="ready",
                                pending=None,
                                last_error=None,
                                last_checked_at=None,
                                group_policy=dict(
                                    observe=True,
                                    mode="whitelist",
                                    list=["20001"],
                                    actor_id="actor:a",
                                ),
                                private_policy=dict(
                                    observe=True, mode="observe_only", list=[], actor_id=None
                                ),
                            )
                            manager.catalog.put(row)
                            with closing(manager._db()) as db, db:
                                db.execute(
                                    "INSERT INTO observation_versions VALUES(?,?,?,?,?)",
                                    (row["id"], 1, 1, 1, 1),
                                )

                            async def archive(request):
                                return dict(
                                    source_ref=request["source_ref"],
                                    state="accepted",
                                    archive_state="archived",
                                )

                            manager._companion = archive
                            corebindings = {}

                            async def core_management(path, body):
                                if path == "learning-frame":
                                    return dict(
                                        schema_version=1,
                                        valid=True,
                                        frame=dict(
                                            actor_id=body["actor_id"],
                                            audience="group",
                                            conversation_id="conversation:joint",
                                        ),
                                        channel=body["channel"],
                                        delivery_channel=body["delivery_channel"],
                                    )
                                if path == "apply":
                                    saved = {
                                        k: body[k] for k in ("connection_id", "revision", "enabled")
                                    }
                                    corebindings[body["connection_id"]] = saved
                                    return saved
                                saved = corebindings.get(body["connection_id"])
                                return dict(found=saved is not None, binding=saved)

                            platform.bot_adapters._core = core_management
                            runner, url = await start_http(create_app(platform))

                            async def post(path, body, token=None):
                                async with http.post(
                                    url + path,
                                    json=body,
                                    headers={"Authorization": token or bearer("COMPANION")},
                                ) as response:
                                    return response.status, await response.json()

                            answers = {}
                            early = set()
                            early_bindings = {}

                            async def core(config, ingest, contracts):
                                key = ingest["command"]["idempotency_key"]
                                authority = platform.sources.read(
                                    bearer("COMPANION"), input_request(ingest)
                                )
                                response = core_fixture(
                                    ingest, authority, time.time(), previous=answers.get(key)
                                )
                                suffix = digest(ingest["input"])[:12]
                                response["physical_receipt_id"] = "physical:" + suffix
                                response["conversation_id"] = "conversation:joint"
                                for outcome in response["outcomes"]:
                                    receipt, admission = outcome["receipt"], outcome["admission"]
                                    receipt["receipt_id"] = admission["source"]["receipt_id"] = (
                                        "receipt:" + suffix
                                    )
                                    receipt["conversation_id"] = admission["scope"][
                                        "conversation_id"
                                    ] = "conversation:joint"
                                    admission["physical_receipt_id"] = response[
                                        "physical_receipt_id"
                                    ]
                                    receipt["collection_key"] = dict(
                                        channel=ingest["input"]["message_key"]["channel"],
                                        author=ingest["input"]["author"],
                                    )
                                answers[key] = copy.deepcopy(response)
                                with closing(platform.bots._db()) as db:
                                    stored = db.execute(
                                        "SELECT document FROM participation_registrations WHERE json_extract(document,'$.admission.core_idempotency_key')=?",
                                        (key,),
                                    ).fetchone()
                                registration = json.loads(stored[0])
                                native = registration["native"]["native_message_id"]
                                if native in early:
                                    status, value = await post(
                                        "/internal/v2/bot-delivery/send", send_request(registration)
                                    )
                                    assert status == 200, (status, value)
                                    await manager._deliver_replies(row)
                                    snapshot = await hostpost(
                                        "/fixture/snapshot",
                                        dict(id=int(native)),
                                        authenticated=False,
                                    )
                                    assert (
                                        snapshot["bindings"][0]["source_confirmation"] == "pending"
                                    ), snapshot
                                    early_bindings[native] = snapshot["bindings"][0]
                                return response

                            def send_request(registration):
                                native = registration["native"]["native_message_id"]
                                scope = answers[registration["admission"]["core_idempotency_key"]][
                                    "outcomes"
                                ][0]["admission"]["scope"]
                                prepared = platform.sources.participation_prepared(
                                    platform.bots._platform_header(),
                                    canonical(
                                        [
                                            registration["admission"][k]
                                            for k in ("connection_id", "event_id", "account_id")
                                        ]
                                    ),
                                    registration["candidate"],
                                )
                                return dict(
                                    schema_version=2,
                                    request_id="send:" + native,
                                    expression_id="expression:" + native,
                                    origin=dict(
                                        kind="response",
                                        actor_id="actor:a",
                                        turn_id="turn:" + native,
                                        origin={
                                            "assertion_ref": registration["admission"][
                                                "actor_assertion_ref"
                                            ]
                                        },
                                        sources=[],
                                    ),
                                    scope=scope,
                                    channel=prepared["ingest"]["input"]["message_key"]["channel"],
                                    final=True,
                                    segments=[
                                        dict(
                                            segment_id="segment:" + native,
                                            reply_id="reply:" + native,
                                            segment_sequence=1,
                                            text="Synthetic actual sender " + native,
                                            content_refs=[],
                                        )
                                    ],
                                )

                            async def capture(number, **flags):
                                await hostpost(
                                    rpc + "observation/poll", dict(account_id="10001", limit=20)
                                )
                                state = await hostpost(
                                    "/fixture/event", dict(id=number, **flags), authenticated=False
                                )
                                polled = await hostpost(
                                    rpc + "observation/poll", dict(account_id="10001", limit=20)
                                )
                                await manager._process_batch(row, polled["events"])
                                snapshot = await hostpost(
                                    "/fixture/snapshot", dict(id=number), authenticated=False
                                )
                                assert snapshot["acked"], dict(
                                    snapshot=snapshot,
                                    ownership=state,
                                    polled=polled,
                                    platform_error=manager.get(row["id"]).get("last_error"),
                                    participation_error=platform.participation.last_error,
                                )
                                with closing(platform.bots._db()) as db:
                                    found = db.execute(
                                        "SELECT document FROM participation_candidates WHERE json_extract(document,'$.native.native_message_id')=?",
                                        (str(number),),
                                    ).fetchone()
                                assert found, (state, platform.participation.last_error)
                                candidate = json.loads(found[0])
                                return candidate, state["ownership"]

                            def decide(candidate):
                                return dict(
                                    schema_version=1,
                                    kind="semantic_participation_decision",
                                    candidate=candidate_ref(candidate),
                                    decision_id="decision:"
                                    + candidate["native"]["native_message_id"],
                                    revision=1,
                                    outcome="engage",
                                    reason="useful_contribution",
                                    contribution="Synthetic interest",
                                    decided_at=utc(time.time()),
                                    expires_at=candidate["expires_at"],
                                )

                            with patch("services.platform.transport.core_post", side_effect=core):
                                c, ownership = await capture(101)
                                assert (
                                    ownership["state"] == "free"
                                    and ownership["completion_evidence"]
                                )
                                status, value = await post(
                                    "/internal/v1/observation-participation/decision", decide(c)
                                )
                                assert status == 200 and value["state"] == "accepted", (
                                    status,
                                    value,
                                )
                                reg = platform.participation.repository.registration(
                                    c["candidate_id"]
                                )["document"]
                                assert reg["author_anchor"]["stage"] == "registered"
                                status, value = await post(
                                    "/internal/v2/bot-delivery/send", send_request(reg)
                                )
                                assert status == 200, (status, value)
                                await manager._deliver_replies(row)
                                snapshot = await hostpost(
                                    "/fixture/snapshot", dict(id=101), authenticated=False
                                )
                                assert (
                                    len(snapshot["calls"]) == 1
                                    and snapshot["bindings"][0]["source_confirmation"]
                                    == "confirmed"
                                ), snapshot
                                with closing(platform.bots._db()) as db:
                                    assert (
                                        db.execute(
                                            "SELECT state FROM replies WHERE reply_id='reply:101'"
                                        ).fetchone()[0]
                                        == "sent"
                                    )
                                early.add("102")
                                c, _ = await capture(102)
                                status, value = await post(
                                    "/internal/v1/observation-participation/decision", decide(c)
                                )
                                assert status == 200 and value["state"] == "accepted", (
                                    status,
                                    value,
                                )
                                await manager._deliver_replies(row)
                                snapshot = await hostpost(
                                    "/fixture/snapshot", dict(id=102), authenticated=False
                                )
                                assert snapshot["bindings"][0] == {
                                    **early_bindings["102"],
                                    "source_confirmation": "confirmed",
                                }
                                assert len(snapshot["calls"]) == 2
                                c, _ = await capture(103)
                                decision = decide(c)
                                await hostpost(
                                    "/fixture/failure", dict(lose_claim=True), authenticated=False
                                )
                                status, _ = await post(
                                    "/internal/v1/observation-participation/decision", decision
                                )
                                assert status >= 500, status
                                status, value = await post(
                                    "/internal/v1/observation-participation/decision", decision
                                )
                                assert status == 200 and value["state"] == "accepted", (
                                    status,
                                    value,
                                )
                                reg = platform.participation.repository.registration(
                                    c["candidate_id"]
                                )["document"]
                                await hostpost(
                                    "/fixture/failure", dict(lose_send=True), authenticated=False
                                )
                                status, value = await post(
                                    "/internal/v2/bot-delivery/send", send_request(reg)
                                )
                                assert status == 200, (status, value)
                                await manager._deliver_replies(row)
                                await manager._deliver_replies(row)
                                snapshot = await hostpost(
                                    "/fixture/snapshot", dict(id=103), authenticated=False
                                )
                                assert len(snapshot["calls"]) == 3, snapshot
                                status, value = await post(
                                    "/internal/v2/bot-delivery/query",
                                    dict(
                                        schema_version=2,
                                        request_id="query:103",
                                        expression_id="expression:103",
                                    ),
                                )
                                assert status == 200 and value["receipt"]["state"] == "unknown", (
                                    status,
                                    value,
                                )
                                for number, flag, expected in [
                                    (104, "foreign", "handled_elsewhere"),
                                    (105, "unknown", "ownership_unknown"),
                                ]:
                                    c, ownership = await capture(number, **{flag: True})
                                    assert ownership["state"] == expected, ownership
                                    status, value = await post(
                                        "/internal/v1/observation-participation/decision", decide(c)
                                    )
                                    assert status == 200 and value["state"] == "unavailable", (
                                        status,
                                        value,
                                    )
                            print(
                                json.dumps(
                                    dict(
                                        platform_base_commit=subprocess.check_output(
                                            ["git", "-C", str(root), "rev-parse", "HEAD"], text=True
                                        ).strip(),
                                        platform_runtime_dirty=subprocess.run(
                                            [
                                                "git",
                                                "-C",
                                                str(root),
                                                "diff",
                                                "--quiet",
                                                "HEAD",
                                                "--",
                                                "services/platform",
                                            ]
                                        ).returncode
                                        != 0,
                                        platform_runtime_sha256=digest(
                                            {
                                                str(path.relative_to(root)).replace(
                                                    "\\", "/"
                                                ): hashlib.sha256(
                                                    path.read_text(encoding="utf-8-sig").encode()
                                                ).hexdigest()
                                                for path in sorted(
                                                    (root / "services/platform").rglob("*.py")
                                                )
                                            }
                                        ),
                                        adapter_commit=options.adapter_sha,
                                        checks=[
                                            "actual_controlled_sdk_free",
                                            "archive_ack_before_claim",
                                            "platform_source_registration",
                                            "first_author_resolved",
                                            "actual_sender_sent_receipt",
                                            "early_binding_pending_confirmed",
                                            "lost_claim_reply_replay",
                                            "unknown_sdk_send_not_repeated",
                                            "foreign_matcher_handled",
                                            "unknown_host_graph_silent",
                                        ],
                                        synthetic=[
                                            "Core ingest/management/archive peer",
                                            "QQ API return/unknown",
                                        ],
                                        sdk_calls=3,
                                    ),
                                    ensure_ascii=False,
                                )
                            )
                finally:
                    if runner:
                        await runner.cleanup()
                    if platform:
                        platform.close()
                    try:
                        async with aiohttp.ClientSession() as shutdown_http:
                            async with shutdown_http.post(
                                f"http://127.0.0.1:{port}/fixture/shutdown", json={}
                            ) as shutdown_response:
                                await shutdown_response.read()
                    except aiohttp.ClientError:
                        pass
                    try:
                        await asyncio.to_thread(child.wait, timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                    (private / "ts321-joint-sdk.log").write_text(
                        (data / "sdk.log").read_text(encoding="utf-8"), encoding="utf-8"
                    )

    asyncio.run(main())


if __name__ == "__main__":
    run()
