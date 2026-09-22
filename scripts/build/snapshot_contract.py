"""Verify an externally pinned manifest before copying its exact bytes into a new context."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath


def snapshot(source, target, expected):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists() or target == source or source.is_relative_to(target):
        raise ValueError("target must be a new, separate package directory")
    raw = (source / "manifest.json").read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError("manifest raw-byte digest mismatch")
    manifest = json.loads(raw)
    hashes = manifest.get("files", manifest.get("sha256"))
    if not isinstance(hashes, dict) or not hashes:
        raise ValueError("unsupported manifest")
    basis = manifest.get("hash_basis", "")
    normalize = basis in (
        "UTF-8 text bytes with CRLF normalized to LF",
        "UTF-8 text normalized CRLF to LF",
        "UTF-8 text bytes with CRLF normalized to LF; manifest itself pinned externally",
        "UTF-8 LF-normalized",
    )
    if basis and not normalize:
        raise ValueError("unsupported hash basis")
    contents = {"manifest.json": raw}
    for name, digest in hashes.items():
        prefix = source.parent.name + "/" + source.name + "/"
        if name.startswith(prefix):
            name = name[len(prefix) :]
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts or "\\" in name or ":" in name:
            raise ValueError("unsafe manifest path")
        if name in contents:
            raise ValueError("duplicate or reserved manifest member")
        path = (source / name).resolve()
        if not path.is_relative_to(source) or not path.is_file():
            raise ValueError("file missing or outside package")
        data = path.read_bytes()
        checked = data.replace(b"\r\n", b"\n") if normalize else data
        if hashlib.sha256(checked).hexdigest() != digest:
            raise ValueError("contract member digest mismatch: " + name)
        contents[name] = data
    # Validate everything before writing; copy captured bytes, never re-read mutable source.
    target.mkdir(parents=True, exist_ok=False)
    for name, data in contents.items():
        output = target / name
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(data)
    return {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute:
        print("plan: verify externally pinned manifest and members; copy raw bytes to new target")
        return
    print(json.dumps(snapshot(args.source, args.target, args.manifest_sha256), indent=2))


if __name__ == "__main__":
    main()
