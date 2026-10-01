"""Pinned shared DTO input, not a product-owned schema copy."""

import hashlib
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ..contracts import Fault

SCHEMA_HASH = "f3b588591411f1ed4b8aa7c9003d201530644d4dfc02294bdd9e9d7f847214a3"


class Contract:
    def __init__(self, path):
        raw = Path(path).read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(raw).hexdigest() != SCHEMA_HASH:
            raise Fault("invalid_input", 400)
        self.schema = json.loads(raw)

    def check(self, kind, value, *, upstream=False):
        try:
            Draft202012Validator({**self.schema, "$ref": "#/$defs/" + kind}).validate(value)
        except ValidationError:
            raise Fault(
                "invalid_upstream" if upstream else "invalid_input", 502 if upstream else 400
            ) from None
