"""Isolated recorded Bilibili HTTP, synthetic media and publication observations."""

import base64
import copy
import hashlib
import json
import uuid
from io import BytesIO
from pathlib import Path

from aiohttp import web
from PIL import Image

from fixtures import CONTRACT, start_http
from services.platform.media.config import EMPTY_RULES
from services.platform.media.engine import Engine, run_process
from web_fixtures import web_settings

BV = "BV1xx411c7mD"
OTHER_BV = "BV1Ab4y1z7Qs"
QUALITY = {"mode": "best", "quality_id": None, "allow_fallback": False}


class Upstream:
    def __init__(self):
        self.members = [BV]
        self.parts = {BV: ["111111111", "222222222"], OTHER_BV: ["333333333"]}
        self.description = "完整合成简介"
        self.anonymous_nav = True
        self.fail_page = False
        self.cookie_seen = []
        self.qr_state = "waiting"
        self.cover_enabled = False
        output = BytesIO()
        Image.new("RGB", (320, 180), (20, 90, 170)).save(output, "PNG")
        self.cover_bytes = output.getvalue()

    async def handle(self, request):
        if request.path == "/__fixture/control":
            body = await request.json()
            if "members" in body:
                self.members = body["members"]
            if "parts" in body:
                self.parts.update(body["parts"])
            self.qr_state = body.get("qr_state", self.qr_state)
            self.description = body.get("description", self.description)
            self.cover_enabled = body.get("cover_enabled", self.cover_enabled)
            return web.json_response(
                {"fixture": True, "members": self.members, "qr_state": self.qr_state}
            )
        self.cookie_seen.append((request.path, request.headers.get("Cookie", "")))
        code = 0
        path = request.path
        if path == "/x/web-interface/nav":
            logged = "SESSDATA=" in request.headers.get("Cookie", "")
            code = 0 if logged or not self.anonymous_nav else -101
            data = {
                "isLogin": logged,
                "mid": 946974,
                "uname": "合成测试UP",
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/" + "a" * 32 + ".png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/" + "b" * 32 + ".png",
                },
            }
        elif path == "/x/web-interface/view":
            bvid = request.query.get("bvid", BV)
            data = {
                "bvid": bvid,
                "title": "合成投稿 " + bvid,
                "desc": self.description,
                "pubdate": 1500000000,
                "owner": {"mid": 946974, "name": "测试UP主"},
                "pic": "https://i0.hdslb.com/bfs/archive/synthetic-fixture.png"
                if self.cover_enabled
                else None,
                "pages": [
                    {
                        "cid": int(cid),
                        "page": index + 1,
                        "part": f"合成分P{index + 1}",
                        "duration": 2,
                    }
                    for index, cid in enumerate(self.parts[bvid])
                ],
            }
        elif path == "/x/tag/archive/tags":
            data = [{"tag_name": "合成测试"}]
        elif path == "/x/player/wbi/playurl":
            data = {
                "dash": {"video": [{"id": 80}, {"id": 64}], "audio": [{"id": 30280}]},
                "support_formats": [
                    {"quality": 80, "new_description": "1080P"},
                    {"quality": 64, "new_description": "720P"},
                ],
            }
        elif path == "/x/v3/fav/resource/list":
            if self.fail_page:
                return web.json_response({"code": -503, "data": {}})
            data = {
                "info": {"media_count": len(self.members)},
                "medias": [{"bvid": bvid} for bvid in self.members],
                "has_more": False,
            }
        elif path == "/x/space/wbi/arc/search":
            data = {
                "page": {"count": len(self.members)},
                "list": {"vlist": [{"bvid": bvid} for bvid in self.members]},
            }
        elif path in {"/x/polymer/web-space/seasons_archives_list", "/x/series/archives"}:
            data = {
                "page": {"total": len(self.members)},
                "archives": [{"bvid": bvid} for bvid in self.members],
            }
        elif path.endswith("/qrcode/generate"):
            data = {
                "url": "https://passport.bilibili.com/synthetic-fixture",
                "qrcode_key": "synthetic-only",
            }
        elif path.endswith("/qrcode/poll"):
            data = {
                "code": {"waiting": 86101, "scanned": 86090, "expired": 86038, "ready": 0}[
                    self.qr_state
                ]
            }
            if self.qr_state == "ready":
                response = web.json_response({"code": 0, "data": data})
                response.set_cookie("SESSDATA", "synthetic-qr-only")
                response.set_cookie("DedeUserID", "946974")
                return response
        else:
            return web.Response(status=404)
        return web.json_response({"code": code, "data": data})

    async def start(self):
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner, self.url = await start_http(app)
        return self


