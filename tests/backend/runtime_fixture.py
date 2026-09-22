"""Synthetic deployment fixtures for the runtime diagnostics, probe and container suites.

Everything here is generated into one temporary directory: no NAS, no production container, no
real account, no paid call. The frozen diagnostic contract is never copied, edited or rewritten —
it is read from the published package, and its absence is an explicit skip rather than a pass.

TLS key pairs come from a separate tool interpreter holding `cryptography`, the same arrangement
the source-sync suite already uses, so the application keeps no new dependency.
"""

import json
import os
import subprocess
import unittest
from pathlib import Path

from aiohttp import web

READY_TOKEN_ENV = "TIANSHU_DIAGNOSTICS_TOKEN"
READY_TOKEN = "synthetic-readiness-token-100"

# Run by the tool interpreter, never by the application: three key pairs whose validity windows
# are the inputs the readiness TLS check is supposed to judge.
_GENERATOR = """
import ipaddress
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

root = Path(sys.argv[1])
now = datetime.now(timezone.utc)
windows = {
    "valid": (now - timedelta(minutes=5), now + timedelta(days=1)),
    "expired": (now - timedelta(days=2), now - timedelta(days=1)),
    "future": (now + timedelta(days=1), now + timedelta(days=2)),
}
for name, (start, end) in windows.items():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "synthetic-runtime-" + name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(end)
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(key, hashes.SHA256())
    )
    (root / (name + ".pem")).write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (root / (name + "-key.pem")).write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
"""


def tool_python():
    """The documented tool interpreter holding cryptography; absent means an explicit skip."""
    configured = os.environ.get("TS013_TLS_PYTHON")
    return configured if configured and Path(configured).exists() else None


def contract_directory():
    """The frozen diagnostics package: named directly, or beside the already-configured one."""
    configured = os.environ.get("TS100_DIAGNOSTICS_CONTRACT_DIR")
    if configured:
        return Path(configured)
    sibling = os.environ.get("TS012_CONTRACT_DIR")
    if sibling:
        return Path(sibling).resolve().parents[1] / "diagnostics" / "v1"
    return None


def require_contract():
    directory = contract_directory()
    if directory is None or not (directory / "event.schema.json").is_file():
        raise unittest.SkipTest("the frozen diagnostics contract package is not configured")
    return directory


def certificates(directory):
    """Three key pairs: usable now, already expired, and not yet valid."""
    python = tool_python()
    if python is None:
        raise unittest.SkipTest("TS013_TLS_PYTHON is not configured")
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    script = root / "make_runtime_certificates.py"
    script.write_text(_GENERATOR, encoding="utf-8")
    subprocess.run(
        [python, str(script), str(root)],
        check=True,
        capture_output=True,
        text=True,
        timeout=180,
    )
    return {
        name: {
            "certificate_file": str(root / (name + ".pem")),
            "private_key_file": str(root / (name + "-key.pem")),
        }
        for name in ("valid", "expired", "future")
    }


def built_static(directory):
    """A synthetic but structurally complete build: an entry point and the assets it references.

    The readiness check judges whether the served entry point can actually load, so a placeholder
    `index.html` with no assets is a failure on purpose. This fixture is the smallest directory
    that is honestly "a build is here", not a copy of the real one.
    """
    root = Path(directory)
    assets = root / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    (assets / "app.js").write_text("export const ready = true;\n", encoding="utf-8")
    (assets / "app.css").write_text(":root { color-scheme: dark; }\n", encoding="utf-8")
    (root / "index.html").write_text(
        "<!doctype html>\n"
        '<html lang="zh"><head><meta charset="utf-8">'
        '<link rel="stylesheet" href="/assets/app.css"></head>'
        '<body><div id="root"></div><script type="module" src="/assets/app.js"></script>'
        "</body></html>\n",
        encoding="utf-8",
    )
    return str(root)


def runtime_settings(directory, *, tls_name="valid", log_bytes=None, static=None, diagnostics=None):
    """A `service_https` deployment with its own log directory and its own readiness identity.

    `diagnostics` is replaced outright when given, which is how the negative cases are built: a
    deployment whose contract cannot be verified, or one with no durable directory at all.
    """
    from web_fixtures import web_settings

    if static is None:
        static = built_static(Path(directory) / "static")
    settings = web_settings(directory, origin="https://127.0.0.1:4433", static=static)
    settings["mode"] = "service_https"
    settings["tls"] = certificates(Path(directory) / "tls")[tls_name]
    if diagnostics is None:
        diagnostics = {
            "contract_directory": str(require_contract()),
            "log_directory": str(Path(directory) / "logs"),
        }
        if log_bytes is not None:
            diagnostics["log_directory_bytes"] = log_bytes
    settings["diagnostics"] = diagnostics
    return settings


def environment(extra=None):
    """The synthetic identities plus this suite's own readiness credential."""
    from fixtures import ENV

    merged = dict(ENV)
    merged[READY_TOKEN_ENV] = READY_TOKEN
    if extra:
        merged.update(extra)
    return merged


def log_files(directory):
    """Every log file this instance produced, in filename order."""
    root = Path(directory)
    return sorted(root.glob("*.jsonl")) if root.is_dir() else []


def read_events(directory):
    """Every complete record across every segment, in the order the files were written."""
    events = []
    for path in log_files(directory):
        for line in path.read_bytes().split(b"\n"):
            if line:
                events.append(json.loads(line))
    return events


def raw_lines(directory):
    """The raw bytes of every record, so encoding and line-ending rules can be asserted."""
    lines = []
    for path in log_files(directory):
        lines.extend(line for line in path.read_bytes().split(b"\n") if line)
    return lines


def events_named(directory, name):
    return [record for record in read_events(directory) if record["event"] == name]


async def start_server(app, *, tls=None):
    """A real loopback socket; TLS is a real handshake, not a double."""
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=tls)
    await site.start()
    port = runner.addresses[0][1]
    return runner, f"{'https' if tls else 'http'}://127.0.0.1:{port}"
