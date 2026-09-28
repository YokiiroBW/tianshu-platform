"""Operator token client for the running Platform observation manager.

This client never constructs Platform or opens its SQLite files. The running
service remains the single owner of adapter, bot and observation state.
"""

import argparse
import json
import os
import ssl
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPSHandler, HTTPRedirectHandler, ProxyHandler, Request, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=["status", "enroll-default"])
    parser.add_argument("--url", required=True, help="Running Platform HTTPS origin")
    parser.add_argument("--ca-file", help="Trusted Platform CA certificate")
    parser.add_argument("--credential-env", required=True)
    parser.add_argument("--input", help="Private enrollment JSON; required for enroll-default")
    args = parser.parse_args()
    target = urlsplit(args.url)
    if (
        target.scheme != "https"
        or not target.hostname
        or target.username
        or target.password
        or target.path not in {"", "/"}
        or target.query
        or target.fragment
    ):
        parser.error("--url must be a Platform HTTPS origin")
    if (args.operation == "enroll-default") != bool(args.input):
        parser.error("--input is required only for enroll-default")
    token = os.environ.get(args.credential_env, "")
    if not 24 <= len(token) <= 4096:
        parser.error("operator credential is unavailable")
    body = Path(args.input).read_bytes() if args.input else b"{}"
    if len(body) > 32768:
        parser.error("input is too large")
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError("input must be an object")
        path = "/internal/v2/observation-admin/" + args.operation
        request = Request(
            args.url.rstrip("/") + path,
            data=json.dumps(value, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Accept-Encoding": "identity",
            },
            method="POST",
        )
        context = ssl.create_default_context(cafile=args.ca_file)
        client = build_opener(ProxyHandler({}), HTTPSHandler(context=context), NoRedirect())
        try:
            with client.open(request, timeout=10) as response:
                code, result = response.status, response.read(131073)
        except HTTPError as error:
            code, result = error.code, error.read(131073)
        if len(result) > 131072:
            raise ValueError("response too large")
        document = json.loads(result)
        if not isinstance(document, dict):
            raise ValueError("invalid response")
        print(json.dumps(document, ensure_ascii=False, separators=(",", ":")))
        return 0 if code == 200 else 1
    except (OSError, URLError, ValueError, TypeError, json.JSONDecodeError):
        print('{"code":"dependency_unavailable"}')
        return 1


if __name__ == "__main__":
    sys.exit(main())
