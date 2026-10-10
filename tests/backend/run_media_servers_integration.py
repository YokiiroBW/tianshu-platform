"""Observe locally installed Emby/Jellyfin using generated fixture accounts and libraries.

The two JSON inputs are local runtime artifacts, never production configuration or credentials.
"""

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from services.platform.media.engine import file_record
from services.platform.media.publication import MediaServers, Remote


async def verify(fixtures, documents):
    peers = json.loads(Path(fixtures).read_bytes())
    source = json.loads(Path(documents).read_bytes())
    remote = Remote()
    results = []
    try:
        for fixture in source:
            directory = Path(fixture["directory"])
            records = []
            for file in directory.rglob("*"):
                if not file.is_file():
                    continue
                path = file.relative_to(directory).as_posix()
                cid = next(
                    (cid for cid in fixture["parts"] if "cid-" + cid + "." in path),
                    fixture["parts"][0]
                    if file.suffix == ".mp4" and fixture["mode"] == "single"
                    else None,
                )
                kind = (
                    "source"
                    if file.suffix == ".json"
                    else "nfo"
                    if file.suffix == ".nfo"
                    else "video"
                    if file.suffix == ".mp4"
                    else "episode_thumb"
                    if "-thumb." in path
                    else "poster"
                )
                records.append(file_record(file, path, kind, cid))
            manifest = {"layout": fixture["mode"], "files": records}
            job = {
                "bvid": fixture["snapshot"]["bvid"],
                "snapshot": fixture["snapshot"],
                "selected_cids": fixture["parts"],
                "overrides": {},
                "cover_present": True,
                "asset_receipt": {"target_relative_path": directory.name},
                "package": {
                    "staging_ref": directory.name,
                    "manifest_base64": base64.b64encode(json.dumps(manifest).encode()).decode(),
                },
                "staging_directory": str(directory.parent),
            }
            servers = []
            for kind, peer in peers.items():
                env_name = "SYNTHETIC_MEDIA_" + kind.upper()
                os.environ[env_name] = peer["token"]
                servers.append(
                    {
                        "server_id": kind,
                        "kind": kind,
                        "base_url": peer["base_url"],
                        "token_env": env_name,
                        **peer["libraries"][fixture["mode"]],
                    }
                )
            observed = await MediaServers(remote).verify({"servers": servers}, job)
            results += [{"layout": fixture["mode"], **value} for value in observed]
        print(
            json.dumps(
                {
                    "results": results,
                    "verified": all(result["state"] == "verified" for result in results),
                },
                ensure_ascii=False,
            )
        )
        return 0 if all(result["state"] == "verified" for result in results) else 1
    finally:
        await remote.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixtures", required=True)
    parser.add_argument("--documents", required=True)
    arguments = parser.parse_args()
    sys.exit(asyncio.run(verify(arguments.fixtures, arguments.documents)))
