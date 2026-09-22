"""Fresh wheel install evidence. Default is a read-only plan; never uses a test venv."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parents[2]


def pins(path):
    return dict(re.findall(r"^([\w-]+)==([^\s]+)", path.read_text(), re.M))


def run(args, cwd):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    env.pop("PYTHONPATH", None)
    subprocess.run([str(x) for x in args], cwd=cwd, env=env, check=True, timeout=600)


def executable(env, name="python"):
    return (
        env
        / ("Scripts" if os.name == "nt" else "bin")
        / (name + ".exe" if os.name == "nt" else name)
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--scope", required=True, help="New directory beneath this checkout's .runtime"
    )
    args = parser.parse_args()
    scope = Path(args.scope).resolve()
    if not scope.is_relative_to((ROOT / ".runtime").resolve()) or scope.exists():
        parser.error("scope must be a new directory below this checkout's .runtime")
    plan = {
        "scope": str(scope),
        "linux_image_executed": False,
        "steps": [
            "create separate build and runtime venvs",
            "build noneditable wheel with pinned backend",
            "install complete hash-locked binary runtime closure",
            "pip check and isolated installed CLI",
        ],
    }
    if not args.execute:
        print(json.dumps(plan, indent=2))
        return
    scope.mkdir(parents=True)
    source = scope / "source"
    source.mkdir()
    shutil.copy2(ROOT / "pyproject.toml", source)
    shutil.copytree(
        ROOT / "services",
        source / "services",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    build, runtime = scope / "build", scope / "runtime"
    venv.create(build, with_pip=True)
    venv.create(runtime, with_pip=True)
    bp, rp = executable(build), executable(runtime)
    run([bp, "-m", "pip", "install", "--no-deps", "-r", ROOT / "scripts/build/tools.lock"], scope)
    run(
        [
            bp,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "-w",
            scope / "wheels",
            source,
        ],
        scope,
    )
    run(
        [
            rp,
            "-m",
            "pip",
            "install",
            "--only-binary=:all:",
            "--require-hashes",
            "-r",
            ROOT / "scripts/build/runtime.lock",
        ],
        scope,
    )
    wheels = list((scope / "wheels").glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError("expected exactly one product wheel")
    run([rp, "-m", "pip", "install", "--no-index", "--no-deps", wheels[0]], scope)
    run([rp, "-m", "pip", "check"], scope)
    run([rp, "-I", "-m", "services.platform", "--help"], scope)
    run([executable(runtime, "tianshu-platform"), "--help"], scope)
    for command in ("serve", "local", "preflight", "healthcheck"):
        run([rp, "-I", "-m", "services.platform", command, "--help"], scope)
    probe = "import importlib.metadata as m,json,services.platform as p; print(json.dumps({'packages':{d.metadata['Name'].lower().replace('_','-'):d.version for d in m.distributions()},'module':p.__file__}))"
    evidence = json.loads(subprocess.check_output([str(rp), "-I", "-c", probe], cwd=scope))
    expected = pins(ROOT / "scripts/build/runtime.lock") | {"tianshu-platform-backend": "0.1.0"}
    actual = {k: v for k, v in evidence["packages"].items() if k != "pip"}
    if expected != actual or not Path(evidence["module"]).resolve().is_relative_to(runtime):
        raise RuntimeError("runtime package set or import provenance differs from frozen inputs")
    evidence.update(plan)
    evidence["python"] = sys.version
    evidence["wheel_sha256"] = hashlib.sha256(wheels[0].read_bytes()).hexdigest()
    evidence["input_sha256"] = {
        p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest()
        for p in (
            "pyproject.toml",
            "scripts/build/runtime.lock",
            "scripts/build/tools.lock",
            "Dockerfile",
        )
    }
    (scope / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")


if __name__ == "__main__":
    main()
