#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "harnesses.json"
RECIPE_VERSION = 2


def lab_root() -> Path:
    return Path(os.environ.get("RESEARCH_LAB_ROOT", str(ROOT / ".lab"))).expanduser().resolve()


def run(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", shlex.join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def run_retry(cmd: list[str], *, cwd: Path | None = None, attempts: int = 2) -> None:
    last: subprocess.CalledProcessError | None = None
    for attempt in range(1, attempts + 1):
        try:
            run(cmd, cwd=cwd)
            return
        except subprocess.CalledProcessError as exc:
            last = exc
            if attempt >= attempts:
                raise
            print(f"[setup] command failed; retrying ({attempt}/{attempts})")
            time.sleep(1)
    if last is not None:
        raise last


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def selected(config: dict[str, Any], value: str | None) -> list[str]:
    ids = [x.strip() for x in value.split(",") if x.strip()] if value else list(config["default_harnesses"])
    unknown = [x for x in ids if x not in config["harnesses"]]
    if unknown:
        raise SystemExit(f"unknown harness(es): {', '.join(unknown)}")
    return ids


def checkout_path(harness_id: str) -> Path:
    return lab_root() / "upstreams" / harness_id


def venv_path(harness_id: str) -> Path:
    return lab_root() / "venvs" / harness_id


def marker_path(harness_id: str) -> Path:
    return venv_path(harness_id) / ".research-lab-ready.json"


def repair_origin(harness_id: str, spec: dict[str, Any]) -> None:
    target = checkout_path(harness_id)
    if not (target / ".git").exists():
        return
    desired = spec["repo"]
    try:
        current = subprocess.check_output(["git", "remote", "get-url", "origin"], cwd=target, text=True).strip()
    except subprocess.CalledProcessError:
        print(f"[setup] {harness_id}: adding origin -> {desired}")
        run(["git", "remote", "add", "origin", desired], cwd=target)
        return
    if current != desired:
        print(f"[setup] {harness_id}: origin -> {desired}")
        run(["git", "remote", "set-url", "origin", desired], cwd=target)


def checkout_ready(harness_id: str, spec: dict[str, Any]) -> bool:
    target = checkout_path(harness_id)
    if not (target / ".git").exists():
        return False
    try:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=target, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        return False
    return head == spec["ref"]


def env_ready(harness_id: str, spec: dict[str, Any]) -> bool:
    if spec["adapter"] == "starintel":
        return checkout_ready(harness_id, spec) and (checkout_path(harness_id) / "bin" / "research").exists()
    py = venv_path(harness_id) / "bin" / "python"
    marker = marker_path(harness_id)
    if not py.exists() or not marker.exists() or not checkout_ready(harness_id, spec):
        return False
    try:
        data = json.loads(marker.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return data == {"recipe_version": RECIPE_VERSION, "upstream_ref": spec["ref"]}


def clone_checkout(harness_id: str, spec: dict[str, Any], target: Path) -> None:
    # A failed clone may leave a non-repository directory behind. This tree is
    # lab-owned scratch state, so discard it rather than forcing manual cleanup.
    if target.exists() and not (target / ".git").exists():
        print(f"[setup] {harness_id}: removing incomplete checkout {target}")
        shutil.rmtree(target)

    for attempt in (1, 2):
        try:
            run(["git", "clone", "--filter=blob:none", spec["repo"], str(target)])
            return
        except subprocess.CalledProcessError:
            if target.exists() and not (target / ".git").exists():
                shutil.rmtree(target)
            if attempt == 2:
                raise
            print(f"[setup] {harness_id}: clone failed; retrying once")
            time.sleep(1)


def ensure_checkout(harness_id: str, spec: dict[str, Any]) -> Path:
    target = checkout_path(harness_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        clone_checkout(harness_id, spec, target)
    repair_origin(harness_id, spec)
    run_retry(["git", "fetch", "--tags", "--prune", "origin"], cwd=target)
    run(["git", "checkout", "--detach", spec["ref"]], cwd=target)
    return target


def install_python_lane(harness_id: str, spec: dict[str, Any], target: Path) -> None:
    if shutil.which("uv") is None:
        raise SystemExit("uv is required; use 'nix develop' or install uv")

    venv = venv_path(harness_id)
    py = venv / "bin" / "python"
    venv.parent.mkdir(parents=True, exist_ok=True)
    if not py.exists():
        run(["uv", "venv", str(venv), "--python", "3.11"])

    adapter = spec["adapter"]
    if adapter == "researchharness":
        run(["uv", "pip", "install", "--python", str(py), "-r", str(target / "requirements.txt")])
        run(["uv", "pip", "install", "--python", str(py), "-e", str(target), "--no-deps"])
    elif adapter == "gpt_researcher":
        run(["uv", "pip", "install", "--python", str(py), "-e", str(target)])
    elif adapter == "smolagents_odr":
        run(["uv", "pip", "install", "--python", str(py), "-e", f"{target}[litellm]"])
    else:
        raise SystemExit(f"{harness_id}: no setup recipe for adapter {adapter!r}")

    marker_path(harness_id).write_text(
        json.dumps({"recipe_version": RECIPE_VERSION, "upstream_ref": spec["ref"]}, indent=2) + "\n"
    )


def prepare(harness_id: str, spec: dict[str, Any], *, force: bool = False) -> None:
    repair_origin(harness_id, spec)
    if not force and env_ready(harness_id, spec):
        print(f"[setup] {harness_id}: ready")
        return
    print(f"[setup] {harness_id}: preparing pinned {spec['ref'][:12]}")
    target = ensure_checkout(harness_id, spec)
    if spec["adapter"] == "starintel":
        if not (target / "bin" / "research").exists():
            raise SystemExit(f"{harness_id}: pinned checkout does not contain bin/research")
        print(f"[setup] {harness_id}: ready (CL runtime supplied by AR_RUNTIME_SOURCE)")
        return
    install_python_lane(harness_id, spec, target)
    print(f"[setup] {harness_id}: ready")


def main() -> int:
    p = argparse.ArgumentParser(prog="research-lab setup")
    p.add_argument("--harnesses", help="comma-separated harness ids; defaults to the controlled matrix")
    p.add_argument("--force", action="store_true", help="re-run dependency installation even when the pin marker matches")
    args = p.parse_args()

    config = load_config()
    for harness_id in selected(config, args.harnesses):
        prepare(harness_id, config["harnesses"][harness_id], force=args.force)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
