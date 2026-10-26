"""Exact local TS-309 candidate, isolated from published user contracts."""

import hashlib

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing import Registry, Resource

from ..contracts import Fault, canonical, loads, require

PACKAGE = "actor-capabilities/candidate-v1"
MANIFEST = "6e36fbfa6c82cadc93b2d0642c3591b596f4e9d7dde0b8774742ba63db1631ef"
REQUEST_LIMIT = 256 * 1024
RESPONSE_LIMIT = 1024 * 1024


class Protocol:
    def __init__(self, root):
        self.root = root / PACKAGE
        self.schema = None
        self.registry = None

    def check(self, kind, value):
        if self.schema is None:
            try:
                raw = (self.root / "manifest.json").read_bytes().replace(b"\r\n", b"\n")
                require(hashlib.sha256(raw).hexdigest() == MANIFEST, "dependency_unavailable", 503)
                for name, expected in loads(raw)["sha256"].items():
                    data = (self.root / name).read_bytes().replace(b"\r\n", b"\n")
                    require(
                        hashlib.sha256(data).hexdigest() == expected, "dependency_unavailable", 503
                    )
                schema = loads((self.root / "schema.json").read_bytes())
                self.registry = Registry().with_resource(
                    schema["$id"], Resource.from_contents(schema)
                )
                self.schema = schema
            except (OSError, ValueError, KeyError):
                raise Fault("dependency_unavailable", 503) from None
        try:
            Draft202012Validator(
                {"$ref": self.schema["$id"] + "#/$defs/" + kind},
                registry=self.registry,
                format_checker=FormatChecker(),
            ).validate(value)
        except (ValidationError, RecursionError):
            raise Fault("invalid_input", 400) from None

    def response(self, kind, value):
        self.check(kind, value)
        require(len(canonical(value).encode()) <= RESPONSE_LIMIT, "capacity_exceeded", 413)
        return value
