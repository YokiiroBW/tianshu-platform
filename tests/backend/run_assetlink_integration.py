"""Explicit isolated Platform -> actual AssetLibrary HTTPS/PG acceptance; no live DB input."""

import argparse
import asyncio
import importlib.util
import json
import os
import queue
import ssl
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parents[2]
REVISION = "ff8e8a1fc405870d5a82e44cc344c9e083df059a"
sys.path.insert(0, str(ROOT))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Bridge:
    def __init__(self, dotnet, dll, environment, log):
        self.process = subprocess.Popen(
            [str(dotnet), str(dll)],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self.responses = queue.Queue()

        def collect():
            with log.open("w", encoding="utf-8") as output:
                for line in self.process.stdout:
                    if line.startswith("TS064:"):
                        self.responses.put(
                            json.loads(line[6:])
                        )  # secrets remain in the pipe/memory
                    else:
                        output.write(line)
            self.responses.put(None)

        self.thread = threading.Thread(target=collect, daemon=True)
        self.thread.start()

    def receive(self):
        result = self.responses.get(timeout=90)
        if result is None:
            raise RuntimeError("AssetLibrary fixture stopped unexpectedly; see private runtime log")
        return result

    def command(self, action, principal=0, fields=None):
        self.process.stdin.write(
            json.dumps({"action": action, "principal": principal, "fields": fields}) + "\n"
        )
        self.process.stdin.flush()
        return self.receive()

    def close(self):
        try:
            if self.process.poll() is None:
                result = self.command("stop")
                assert result == {"stopped": True, "originals_unchanged": True}
            assert self.process.wait(timeout=20) == 0
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            self.thread.join(timeout=10)
            self.process.stdin.close()
            self.process.stdout.close()


async def verify(bridge, ready, runtime):
    import aiohttp

    from fixtures import ENV, bearer
    from test_assets import asset_settings
    from services.platform.service import Platform

    os.environ.update(ENV)
    for index, suffix in enumerate(("A", "B")):
        os.environ["TS064_ASSET_" + suffix] = ready["credentials"][index]["token"]
    config = asset_settings(str(runtime), ready["endpoint"], ready["ca_file"])
    config["principals"]["companion"].update(
        actions=["asset.read"], asset_connections=["library-b"]
    )
    platform = Platform(config)
    libraries = ready["libraries"]
    checks = []
    observations = []

    async def read(operation, body=None, index=0, status=200):
        result = await platform.assets.read(
            bearer("READER" if index == 0 else "COMPANION"),
            {
                "connection_id": "library-a" if index == 0 else "library-b",
                "operation": operation,
                "body": body or {},
            },
        )
        assert result["status"] == status, {k: v for k, v in result.items() if k != "body"}
        assert result["request_id"] != "unknown"
        observations.append(
            {
                "operation": operation,
                "status": status,
                "request_id": result["request_id"],
                "upstream_request_id": result["upstream_request_id"],
            }
        )
        if status == 200:
            assert result["request_id"] == result["upstream_request_id"]
            assert result["original_available"] == "not_verified"
        else:
            assert result["body"] is None and result["clear_previous"]
        return result["body"]

    for index in (0, 1):
        listed = await read("libraries.list", index=index)
        assert [item["library_id"] for item in listed["items"]] == [libraries[index]]
        await read("libraries.get", {"library_id": libraries[1 - index]}, index, 404)
    checks.append("two_independent_service_scopes_cross_library_denial")
    await read("libraries.get", {"library_id": libraries[0]})
    browse = {
        "library_id": libraries[0],
        "parent_relative_path": "",
        "kind": "files",
        "page_size": 100,
    }
    first = await read("entries.browse", browse)
    cursor = first["next_cursor"]
    second = await read("entries.browse", {**browse, "cursor": cursor})
    assert len(first["items"]) == 100 and len(second["items"]) == 37
    assert len({item["entry_id"] for item in first["items"] + second["items"]}) == 137
    detail = {"library_id": libraries[0], "entry_id": first["items"][0]["entry_id"]}
    await read("entries.get", detail)
    search = {
        "scope": "library",
        "library_id": libraries[0],
        "query": "说明_中文",
        "page_size": 100,
    }
    assert len((await read("assets.search", search))["items"]) == 1
    await read("entries.browse", {**browse, "cursor": cursor, "sort_direction": "desc"}, status=400)
    await read(
        "entries.browse",
        {**browse, "library_id": libraries[1], "cursor": cursor},
        index=1,
        status=400,
    )
    assert (
        len(
            (await read("assets.search", {"query": "第二库_中文", "page_size": 100}, index=1))[
                "items"
            ]
        )
        == 1
    )
    assert not (await read("assets.search", {"query": "第二库_中文", "page_size": 100}))["items"]
    checks.append("five_reads_100_plus_37_chinese_paging_cursor_scope")

    # Actual CLI authenticates its own principal and invokes the actual HTTPS client.
    configuration = runtime / "platform-settings.json"
    configuration.write_text(json.dumps(config), encoding="utf-8")
    request = runtime / "platform-request.json"
    request.write_text(
        json.dumps({"connection_id": "library-a", "operation": "libraries.list", "body": {}}),
        encoding="utf-8",
    )
    cli = await asyncio.to_thread(
        subprocess.run,
        [
            sys.executable,
            "-m",
            "services.platform",
            "--settings",
            str(configuration),
            "local",
            "--credential-env",
            "TS012_READER",
            "asset-read",
            "--input",
            str(request),
        ],
        cwd=ROOT,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert cli.returncode == 0 and json.loads(cli.stdout)["ok"]
    checks.append("authenticated_cli_real_https")

    # Explicit negative upstream probes, separate from the platform's restricted API.
    tls = ssl.create_default_context(cafile=ready["ca_file"])
    async with aiohttp.ClientSession(
        cookie_jar=aiohttp.DummyCookieJar(), trust_env=False, timeout=aiohttp.ClientTimeout(total=5)
    ) as session:
        for extra in (
            {"Cookie": ""},
            {"Origin": ""},
            {"Sec-Fetch-Site": "same-origin"},
            {"X-AssetLibrary-CSRF": ""},
        ):
            async with session.post(
                ready["endpoint"],
                ssl=tls,
                allow_redirects=False,
                headers={"Authorization": "Bearer " + os.environ["TS064_ASSET_A"], **extra},
                json={
                    "message_type": "control.request",
                    "request_id": str(uuid.uuid4()),
                    "operation": "libraries.list",
                    "body": {},
                },
            ) as response:
                assert response.status == 403
        async with session.post(
            ready["endpoint"],
            ssl=tls,
            allow_redirects=False,
            headers={
                "Authorization": "Bearer " + os.environ["TS064_ASSET_A"],
                "Content-Type": "application/json",
            },
            data=b" " * 65537,
        ) as response:
            assert response.status == 413
    checks.append("actual_host_mixed_identity_and_64k_denial")

    offline = await asyncio.to_thread(bridge.command, "source-offline")
    assert offline["availability"] == "offline"
    snapshot = await read("entries.browse", browse)
    assert snapshot["library"]["availability"] == "offline"
    assert len(snapshot["items"]) == 100  # authorized index only, never original availability
    await asyncio.to_thread(bridge.command, "source-online")
    await asyncio.to_thread(bridge.command, "pause")
    await read("entries.get", detail, status=503)
    await asyncio.to_thread(bridge.command, "restart")
    await read("libraries.get", {"library_id": libraries[0]})
    checks.append("source_offline_index_label_network_offline_clears_restart_reconnect")
    await asyncio.to_thread(bridge.command, "service-ungrant", 0, {"library_ids": [libraries[0]]})
    for operation, body in (
        ("entries.browse", {**browse, "cursor": cursor}),
        ("entries.get", detail),
        ("libraries.get", {"library_id": libraries[0]}),
        ("assets.search", search),
    ):
        await read(operation, body, status=404)
    assert not (await read("libraries.list"))["items"]
    assert not (await read("assets.search", {"query": "说明_中文"}))["items"]
    await read("libraries.get", {"library_id": libraries[1]}, index=1)
    checks.append("committed_library_revocation_next_page_get_list_search")
    await asyncio.to_thread(bridge.command, "service-grant", 0, {"library_ids": [libraries[0]]})
    await read("entries.get", detail)
    await asyncio.to_thread(
        bridge.command,
        "service-revoke",
        0,
        {"credential_id": ready["credentials"][0]["credential_id"]},
    )
    for operation, body in (
        ("entries.browse", {**browse, "cursor": cursor}),
        ("entries.get", detail),
        ("libraries.list", {}),
        ("assets.search", search),
    ):
        await read(operation, body, status=401)
    await asyncio.to_thread(bridge.command, "restart")
    await read("libraries.list", status=401)
    await read("libraries.list", index=1)
    replacement = await asyncio.to_thread(bridge.command, "service-issue", 0)
    os.environ["TS064_ASSET_A"] = replacement["token"]
    await read("entries.get", detail)
    await asyncio.to_thread(bridge.command, "service-disable", 0)
    await read("libraries.list", status=401)
    checks.append("service_revocation_all_reads_persists_restart_explicit_new_credential")
    return checks, observations


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-repo", type=Path, required=True, help="read-only Git source")
    parser.add_argument("--dotnet", type=Path, required=True)
    parser.add_argument("--postgres-bin", type=Path, required=True)
    args = parser.parse_args()
    run = ROOT / ".runtime" / ("ts064-" + uuid.uuid4().hex[:10])
    run.mkdir(parents=True)
    snapshot = ROOT / ".runtime/assetlibrary"
    archive = run / "source.zip"
    subprocess.run(
        [
            "git",
            "-C",
            str(args.asset_repo),
            "archive",
            "--format=zip",
            "--output=" + str(archive),
            REVISION,
        ],
        check=True,
        capture_output=True,
    )
    with zipfile.ZipFile(archive) as exported:
        if not snapshot.exists():
            exported.extractall(snapshot)
        # Reuse build caches only if every tracked source byte matches the fixed export.
        for item in exported.infolist():
            if not item.is_dir():
                assert (snapshot / item.filename).read_bytes() == exported.read(item), item.filename
    environment = {
        **os.environ,
        "DOTNET_CLI_HOME": str(ROOT / ".runtime/dotnet-home"),
        "NUGET_PACKAGES": str(ROOT / ".runtime/nuget"),
        "DOTNET_GENERATE_ASPNET_CERTIFICATE": "false",
        "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
    }
    probe = ROOT / ".runtime/assetlink-probe"
    probe.mkdir(exist_ok=True)
    project = probe / "Probe.csproj"
    project.write_text(
        f'''<Project Sdk="Microsoft.NET.Sdk">
<PropertyGroup><TargetFramework>net10.0</TargetFramework><OutputType>Exe</OutputType><ImplicitUsings>enable</ImplicitUsings><Nullable>enable</Nullable></PropertyGroup>
<ItemGroup><ProjectReference Include="{escape(str(snapshot / "tests/dotnet/AssetLibrary.WebGateway.Tests/AssetLibrary.WebGateway.Tests.csproj"))}" />
<Compile Include="{escape(str(ROOT / "tests/backend/assetlink_probe/Program.cs"))}" Link="Program.cs" /></ItemGroup></Project>''',
        encoding="utf-8",
    )
    built = subprocess.run(
        [str(args.dotnet), "build", str(project), "--configuration", "Release"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    (run / "build.log").write_text(built.stdout + built.stderr, encoding="utf-8")
    if built.returncode:
        raise RuntimeError("Probe build failed: " + str(run / "build.log"))
    module = load("ts064_trial", snapshot / "tests/integration/read-only-trial/run_e2e.py")
    database = module.load_fixture()
    with tempfile.TemporaryDirectory(prefix="al20-ts064-") as temporary:
        runtime = Path(temporary)
        web = runtime / "web"
        web.mkdir()
        (web / "index.html").write_text(
            "<!doctype html><title>TS064 API fixture</title>", encoding="utf-8"
        )
        os.environ.update(
            {
                "ASSETLIBRARY_TEST_POSTGRES_REQUIRED": "1",
                "ASSETLIBRARY_TEST_POSTGRES_EXTERNAL": "0",
                "ASSETLIBRARY_TEST_POSTGRES_BIN": str(args.postgres_bin.resolve()),
                "ASSETLIBRARY_TEST_RUNTIME": str(runtime),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        fixture_type = database.PostgreSqlIntegrationTests
        fixture = fixture_type(methodName="runTest")
        logins = []
        bridge = None
        try:
            fixture_type.setUpClass()
            settings = module.prepare_database(database, fixture, logins)
            settings.update(
                {
                    "runtime_root": str(runtime),
                    "web_root": str(web),
                    "node": "unused",
                    "playwright_module": "unused",
                    "browser_script": "unused",
                    "evidence": str(run),
                    "dotnet": str(args.dotnet.resolve()),
                    "host_dll": str(
                        snapshot
                        / "services/core-server/Host/bin/Release/net10.0/AssetLibrary.CoreServer.Host.dll"
                    ),
                }
            )
            configuration = runtime / "settings.json"
            configuration.write_text(json.dumps(settings), encoding="utf-8")
            environment.update(
                {
                    "ASSETLIBRARY_TRIAL_E2E_REQUIRED": "1",
                    "ASSETLIBRARY_TRIAL_E2E_SETTINGS": str(configuration),
                }
            )
            bridge = Bridge(
                args.dotnet, probe / "bin/Release/net10.0/Probe.dll", environment, run / "host.log"
            )
            ready = bridge.receive()
            checks, observations = asyncio.run(verify(bridge, ready, runtime))
        finally:
            try:
                if bridge:
                    bridge.close()
            finally:
                cleaned = fixture.doCleanups()
                try:
                    for login in logins:
                        fixture.sql("postgres", fixture.admin, f"DROP ROLE {login};")
                finally:
                    fixture_type.doClassCleanups()
                if not cleaned or getattr(fixture_type, "tearDown_exceptions", []):
                    raise RuntimeError("Owned PG cleanup failed")
    # Source snapshot integrity is rechecked after all fixture/build activity.
    with zipfile.ZipFile(archive) as exported:
        for item in exported.infolist():
            if not item.is_dir():
                assert (snapshot / item.filename).read_bytes() == exported.read(item), item.filename
    result = {
        "status": "passed",
        "assetlibrary_revision": REVISION,
        "checks": checks,
        "observations": observations,
        "resource_cleanup": "verified",
        "originals": "unchanged",
        "assetlibrary_source": "unchanged",
        "boundary": "real PG and HTTPS, synthetic originals and HIBP responder; no browser/NAS/500k/production",
    }
    (run / "acceptance.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {"status": "passed", "evidence": str(run / "acceptance.json"), "checks": len(checks)}
        )
    )


if __name__ == "__main__":
    main()
