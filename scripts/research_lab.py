#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT / "config" / "harnesses.json"
GUIDELINES_PATH = ROOT / "guidelines" / "auto-research.org"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        value = os.path.expandvars(os.path.expanduser(value))
        os.environ.setdefault(key, value)


def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text())


def read_org_section(path: Path, heading: str) -> str:
    text = path.read_text()
    pattern = re.compile(
        rf"^\*+\s+{re.escape(heading)}\s*$\n(.*?)(?=^\*+\s+|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise SystemExit(f"{path}: missing '* {heading}' section")
    return match.group(1).strip()


def org_keyword(path: Path, key: str, default: str | None = None) -> str | None:
    rx = re.compile(rf"^#\+{re.escape(key)}:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
    match = rx.search(path.read_text())
    return match.group(1).strip() if match else default


def resolve_case(case_arg: str) -> tuple[str, Path, str]:
    candidate = Path(case_arg)
    if not candidate.exists():
        candidate = ROOT / "cases" / (case_arg if case_arg.endswith(".org") else f"{case_arg}.org")
    if not candidate.exists():
        raise SystemExit(f"case not found: {case_arg}")
    case_id = org_keyword(candidate, "lab_id", candidate.stem) or candidate.stem
    question = read_org_section(candidate, "Question")
    return case_id, candidate, question


def selected_harnesses(config: dict[str, Any], value: str | None) -> list[str]:
    if value:
        ids = [item.strip() for item in value.split(",") if item.strip()]
    else:
        ids = list(config["default_harnesses"])
    unknown = [h for h in ids if h not in config["harnesses"]]
    if unknown:
        raise SystemExit(f"unknown harness(es): {', '.join(unknown)}")
    return ids


def lab_root() -> Path:
    return Path(os.environ.get("RESEARCH_LAB_ROOT", str(ROOT / ".lab"))).expanduser().resolve()


def upstream_dir(harness_id: str) -> Path:
    return lab_root() / "upstreams" / harness_id


def venv_python(harness_id: str) -> Path:
    return lab_root() / "venvs" / harness_id / "bin" / "python"


def run_checked(cmd: list[str], *, cwd: Path | None = None) -> None:
    print("+", shlex.join(cmd))
    subprocess.run(cmd, cwd=cwd, check=True)


def ensure_checkout(harness_id: str, spec: dict[str, Any]) -> Path:
    target = upstream_dir(harness_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not (target / ".git").exists():
        run_checked(["git", "clone", "--filter=blob:none", spec["repo"], str(target)])
    run_checked(["git", "fetch", "--tags", "--prune", "origin"], cwd=target)
    run_checked(["git", "checkout", "--detach", spec["ref"]], cwd=target)
    return target


def setup_one(harness_id: str, spec: dict[str, Any]) -> None:
    target = ensure_checkout(harness_id, spec)
    adapter = spec["adapter"]
    if adapter == "starintel":
        print(f"{harness_id}: source pinned at {target}; Common Lisp runtime remains managed by the StarIntel harness.")
        return
    if shutil.which("uv") is None:
        raise SystemExit("uv is required for Python harness setup")
    py = venv_python(harness_id)
    py.parent.parent.mkdir(parents=True, exist_ok=True)
    run_checked(["uv", "venv", str(py.parent.parent), "--python", "3.11"])
    if adapter == "researchharness":
        run_checked(["uv", "pip", "install", "--python", str(py), "-r", str(target / "requirements.txt")])
        run_checked(["uv", "pip", "install", "--python", str(py), "-e", str(target), "--no-deps"])
    elif adapter == "gpt_researcher":
        run_checked(["uv", "pip", "install", "--python", str(py), "-e", str(target)])
    elif adapter == "smolagents_odr":
        req = target / "examples" / "open_deep_research" / "requirements.txt"
        run_checked(["uv", "pip", "install", "--python", str(py), "-r", str(req)])
        run_checked(["uv", "pip", "install", "--python", str(py), "-e", f"{target}[dev]"])
    else:
        print(f"{harness_id}: adapter '{adapter}' is scaffolded but has no setup recipe yet")


def missing_env(spec: dict[str, Any]) -> list[str]:
    return [name for name in spec.get("required_env", []) if not os.environ.get(name)]


def doctor(config: dict[str, Any]) -> int:
    print(f"lab root: {lab_root()}")
    print(f"default model: {os.environ.get('RESEARCH_LAB_MODEL', config['default_model'])}")
    print(f"OPENROUTER_API_KEY: {'set' if os.environ.get('OPENROUTER_API_KEY') else 'MISSING'}")
    print(f"git: {shutil.which('git') or 'MISSING'}")
    print(f"uv: {shutil.which('uv') or 'MISSING'}")
    bad = False
    for hid, spec in config["harnesses"].items():
        state = "enabled" if spec.get("enabled") else "disabled"
        missing = missing_env(spec)
        checkout = upstream_dir(hid)
        ready = checkout.exists()
        if spec["adapter"] == "starintel":
            native = Path(os.environ.get("STARINTEL_AUTO_RESEARCH_PATH", "")).expanduser()
            ready = ready or (native / "bin" / "research").exists()
        marker = "OK" if ready and not missing else "NEEDS-SETUP"
        if spec.get("enabled") and missing:
            bad = True
        print(f"{hid:18} {marker:11} {state:8} missing={','.join(missing) or '-'}")
    return 1 if bad else 0


def build_request(
    *,
    case_id: str,
    case_path: Path,
    question: str,
    model: str,
    timeout: int,
    guidelines: str,
) -> dict[str, Any]:
    prompt = (
        "Follow the Auto Research controlled-test contract below.\n\n"
        "=== AUTO RESEARCH CONTRACT ===\n"
        f"{guidelines}\n\n"
        "=== RESEARCH TASK ===\n"
        f"{question}\n"
    )
    return {
        "schema": "research-harness-test.request.v1",
        "case_id": case_id,
        "case_file": str(case_path.relative_to(ROOT)),
        "question": question,
        "guidelines": guidelines,
        "prompt": prompt,
        "model": model,
        "provider": "openrouter",
        "api_base": OPENROUTER_BASE,
        "timeout_seconds": timeout,
    }


def command_for(
    harness_id: str,
    spec: dict[str, Any],
    request_path: Path,
    output_dir: Path,
) -> tuple[list[str], Path, dict[str, str]]:
    adapter = spec["adapter"]
    env = os.environ.copy()
    env["RESEARCH_LAB_HARNESS"] = harness_id
    env["RESEARCH_LAB_OUTPUT"] = str(output_dir)
    env["OPENAI_API_KEY"] = env.get("OPENROUTER_API_KEY", "")
    env["OPENAI_BASE_URL"] = OPENROUTER_BASE
    env["SERPER_API_KEY"] = env.get("SERPER_API_KEY") or env.get("SERPER_KEY", "")

    if adapter == "starintel":
        native = Path(env.get("STARINTEL_AUTO_RESEARCH_PATH", "")).expanduser()
        target = native if (native / "bin" / "research").exists() else upstream_dir(harness_id)
        request = json.loads(request_path.read_text())
        env["AR_MODEL"] = request["model"]
        env["AR_DEFAULT_PROJECT"] = env.get("STARINTEL_PROJECT", "auto-research")
        env["AR_CORPUS_ROOT"] = str(target)
        cmd = [str(target / "bin" / "research"), "-p", env["AR_DEFAULT_PROJECT"], request["prompt"]]
        return cmd, target, env

    py = venv_python(harness_id)
    if not py.exists():
        raise RuntimeError(f"{harness_id}: missing venv; run 'research-lab setup --harnesses {harness_id}'")
    target = upstream_dir(harness_id)
    cmd = [
        str(py),
        str(ROOT / "scripts" / "adapter_entry.py"),
        adapter,
        "--upstream",
        str(target),
        "--request",
        str(request_path),
        "--output",
        str(output_dir),
    ]
    return cmd, target, env


async def exec_process(
    cmd: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
) -> tuple[int, bool, float]:
    started = time.monotonic()
    with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
        proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(cwd), env=env, stdout=stdout, stderr=stderr)
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        duration = time.monotonic() - started
    return int(proc.returncode or 0), timed_out, duration


async def run_one(
    *,
    semaphore: asyncio.Semaphore,
    config: dict[str, Any],
    harness_id: str,
    request: dict[str, Any],
    strict: bool,
    dry_run: bool,
) -> dict[str, Any]:
    async with semaphore:
        spec = config["harnesses"][harness_id]
        run_id = f"{time.strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}"
        out = ROOT / "runs" / request["case_id"] / harness_id / run_id
        out.mkdir(parents=True, exist_ok=False)
        req_path = out / "request.json"
        req_path.write_text(json.dumps({**request, "harness_id": harness_id, "run_id": run_id}, indent=2) + "\n")

        missing = missing_env(spec)
        if missing:
            result = {
                "schema": "research-harness-test.result.v1",
                "harness_id": harness_id,
                "run_id": run_id,
                "status": "failed" if strict else "skipped",
                "reason": f"missing environment: {', '.join(missing)}",
            }
            (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(f"[{harness_id}] {result['status']}: {result['reason']}")
            return result

        try:
            cmd, cwd, env = command_for(harness_id, spec, req_path, out)
        except Exception as exc:
            result = {
                "schema": "research-harness-test.result.v1",
                "harness_id": harness_id,
                "run_id": run_id,
                "status": "failed",
                "reason": str(exc),
            }
            (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(f"[{harness_id}] failed: {exc}")
            return result

        (out / "command.json").write_text(json.dumps({"argv": cmd, "cwd": str(cwd)}, indent=2) + "\n")
        if dry_run:
            result = {
                "schema": "research-harness-test.result.v1",
                "harness_id": harness_id,
                "run_id": run_id,
                "status": "planned",
                "model": request["model"],
                "argv": cmd,
            }
            (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
            print(f"[{harness_id}] planned")
            return result

        print(f"[{harness_id}] running {request['model']}")
        code, timed_out, duration = await exec_process(
            cmd,
            cwd=cwd,
            env=env,
            timeout=int(request["timeout_seconds"]),
            stdout_path=out / "stdout.log",
            stderr_path=out / "stderr.log",
        )
        report_path = out / "report.md"
        if not report_path.exists():
            stdout_text = (out / "stdout.log").read_text(errors="replace")
            report_path.write_text(stdout_text)
        status = "timeout" if timed_out else ("completed" if code == 0 else "failed")
        result = {
            "schema": "research-harness-test.result.v1",
            "harness_id": harness_id,
            "harness_name": spec["name"],
            "run_id": run_id,
            "status": status,
            "exit_code": code,
            "timed_out": timed_out,
            "duration_seconds": round(duration, 3),
            "model": request["model"],
            "provider": "openrouter",
            "report": str(report_path.relative_to(ROOT)),
            "upstream_ref": spec["ref"],
        }
        adapter_meta = out / "adapter-result.json"
        if adapter_meta.exists():
            try:
                result["adapter"] = json.loads(adapter_meta.read_text())
            except json.JSONDecodeError:
                pass
        (out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(f"[{harness_id}] {status} {duration:.1f}s -> {report_path.relative_to(ROOT)}")
        return result


async def matrix(args: argparse.Namespace, config: dict[str, Any]) -> int:
    case_id, case_path, question = resolve_case(args.case)
    model = args.model or os.environ.get("RESEARCH_LAB_MODEL") or config["default_model"]
    timeout = args.timeout or int(os.environ.get("RESEARCH_LAB_TIMEOUT", config["default_timeout_seconds"]))
    parallel = args.parallel or int(os.environ.get("RESEARCH_LAB_PARALLEL", config["default_parallel"]))
    guidelines = GUIDELINES_PATH.read_text()
    request = build_request(
        case_id=case_id,
        case_path=case_path,
        question=question,
        model=model,
        timeout=timeout,
        guidelines=guidelines,
    )
    ids = selected_harnesses(config, args.harnesses)
    semaphore = asyncio.Semaphore(parallel)
    results = await asyncio.gather(
        *[
            run_one(
                semaphore=semaphore,
                config=config,
                harness_id=hid,
                request=request,
                strict=args.strict,
                dry_run=args.dry_run,
            )
            for hid in ids
        ]
    )
    completed = sum(r["status"] == "completed" for r in results)
    failed = [r for r in results if r["status"] in {"failed", "timeout"}]
    print(f"matrix: {completed}/{len(results)} completed; {len(failed)} failed/timeout")
    return 1 if failed else 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research-lab")
    sub = p.add_subparsers(dest="command", required=True)

    setup = sub.add_parser("setup", help="clone pinned upstreams and create isolated Python venvs")
    setup.add_argument("--harnesses", help="comma-separated harness ids; defaults to matrix set")

    sub.add_parser("doctor", help="check keys, checkouts, and local tooling")

    mx = sub.add_parser("matrix", help="run several harnesses concurrently")
    mx.add_argument("--case", required=True)
    mx.add_argument("--harnesses", help="comma-separated harness ids")
    mx.add_argument("--parallel", type=int)
    mx.add_argument("--model")
    mx.add_argument("--timeout", type=int)
    mx.add_argument("--strict", action="store_true", help="missing harness prerequisites count as failures")
    mx.add_argument("--dry-run", action="store_true", help="write plans without spending API money")

    run = sub.add_parser("run", help="run one harness")
    run.add_argument("--case", required=True)
    run.add_argument("--harness", required=True)
    run.add_argument("--model")
    run.add_argument("--timeout", type=int)
    run.add_argument("--strict", action="store_true")
    run.add_argument("--dry-run", action="store_true")
    return p


def main() -> int:
    load_dotenv(ROOT / ".env")
    config = load_config()
    args = parser().parse_args()
    if args.command == "setup":
        for hid in selected_harnesses(config, args.harnesses):
            setup_one(hid, config["harnesses"][hid])
        return 0
    if args.command == "doctor":
        return doctor(config)
    if args.command == "run":
        args.harnesses = args.harness
        args.parallel = 1
        return asyncio.run(matrix(args, config))
    if args.command == "matrix":
        return asyncio.run(matrix(args, config))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
