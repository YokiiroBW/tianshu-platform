"""Real Platform/AssetLibrary/Emby/Jellyfin, synthetic Bilibili and FFmpeg input."""

# This standalone runner adds the repository and existing test fixtures before imports.
# ruff: noqa: E402
import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO), str(REPO / "tests/backend")]

import aiohttp
from fixtures import ENV, start_http
from media_runtime_fixtures import QUALITY, SyntheticEngine, Upstream, settings
from services.platform.media.bilibili import Bilibili
from services.platform.server import create_app
from services.platform.service import Platform
from web_fixtures import PASSWORD


async def main(args):
    root = Path(args.work_directory).resolve()
    root.mkdir(parents=True, exist_ok=True)
    bvid = args.bvid
    asset = json.loads(Path(args.asset).read_text(encoding="utf-8-sig"))
    servers = json.loads(Path(args.servers).read_text(encoding="utf-8-sig"))
    run = root / ("joint-" + uuid.uuid4().hex[:8])
    run.mkdir()
    os.environ.update(ENV)
    os.environ["SYNTHETIC_MEDIA_PUBLISH"] = asset["token"]
    upstream = await Upstream().start()
    upstream.parts = {bvid: ["111111111", "222222222"]}
    upstream.members = [bvid]
    upstream.cover_enabled = True
    engine = {"python": sys.executable, "ffmpeg": args.ffmpeg, "ffprobe": args.ffprobe}
    config = settings(run, engine, asset["base_url"])
    config["media"]["staging_directory"] = asset["staging_directory"]
    target = config["media"]["targets"][0]
    target["library_id"] = asset["library_id"]
    if asset.get("ca_file"):
        target["publisher"]["ca_file"] = asset["ca_file"]
    async with aiohttp.ClientSession() as http:
        for kind, server in servers.items():
            headers = {
                "Authorization": f'MediaBrowser Client="TianshuJoint", Device="Synthetic", DeviceId="joint-fixture", Version="1.0", Token="{server["token"]}"',
                "X-Emby-Token": server["token"],
            }
            name = "Tianshu asset joint " + run.name
            options = {
                "EnableRealtimeMonitor": False,
                "EnableInternetProviders": False,
                "PathInfos": [{"Path": asset["library_directory"]}],
                "MetadataOptions": [
                    {"ItemType": item, "MetadataFetchers": [], "ImageFetchers": []}
                    for item in ["Series", "Season", "Episode", "Person"]
                ],
            }
            async with http.post(
                server["base_url"] + "/Library/VirtualFolders",
                params={"name": name, "collectionType": "tvshows", "refreshLibrary": "true"},
                json={"LibraryOptions": options},
                headers=headers,
            ) as response:
                assert response.status in {200, 204}, (kind, response.status)
            async with http.get(
                server["base_url"] + "/Library/VirtualFolders", headers=headers
            ) as response:
                folders = await response.json()
            folder = next(value for value in folders if value["Name"] == name)
            token_env = "SYNTHETIC_JOINT_" + kind.upper()
            os.environ[token_env] = server["token"]
            target["servers"].append(
                {
                    "server_id": kind,
                    "kind": kind,
                    "base_url": server["base_url"],
                    "token_env": token_env,
                    "library_id": folder["ItemId"],
                    "media_root": asset["library_directory"],
                }
            )
    platform = Platform(config)
    platform.media.connector = Bilibili(api=upstream.url, passport=upstream.url)
    platform.media.engine = SyntheticEngine(platform.media.config["engine"])

    async def synthetic_cover(url):
        return upstream.cover_bytes, "png"

    platform.media.worker.cover = synthetic_cover
    runner, url = await start_http(create_app(platform))
    config["web"]["origin"] = url
    evidence = {
        "synthetic_upstream": True,
        "real_services": [
            "Platform HTTP/SQLite",
            "AssetLibrary HTTP/PostgreSQL/filesystem",
            "Emby",
            "Jellyfin",
        ],
        "run_directory": str(run),
    }
    try:
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as client:
            async with client.get(url + "/api/web/session") as response:
                csrf = (await response.json())["csrf"]
            async with client.post(
                url + "/api/web/login",
                json={"username": "synthetic-admin", "password": PASSWORD},
                headers={"Origin": url, "X-CSRF-Token": csrf},
            ) as response:
                assert response.status == 200
                csrf = (await response.json())["csrf"]

            async def post(operation, body):
                async with client.post(
                    url + "/api/web/media/" + operation,
                    json=body,
                    headers={"Origin": url, "X-CSRF-Token": csrf},
                ) as response:
                    result = await response.json()
                    assert response.status == 200, (operation, response.status, result)
                    return result

            preview = await post(
                "resolve", {"url": "https://www.bilibili.com/video/" + bvid, "account_id": None}
            )
            result = await post(
                "enqueue",
                {
                    "preview_id": preview["preview_id"],
                    "part_cids": upstream.parts[bvid],
                    "account_id": None,
                    "target_id": target["target_id"],
                    "quality": QUALITY,
                    "client_id": str(uuid.uuid4()),
                },
            )
            identity = result["jobs"][0]["job_id"]
            previous = None
            end = time.monotonic() + 180
            while time.monotonic() < end:
                result = await post("jobs/detail", {"job_id": identity})
                job = result["job"]
                state = (job["state"], job["code"])
                if state != previous:
                    print(json.dumps({"state": state}, ensure_ascii=True), flush=True)
                    previous = state
                if job["state"] == "completed":
                    evidence["job"] = job
                    assert all(value["state"] == "verified" for value in job["library_results"])
                    assert (
                        len(
                            [
                                entry
                                for entry in job["asset_receipt"]["files"]
                                if entry["kind"] == "video"
                            ]
                        )
                        == 2
                    )
                    assert all(entry["entry_id"] for entry in job["asset_receipt"]["files"])
                    evidence["result"] = "passed"
                    break
                if job["state"] in {"failed", "unknown", "cancelled", "auth_required"}:
                    evidence["job"] = job
                    raise AssertionError(state)
                await asyncio.sleep(2)
            else:
                evidence["job"] = job
                raise AssertionError("joint_completion_timeout")
    except Exception as error:
        evidence["result"] = "failed"
        evidence["error_type"] = type(error).__name__
        raise
    finally:
        (run / "evidence.json").write_text(
            json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print("Evidence: " + str(run / "evidence.json"), flush=True)
        await runner.cleanup()
        await upstream.runner.cleanup()
        platform.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--servers", required=True)
    parser.add_argument("--work-directory", required=True)
    parser.add_argument("--ffmpeg", required=True)
    parser.add_argument("--ffprobe", required=True)
    parser.add_argument(
        "--bvid",
        default="BV1Zx411c7mD",
        help="Synthetic identity; use a fresh isolated library or unused identity for each run.",
    )
    asyncio.run(main(parser.parse_args()))
