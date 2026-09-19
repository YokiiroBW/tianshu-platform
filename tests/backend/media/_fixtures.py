"""Synthetic fixtures for the TS-090 media metadata tests.

Everything here is hand-made input for a *future* connector projection. There is no captured API
response and no real account data: the shapes are the ones frozen by the task card, and the values
are chosen to exercise escaping, identity and gap reporting.
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Mapping

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if os.path.isdir(os.path.join(_ROOT, "services", "platform")) and _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from services.platform.media import ImageBinding, RenderRequest  # noqa: E402

BV = "BV1xx411c7mD"
OTHER_BV = "BV1Ab4y1z7Qs"
CID_ONE = "111111111"
CID_TWO = "222222222"
CID_THREE = "333333333"
MID_UP = "946974"
MID_JOINT = "208259"
MID_OTHER = "409796278"

TITLE = "  标题 & <测试> \"引号\" '单引号' 😀\n第二行  "
DESCRIPTION = "简介第一行 & <标签>\n简介第二行 😀"
UP_NAME = "测试UP主"
JOINT_NAME = "联合投稿人"


def snapshot(**overrides: object) -> dict[str, object]:
    """A complete, valid one-part projection with a Chinese title, emoji and XML punctuation."""

    document: dict[str, object] = {
        "schema_version": 1,
        "bvid": BV,
        "title": TITLE,
        "description": DESCRIPTION,
        "published_at": "2019-08-02T08:30:00+08:00",
        "captured_at": "2026-09-19T12:00:00Z",
        "creators": [{"mid": MID_UP, "name": UP_NAME, "role": "uploader"}],
        "parts": [
            {"cid": CID_ONE, "index": 1, "title": "第一集 & 开场", "duration_seconds": 725},
        ],
        "tags": [" 测试 ", "测试", "动画"],
        "cover_available": True,
    }
    document.update(overrides)
    return document


def multipart_snapshot(**overrides: object) -> dict[str, object]:
    """A three-part projection in a deliberately reshuffled source order."""

    parts = [
        {"cid": CID_THREE, "index": 3, "title": "第三集", "duration_seconds": 59},
        {"cid": CID_ONE, "index": 1, "title": "第一集", "duration_seconds": 3601},
        {"cid": CID_TWO, "index": 2, "title": None, "duration_seconds": None},
    ]
    document = snapshot(
        title="多 P 投稿",
        description=None,
        parts=parts,
        creators=[
            {"mid": MID_UP, "name": UP_NAME, "role": "uploader"},
            {"mid": MID_JOINT, "name": JOINT_NAME, "role": "collaborator"},
        ],
        tags=["合集"],
    )
    document.update(overrides)
    return document


def part(**overrides: object) -> dict[str, object]:
    """One part record with the frozen four keys; override a key to build a malformed sample."""

    record: dict[str, object] = {
        "cid": CID_ONE,
        "index": 1,
        "title": "第一集 & 开场",
        "duration_seconds": 725,
    }
    record.update(overrides)
    return record


def frozen_copy(document: Mapping[str, object]) -> dict[str, object]:
    return copy.deepcopy(dict(document))


def single_request(**overrides: object) -> RenderRequest:
    options: dict[str, object] = {
        "layout": "single",
        "selected_cids": (CID_ONE,),
        "media_extension": "mkv",
        "images": (),
    }
    options.update(overrides)
    return RenderRequest.build(**options)  # type: ignore[arg-type]


def multipart_request(**overrides: object) -> RenderRequest:
    options: dict[str, object] = {
        "layout": "multipart",
        "selected_cids": (CID_ONE, CID_TWO),
        "media_extension": "mp4",
        "episode_numbers": {CID_ONE: 1, CID_TWO: 2},
        "images": (),
    }
    options.update(overrides)
    return RenderRequest.build(**options)  # type: ignore[arg-type]


def poster(extension: str = "jpg") -> ImageBinding:
    return ImageBinding(role="poster", cid=None, extension=extension)


def thumb(cid: str, extension: str = "png") -> ImageBinding:
    return ImageBinding(role="episode_thumb", cid=cid, extension=extension)
