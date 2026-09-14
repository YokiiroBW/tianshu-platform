"""Export integrated pins into this TS-014 worktree and run actual web TLS acceptance."""

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / ".runtime/web-joint"
COORDINATOR = "a472825dd983dca43862f9cb8be2e3a7834d3415"
PINS = {
    "memory": "575941ee12e2a7d6dbd3d3374e34866eb82006fb",
    "model-gateway": "b3b101faf3902f05d80818b39fe7c91367865d4e",
}


def prepare(core_commit):
    context = json.loads((ROOT / ".runtime/workspace-context.json").read_text("utf-8"))
    assert context["task"] == "TS-014"
    workspace = Path(context["workspace"])
    RUNTIME.mkdir(parents=True, exist_ok=True)
    sources = RUNTIME / "sources"
    pins = {**PINS, "companion": core_commit}
    for name, commit in pins.items():
        repo = workspace / "projects" / ("tianshu-" + name)
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "main"], check=True
        )
        destination = sources / name
        marker = destination / ".ts014-commit"
        if destination.exists():
            assert marker.read_text() == commit, "Refusing different existing snapshot"
            continue
        raw = subprocess.check_output(["git", "-C", str(repo), "archive", "--format=tar", commit])
        destination.mkdir(parents=True)
        with tarfile.open(fileobj=io.BytesIO(raw)) as archive:
            archive.extractall(destination, filter="data")
        marker.write_text(commit)
    support = RUNTIME / "support"
    support.mkdir(exist_ok=True)
    for name in ("ts050_support.py", "ts050_source_support.py"):
        raw = subprocess.check_output(
            ["git", "-C", str(workspace), "show", COORDINATOR + ":tests/integration/" + name]
        )
        (support / name).write_bytes(raw)
    # Reuse the exact union approach from the committed TS050 runner. Product pins
    # must agree; this isolated requirements file is never a project lock change.
    versions = {}
    for path in (ROOT / "requirements-dev.txt", sources / "companion/requirements-dev.txt"):
        for line in path.read_text().splitlines():
            if not line or line.startswith(("#", " ", "-")):
                continue
            name, version = line.split("==")
            name = name.lower().replace("_", "-")
            if name == "ruff":
                continue
            assert name not in versions or versions[name] == version
            versions[name] = version
    versions.update(cryptography="50.0.1", cffi="2.1.1", pycparser="3.0")
    requirements = RUNTIME / "requirements.txt"
    requirements.write_text(
        "".join(f"{name}=={version}\n" for name, version in sorted(versions.items()))
    )
    evidence = {
        "products": pins,
        "platform_base_head": subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip(),
        "support_commit": COORDINATOR,
        "requirements_sha256": hashlib.sha256(requirements.read_bytes()).hexdigest(),
    }
    files = sorted(
        [
            *ROOT.glob("services/platform/*.py"),
            *(p for p in (ROOT / "apps/web/src").rglob("*") if p.is_file()),
        ]
    )
    evidence["platform_source_sha256"] = hashlib.sha256(
        "".join(
            str(p.relative_to(ROOT)).replace("\\", "/")
            + ":"
            + hashlib.sha256(p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()
            + "\n"
            for p in files
        ).encode()
    ).hexdigest()
    (RUNTIME / "inputs.json").write_text(json.dumps(evidence, indent=2))
    return workspace, sources, support


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--core-commit", required=True, help="Coordinator-published integrated Core commit"
    )
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    workspace, sources, support = prepare(args.core_commit)
    if args.prepare_only:
        print(str(RUNTIME / "requirements.txt"))
        return
    os.environ.update(
        TS050_RUNTIME=str(RUNTIME),
        TS050_CONTRACTS=str(workspace / "contracts/text-dialogue/v1"),
        TS050_SNAPSHOT_ROOT=str(sources),
        TS014_JOINT_RUNTIME=str(RUNTIME),
        PYTHONDONTWRITEBYTECODE="1",
    )
    paths = [
        str(ROOT),
        str(support),
        *(str(sources / name / "src") for name in ("companion", "memory", "model-gateway")),
        str(ROOT / "tests/backend"),
    ]
    os.environ["PYTHONPATH"] = os.pathsep.join(paths)
    sys.path[:0] = paths
    import unittest

    suite = unittest.defaultTestLoader.loadTestsFromName("web_joint_scenarios.WebJoint")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())


if __name__ == "__main__":
    main()
