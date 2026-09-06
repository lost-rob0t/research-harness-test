#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path

from mcp_client import LabRetrieval, MCPError, load_mcp_config, public_server_metadata, resolved_server

ROOT = Path(__file__).resolve().parents[1]


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
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), os.path.expandvars(os.path.expanduser(value)))


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def cmd_list() -> int:
    config = load_mcp_config()
    for server_id, spec in config["servers"].items():
        meta = public_server_metadata(server_id, spec)
        argv, _ = resolved_server(spec)
        print(f"{server_id}: {meta['package']}@{meta['version']} transport={meta['transport']}")
        print("  " + " ".join(argv))
    return 0


def cmd_exec(server_id: str) -> int:
    config = load_mcp_config()
    if server_id not in config["servers"]:
        raise SystemExit(f"unknown MCP server: {server_id}")
    argv, env = resolved_server(config["servers"][server_id])
    os.execvpe(argv[0], argv, env)
    return 0


def cmd_doctor() -> int:
    config = load_mcp_config()
    errors: list[str] = []
    print(f"config: {os.environ.get('RESEARCH_MCP_CONFIG', 'mcp/servers.json')}")
    for tool in ("npx", "uvx"):
        found = shutil.which(tool)
        print(f"{tool}: {found or 'MISSING'}")
        if not found:
            errors.append(f"missing executable: {tool}")
    for key in ("OPENROUTER_API_KEY", "BRAVE_API_KEY"):
        present = bool(os.environ.get(key))
        print(f"{key}: {'set' if present else 'MISSING'}")
        if not present:
            errors.append(f"missing environment: {key}")
    for server_id, spec in config["servers"].items():
        command = str(spec["command"])
        if not command_exists(command):
            errors.append(f"{server_id}: missing command {command}")
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    probe_dir = Path(os.environ.get("RESEARCH_LAB_ROOT", str(ROOT / ".lab"))) / "mcp-doctor"
    try:
        with LabRetrieval(probe_dir) as retrieval:
            for server_id, client in retrieval.clients.items():
                names = ", ".join(str(tool.get("name")) for tool in client.tools)
                print(f"{server_id}: OK [{names}]")
    except (MCPError, OSError, RuntimeError) as exc:
        print(f"ERROR: MCP probe failed: {exc}", file=sys.stderr)
        return 1
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="research-mcp")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="verify local programs, keys, initialize both servers, and list tools")
    sub.add_parser("list", help="show pinned repo-local MCP servers without starting them")
    sub.add_parser("probe", help="initialize both MCP servers and exit")
    sub.add_parser("brave", help="exec the pinned Brave Search MCP server over stdio")
    sub.add_parser("fetch", help="exec the pinned Fetch MCP server over stdio")
    return p


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parser().parse_args()
    if args.command == "list":
        return cmd_list()
    if args.command in {"doctor", "probe"}:
        return cmd_doctor()
    if args.command in {"brave", "fetch"}:
        return cmd_exec(args.command)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