class SyntheticEngine(Engine):
    """Produces genuine FFmpeg media; deliberately not evidence of a real upstream download."""

    def __init__(self, config):
        super().__init__(config)
        self.calls = []
        self.fail_cid = None

    async def check(self):
        self.status = {
            "state": "ready",
            "version": "2026.08.19",
            "code": "synthetic_ffmpeg_fixture",
        }
        return self.status

    async def formats(self, bvid, part, cookie, directory):
        return [
            {"quality_id": "80", "label": "合成1080P"},
            {"quality_id": "64", "label": "合成720P"},
        ]

    async def download(self, job, part, cookie, directory):
        from services.platform.contracts import Fault, require

        self.calls.append(part["cid"])
        if part["cid"] == self.fail_cid:
            raise Fault("download_failed", 503)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "download.mp4"
        code, _, _ = await run_process(
            [
                self.config["ffmpeg"],
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=160x90:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=2",
                "-c:v",
                "mpeg4",
                "-c:a",
                "aac",
                "-shortest",
                "-y",
                str(output),
            ]
        )
        require(code == 0, "fixture_video_failed", 503)
        await self.verify(output, 2)
        return {"file": str(output), "actual_quality": "80", "extension": "mp4"}


class PublicationFixture:
    def __init__(self, staging):
        self.staging = Path(staging)
        self.operations = {}
        self.keys = {}
        self.fail = False

    async def handle(self, request):
        if self.fail:
            return web.json_response({"code": "busy"}, status=503)
        if request.path.endswith("/prepare"):
            body = await request.json()
            raw = base64.b64decode(body["manifest_base64"])
            if hashlib.sha256(raw).hexdigest() != body["manifest_digest"]:
                return web.json_response({"code": "hash_mismatch"}, status=400)
            if body["idempotency_key"] in self.keys:
                return web.json_response(self.operations[self.keys[body["idempotency_key"]]])
            manifest = json.loads(raw)
            for entry in manifest["files"]:
                path = self.staging / manifest["staging_ref"] / entry["path"]
                if (
                    not path.is_file()
                    or hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]
                ):
                    return web.json_response({"code": "hash_mismatch"}, status=400)
            identity = str(uuid.uuid4())
            operation = {
                "operation_id": identity,
                "package_id": manifest["package_id"],
                "library_id": manifest["library_id"],
                "status": "prepared",
                "version": 1,
                "manifest_digest": body["manifest_digest"],
                "confirmation_digest": "f" * 64,
                "scope_revision": 1,
                "target_relative_path": "bilibili-" + manifest["bvid"],
                "expires_at": None,
                "issues": [],
                "receipt": None,
            }
            self.operations[identity] = operation
            self.keys[body["idempotency_key"]] = identity
            operation["_manifest"] = manifest
        else:
            identity = request.path.split("/")[5]
            operation = self.operations[identity]
            if request.path.endswith("/publish"):
                manifest = operation["_manifest"]
                operation.update(
                    status="indexed",
                    version=2,
                    receipt={
                        "operation_id": identity,
                        "package_id": manifest["package_id"],
                        "library_id": manifest["library_id"],
                        "target_relative_path": operation["target_relative_path"],
                        "published_at": "2026-10-09T00:00:00Z",
                        "indexed_at": "2026-10-09T00:00:01Z",
                        "files": [
                            {**entry, "entry_id": str(uuid.uuid4())} for entry in manifest["files"]
                        ],
                    },
                )
            elif request.path.endswith("/cancel"):
                operation.update(status="cancelled", version=operation["version"] + 1)
        return web.json_response(
            {key: value for key, value in operation.items() if not key.startswith("_")}
        )

    async def start(self):
        app = web.Application(client_max_size=2 * 1024**2)
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.runner, self.url = await start_http(app)
        return self


def settings(directory, engine, publisher_url, static=None, port=18898):
    config = web_settings(directory, f"http://127.0.0.1:{port}", static)
    config["media"] = {
        "enabled": True,
        "package_contract_directory": str(CONTRACT.parents[1] / "media-package" / "v1"),
        "staging_directory": str(Path(directory) / "staging"),
        "workers": 1,
        "engine": engine,
        "targets": [
            {
                "target_id": "synthetic-network-video",
                "label": "合成网络视频库",
                "library_id": "11111111-1111-1111-1111-111111111111",
                "publisher": {"base_url": publisher_url, "token_env": "SYNTHETIC_MEDIA_PUBLISH"},
                "servers": [],
            }
        ],
    }
    return config


def subscription(initial="history", source_value=None):
    return {
        "subscription_id": None,
        "expected_revision": 0,
        "label": "合成收藏订阅",
        "source": source_value or {"kind": "favorite", "id": "123"},
        "account_id": None,
        "target_id": "synthetic-network-video",
        "initial_sync": initial,
        "quality": dict(QUALITY),
        "rules": copy.deepcopy(EMPTY_RULES),
        "interval_seconds": 600,
        "client_id": str(uuid.uuid4()),
    }
