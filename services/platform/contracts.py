"""Read the published 1.0.0 documents in place; never fetch remote schemas."""

import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

MANIFEST_SHA256 = "81e6cc4ddef7c6f82e055d4cb04b090db036dd5c52763473ce697aa02db478a1"


class Fault(Exception):
    def __init__(self, code="invalid_input", status=400):
        self.code, self.status = code, status
        super().__init__(code)


def require(condition, code="forbidden", status=403):
    if not condition:
        raise Fault(code, status)


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def loads(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError()
            result[key] = value
        return result

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError()
        return number

    try:
        return json.loads(
            raw.decode("utf-8") if isinstance(raw, bytes) else raw,
            object_pairs_hook=pairs,
            parse_constant=finite,
            parse_float=finite,
        )
    except (ValueError, UnicodeError, RecursionError):
        raise Fault() from None


def epoch(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def utc(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


class Contracts:
    def __init__(self, directory):
        root = Path(directory).resolve()

        def read(path, expected):
            raw = path.read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError("published contract hash mismatch")
            return loads(raw)

        manifest = read(root / "manifest.json", MANIFEST_SHA256)
        resources = []
        for family in ("common", "model", "conversation", "identity-memory", "web"):
            schema = read(
                root / "schemas" / f"{family}.json",
                manifest["sha256"][f"text-dialogue/v1/schemas/{family}.json"],
            )
            resources.append((schema["$id"], Resource.from_contents(schema)))
        self.registry = Registry().with_resources(resources)

    def check(self, name, document):
        family, kind = name.split("#")
        try:
            Draft202012Validator(
                {
                    "$ref": f"https://contracts.tianshu.invalid/text-dialogue/v1/{family}.json#/$defs/{kind}"
                },
                registry=self.registry,
                format_checker=FormatChecker(),
            ).validate(document)
        except (ValidationError, RecursionError):
            raise Fault() from None
