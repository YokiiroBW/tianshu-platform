"""Public web transport policy and restart-pending configuration, separate from RPC TLS."""

import copy
import ipaddress
import json
import os
import re
import ssl
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from .contracts import Fault, require
from .transport import server_tls


def origin(value):
    require(isinstance(value, str) and len(value) <= 300 and value.isascii(), "invalid_input", 400)
    parts = urlsplit(value)
    require(
        parts.scheme in {"http", "https"}
        and parts.hostname
        and not (parts.username or parts.password or parts.path or parts.query or parts.fragment)
        and not any(c.isspace() for c in value)
        and "\\" not in value,
        "invalid_input",
        400,
    )
    host = parts.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        require(
            len(host) <= 253
            and all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in host.split(".")
            ),
            "invalid_input",
            400,
        )
    require(parts.port is None or 1 <= parts.port <= 65535, "invalid_input", 400)
    require(value == parts.scheme + "://" + parts.netloc.lower(), "invalid_input", 400)
    return parts


def certificate_context(tls, hostname):
    """Check key, lifetime and SAN using an offline TLS handshake; no DNS or network I/O."""
    try:
        context = server_tls(tls)
        verifier = ssl.create_default_context(cafile=tls["certificate_file"])
        verifier.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
        si, so, ci, co = (ssl.MemoryBIO() for _ in range(4))
        server = context.wrap_bio(si, so, server_side=True)
        client = verifier.wrap_bio(ci, co, server_hostname=hostname)
        completed = set()
        for _ in range(16):
            for name, peer in (("client", client), ("server", server)):
                if name not in completed:
                    try:
                        peer.do_handshake()
                        completed.add(name)
                    except ssl.SSLWantReadError:
                        pass
            if co.pending:
                si.write(co.read())
            if so.pending:
                ci.write(so.read())
            if len(completed) == 2:
                return context
    except (OSError, ValueError, TypeError, KeyError):
        pass
    raise Fault("invalid_input", 400)


class WebAccess:
    def __init__(self, settings):
        self.config = settings.get("web_access")
        self.path = Path(str(settings.get("database_path", "")) + ".web-access.json")
        self.current = None
        self.tls = None
        if self.config is None:
            return
        c = self.config
        require(
            settings.get("mode") == "service_https" and isinstance(settings.get("web"), dict),
            "invalid_input",
            400,
        )
        require(
            isinstance(c, dict) and set(c) == {"host", "port", "initial", "certificates"},
            "invalid_input",
            400,
        )
        address = ipaddress.ip_address(c["host"])
        require(
            address.version == 4
            and (address.is_loopback or address.is_private or address.is_unspecified),
            "invalid_input",
            400,
        )
        require(type(c["port"]) is int and 1024 <= c["port"] <= 65535, "invalid_input", 400)
        require(
            isinstance(c["certificates"], dict) and len(c["certificates"]) <= 16,
            "invalid_input",
            400,
        )
        for name in c["certificates"]:
            require(
                isinstance(name, str) and re.fullmatch(r"[a-z0-9-]{1,48}", name),
                "invalid_input",
                400,
            )
        self.current = self.read()["value"]
        self.tls = self.validate(self.current)

    def validate(self, value, *, check_certificate=True):
        require(
            isinstance(value, dict) and set(value) == {"mode", "origin", "certificate"},
            "invalid_input",
            400,
        )
        parts = origin(value["origin"])
        mode = value["mode"]
        require(mode in {"http", "https", "proxy"}, "invalid_input", 400)
        require(mode == "proxy" or parts.scheme == mode, "invalid_input", 400)
        if mode == "https":
            name = value["certificate"]
            require(
                isinstance(name, str) and name in self.config["certificates"], "invalid_input", 400
            )
            if check_certificate:
                return certificate_context(self.config["certificates"][name], parts.hostname)
            return None
        require(value["certificate"] is None, "invalid_input", 400)
        return None

    def read(self):
        if not self.path.exists():
            self.validate(self.config["initial"], check_certificate=False)
            return {"revision": 0, "value": copy.deepcopy(self.config["initial"])}
        require(not self.path.is_symlink(), "invalid_input", 400)
        with self.path.open("rb") as source:
            raw = source.read(8193)
        require(len(raw) <= 8192, "invalid_input", 400)
        saved = json.loads(raw)
        require(
            isinstance(saved, dict) and set(saved) == {"revision", "value"}, "invalid_input", 400
        )
        require(type(saved["revision"]) is int and saved["revision"] > 0, "invalid_input", 400)
        self.validate(saved["value"], check_certificate=False)
        return saved

    def view(self):
        if self.current is None:
            return {"available": False}
        saved = self.read()
        return {
            "available": True,
            "active": self.current,
            "saved": saved["value"],
            "revision": saved["revision"],
            "restart_required": saved["value"] != self.current,
            "certificates": sorted(self.config["certificates"]),
            "listener_port": self.config["port"],
        }

    def save(self, value, revision):
        self.validate(value)
        require(
            type(revision) is int and revision == self.read()["revision"], "version_conflict", 409
        )
        document = {"revision": revision + 1, "value": value}
        raw = json.dumps(document, ensure_ascii=True, sort_keys=True).encode() + b"\n"
        descriptor, temporary = tempfile.mkstemp(prefix=".web-access-", dir=self.path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            if os.name != "nt":
                directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return self.view()
