"""Independent roles progress while each role retains one staged owner."""

import asyncio
from contextlib import closing
import threading
import uuid

from test_role_runtime import body, fixture

from services.platform.contracts import Fault, canonical
from services.platform.local_work import LocalWork
from services.platform.role_runtime import RoleRuntime


def actor_id(request):
    return "actor:role-" + request["client_id"].replace("-", "")


async def lock_users(manager, actor, count):
    async def wait():
        while manager._actor_locks[actor][1] < count:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait(), 2)


def test_cancelled_role_owner_keeps_lock_until_started_sqlite_write_finishes(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        pool = LocalWork(workers=2, capacity=2)
        platform.local_work = pool
        entered, release = threading.Event(), threading.Event()
        request = body("provider-a")
        actor = actor_id(request)
        put = manager._put
        writes = []

        def blocking_put(row):
            if row["stage"] != "core_paused":
                return put(row)
            with closing(manager._db()) as db, db:
                db.execute("BEGIN IMMEDIATE")
                db.execute("UPDATE roles SET body=? WHERE actor_id=?", (canonical(row), actor))
                writes.append(row["stage"])
                entered.set()
                release.wait(5)

        manager._put = blocking_put
        owner = asyncio.create_task(manager.route(console, "/api/web/roles/apply", request, {}))
        tasks = [owner]
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            owner.cancel("first owner cancellation")
            await asyncio.sleep(0)
            owner.cancel("second owner cancellation")
            await asyncio.sleep(0)
            assert not owner.done() and manager._actor_locks[actor][1] == 1
            retry = asyncio.create_task(manager.route(console, "/api/web/roles/apply", request, {}))
            tasks.append(retry)
            await lock_users(manager, actor, 2)
            assert not retry.done() and actor not in peers.grants
            release.set()
            try:
                await owner
            except asyncio.CancelledError as error:
                assert error.args == ("first owner cancellation",)
            else:
                raise AssertionError("Cancellation must survive settling the existing transaction")
            result = await retry
            assert result["role"]["state"] == "active"
            assert writes == ["core_paused"]
            assert peers.roles[actor]["version"] == 2 and peers.grants[actor]["version"] == 1
            assert pool.active == 0 and manager._actor_locks == {}
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            pool.close()

    asyncio.run(scenario())


def test_roles_progress_independently_and_same_role_waiters_share_one_lock(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        pool = LocalWork()
        platform.local_work = pool
        entered, release = asyncio.Event(), asyncio.Event()
        first, second = body("provider-a"), body("provider-b")
        actor = actor_id(first)

        async def remote(settings, path, payload):
            if payload.get("request_id") == first["client_id"] + ":pause":
                entered.set()
                await release.wait()
            return await peers.call(settings, path, payload)

        manager._remote = remote
        tasks = []
        try:
            tasks.append(
                asyncio.create_task(manager.route(console, "/api/web/roles/apply", first, {}))
            )
            await asyncio.wait_for(entered.wait(), 2)
            entry = manager._actor_locks[actor]
            for _ in range(2):
                tasks.append(
                    asyncio.create_task(manager.route(console, "/api/web/roles/apply", first, {}))
                )
            await lock_users(manager, actor, 3)
            tasks[-1].cancel()
            await asyncio.gather(tasks[-1], return_exceptions=True)
            assert manager._actor_locks[actor] is entry and entry[1] == 2
            other = await asyncio.wait_for(
                manager.route(console, "/api/web/roles/apply", second, {}), 2
            )
            assert other["role"]["state"] == "active"
            assert not tasks[0].done() and not tasks[1].done()
            release.set()
            original, replay = await asyncio.gather(*tasks[:2])
            assert original == replay
            assert original["role"]["state"] == "active"
            assert peers.roles[actor]["version"] == 2
            assert peers.grants[actor]["version"] == 1
            assert len(console.input_entries) == 3
            assert len(platform.auth.entries) == 3
            assert manager._actor_locks == {}
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            pool.close()

    asyncio.run(scenario())


def test_retry_cancel_and_background_recovery_keep_one_role_stage_order(tmp_path):
    async def scenario():
        platform, manager, console, peers = fixture(tmp_path)
        request = body("provider-a")

        async def lose_memory_receipt(settings, path, payload):
            answer = await peers.call(settings, path, payload)
            if payload.get("request_id") == request["client_id"] + ":memory":
                raise Fault("dependency_unavailable", 503)
            return answer

        manager._remote = lose_memory_receipt
        pending = (await manager.route(console, "/api/web/roles/apply", request, {}))["role"]
        actor = pending["actor_id"]
        assert pending["stage"] == "core_paused" and peers.grants[actor]["enabled"]
        entered, release = asyncio.Event(), asyncio.Event()

        async def retry_receipt(settings, path, payload):
            if payload.get("request_id") == request["client_id"] + ":memory":
                entered.set()
                await release.wait()
            return await lose_memory_receipt(settings, path, payload)

        manager._remote = retry_receipt
        retry = asyncio.create_task(
            manager.route(
                console,
                "/api/web/roles/retry",
                {"actor_id": actor, "client_id": request["client_id"]},
                {},
            )
        )
        tasks = [retry]
        try:
            await asyncio.wait_for(entered.wait(), 2)
            cancel = asyncio.create_task(
                manager.route(
                    console,
                    "/api/web/roles/cancel",
                    {"actor_id": actor, "client_id": str(uuid.uuid4()), "expected_version": 1},
                    {},
                )
            )
            tasks.append(cancel)
            await lock_users(manager, actor, 2)
            background = asyncio.create_task(manager.resume_pending())
            tasks.append(background)
            await lock_users(manager, actor, 3)
            release.set()
            retried, cancelled, _ = await asyncio.gather(*tasks)
            assert retried["role"]["state"] == "pending"
            assert cancelled["role"]["state"] == "disabled"
            assert manager.get(actor) == cancelled["role"]
            assert not peers.roles[actor]["enabled"] and not peers.grants[actor]["enabled"]
            assert peers.roles[actor]["version"] == peers.grants[actor]["version"] == 2
            assert manager._actor_locks == {}
            restarted = RoleRuntime(platform)
            restarted.console, restarted._remote = console, peers.call
            await restarted.resume_pending()
            assert restarted.get(actor) == cancelled["role"]
            assert restarted._actor_locks == {}
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_background_rereads_cancelled_role_after_waiting_for_its_owner(tmp_path):
    async def scenario():
        _, manager, console, peers = fixture(tmp_path)
        request = body("provider-a")
        row = manager._begin(request)
        actor = row["actor_id"]
        snapshot_read = asyncio.Event()
        directory = manager.directory
        calls = []

        def read_directory():
            rows = directory()
            snapshot_read.set()
            return rows

        async def remote(settings, path, payload):
            calls.append(payload)
            return await peers.call(settings, path, payload)

        manager.directory = read_directory
        manager._remote = remote
        async with manager._actor_lock(actor):
            background = asyncio.create_task(manager.resume_pending())
            await snapshot_read.wait()
            manager._begin_cancel(
                {"actor_id": actor, "expected_version": 1, "client_id": str(uuid.uuid4())}
            )
        await background
        assert manager.get(actor)["state"] == "disabled"
        assert actor not in peers.roles
        assert peers.grants[actor]["enabled"] is False
        assert not any(
            item.get("request_id", "").startswith(request["client_id"]) for item in calls
        )
        try:
            await manager.route(
                console,
                "/api/web/roles/retry",
                {"actor_id": actor, "client_id": request["client_id"]},
                {},
            )
        except Fault as error:
            assert error.code == "forbidden"
        else:
            raise AssertionError("The old application must not resume after cancellation")
        assert manager._actor_locks == {}

    asyncio.run(scenario())


def test_background_roles_share_existing_bounded_local_admission_budget(tmp_path):
    async def scenario():
        platform, manager, _, peers = fixture(tmp_path)
        platform.local_work.capacity = 2
        for _ in range(5):
            manager._begin(body("provider-a"))
        entered, release = asyncio.Event(), asyncio.Event()
        active = maximum = 0

        async def remote(settings, path, payload):
            nonlocal active, maximum
            if payload.get("request_id", "").endswith(":pause"):
                active += 1
                maximum = max(maximum, active)
                if active == 2:
                    entered.set()
                try:
                    await release.wait()
                finally:
                    active -= 1
            return await peers.call(settings, path, payload)

        manager._remote = remote
        background = asyncio.create_task(manager.resume_pending())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            assert maximum == 2
            assert len(manager._actor_locks) == 2
        finally:
            release.set()
            await background
        assert maximum == 2
        assert len(manager.active_actors()) == 5
        assert manager._actor_locks == {}

    asyncio.run(scenario())
