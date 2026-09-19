"""Synthetic fixtures for the TS-098 rule tests.

Everything here is hand-made: no captured API response, no real account, no real policy document and
no network. Documents are built by small helpers so a test can state the *shape* it means
(``group("bl", rule("r1", "title", "contains", "预告"))``) instead of pasting a 30-line literal.

``normalize_bilibili`` from TS-090 is reused read-only: the rules block consumes normalized metadata,
and building the fixture *through* the real normalizer is what keeps these tests about the rules
rather than about a hand-written stand-in for ``MediaMetadata``.
"""

from __future__ import annotations

import copy
from typing import Mapping

from services.platform.media import normalize_bilibili

BV = "BV1xx411c7mD"
CID_ONE = "111111111"
CID_TWO = "222222222"
MID_UP = "946974"
MID_JOINT = "208259"

TITLE = "总集篇 4K 合集 预告?"
DESCRIPTION = "简介第一行\n简介第二行"
UP_NAME = "测试UP主"
JOINT_NAME = "联合投稿人"
OTHER_BV = "BV1Ab4y1z7Qs"


def snapshot(**overrides: object) -> dict[str, object]:
    """A complete, valid one-part projection accepted by TS-090's normalizer."""

    document: dict[str, object] = {
        "schema_version": 1,
        "bvid": BV,
        "title": TITLE,
        "description": DESCRIPTION,
        "published_at": "2019-08-02T08:30:00+08:00",
        "captured_at": "2026-09-19T12:00:00Z",
        "creators": [{"mid": MID_UP, "name": UP_NAME, "role": "uploader"}],
        "parts": [
            {"cid": CID_ONE, "index": 1, "title": "第一集", "duration_seconds": 725},
        ],
        "tags": ["动画", "合集"],
        "cover_available": True,
    }
    document.update(overrides)
    return document


def metadata(**overrides: object):
    """Normalized metadata for one synthetic upload; override any snapshot field by keyword."""

    return normalize_bilibili(snapshot(**overrides))


def rule(
    rule_id: str,
    field: str = "title",
    op: str = "contains",
    value: str = "合集",
    *,
    case_sensitive: bool = False,
) -> dict[str, object]:
    """One rule record with the frozen five keys."""

    return {
        "id": rule_id,
        "field": field,
        "op": op,
        "value": value,
        "case_sensitive": case_sensitive,
    }


def group(group_id: str, *rules: Mapping[str, object]) -> dict[str, object]:
    """One group record; a group with no rules is deliberately constructible for negative tests."""

    return {"id": group_id, "rules": [dict(entry) for entry in rules]}


def document(
    *,
    revision: int = 7,
    whitelist: list[object] | None = None,
    blacklist: list[object] | None = None,
) -> dict[str, object]:
    """A complete policy document; both lists default to empty.

    Entries are copied but never coerced: a negative test needs to place a non-mapping, a string or
    a list where a group is expected, and a fixture that "helpfully" converted it would test the
    fixture instead of the parser.
    """

    return {
        "schema_version": 1,
        "revision": revision,
        "whitelist": copy.deepcopy(list(whitelist or [])),
        "blacklist": copy.deepcopy(list(blacklist or [])),
    }


def text_policy(**overrides: object) -> dict[str, object]:
    """A policy whose every rule is ordinary text, so no child process is ever needed."""

    options: dict[str, object] = {
        "whitelist": [group("wl", rule("r_title", "title", "contains", "合集"))],
        "blacklist": [group("bl", rule("r_preview", "title", "contains", "预告"))],
    }
    options.update(overrides)
    return document(**options)  # type: ignore[arg-type]
