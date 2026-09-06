#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "mcp" / "servers.json"
PROTOCOL_VERSION = "2025-06-18"
URL_RE = re.compile(r"https?://[^\s<>\]\)\"']+")
MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)]+)\)")


class MCPError(RuntimeError):
    pass


def load_mcp_config(path: Path | None = None) -> dict[str, Any]:
    configured = path or Path(os.environ.get("RESEARCH_MCP_CONFIG", str(DEFAULT_CONFIG)))
    if not configured.is_absolute():
        configured = ROOT / configured
    payload = json.loads(configured.read_text())
    if payload.get("schema") != "research-harness-test.mcp.v1":
        raise MCPError(f"unsupported MCP config schema in {configured}")
    if not isinstance(payload.get("servers"), dict):
        raise MCPError(f"invalid MCP server map in {configured}")
    return payload


def _expand(value: str, env: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        return env.get(name, "")

    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", replace, value)


def resolved_server(spec: dict[str, Any], base_env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    env = dict(base_env or os.environ)
    command = str(spec["command"])
    args = [_expand(str(arg), env) for arg in spec.get("args", [])]
    for key, value in spec.get("env", {}).items():
        env[str(key)] = _expand(str(value), env)
    return [command, *args], env


def public_server_metadata(server_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": server_id,
        "name": spec.get("name", server_id),
        "package": spec.get("package"),
        "version": spec.get("version"),
        "transport": spec.get("transport", "stdio"),
        "command": spec.get("command"),
        "args": list(spec.get("args", [])),
    }


def content_text(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result or "")
    chunks: list[str] = []
    for item in result.get("content", []) or []:
        if isinstance(item, dict) and item.get("type") == "text":
            chunks.append(str(item.get("text", "")))
    if chunks:
        return "\n".join(chunks)
    if "structuredContent" in result:
        return json.dumps(result["structuredContent"], ensure_ascii=False)
    return json.dumps(result, ensure_ascii=False)


class MCPStdioClient:
    def __init__(
        self,
        server_id: str,
        spec: dict[str, Any],
        *,
        timeout: float = 30.0,
        cwd: Path = ROOT,
        stderr_path: Path | None = None,
    ) -> None:
        self.server_id = server_id
        self.spec = spec
        self.timeout = timeout
        self.cwd = cwd
        self.stderr_path = stderr_path
        self.proc: subprocess.Popen[str] | None = None
        self._stderr_handle = None
        self._messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self._ids = 0
        self.server_info: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.tools: list[dict[str, Any]] = []
        self._request_lock = threading.Lock()

    def start(self) -> "MCPStdioClient":
        if self.proc is not None:
            return self
        argv, env = resolved_server(self.spec)
        if self.stderr_path:
            self.stderr_path.parent.mkdir(parents=True, exist_ok=True)
            self._stderr_handle = self.stderr_path.open("a", encoding="utf-8")
            stderr: Any = self._stderr_handle
        else:
            stderr = subprocess.DEVNULL
        try:
            self.proc = subprocess.Popen(
                argv,
                cwd=str(self.cwd),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=stderr,
                text=True,
                bufsize=1,
            )
            if self.proc.stdin is None or self.proc.stdout is None:
                raise MCPError(f"{self.server_id}: failed to open stdio pipes")
            threading.Thread(target=self._read_stdout, name=f"mcp-{self.server_id}-reader", daemon=True).start()
            result = self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "research-harness-test", "version": "1"},
                },
            )
            self.protocol_version = str(result.get("protocolVersion", "")) if isinstance(result, dict) else None
            self.server_info = dict(result.get("serverInfo", {})) if isinstance(result, dict) else {}
            self.notify("notifications/initialized", {})
            listed = self.request("tools/list", {})
            self.tools = list(listed.get("tools", [])) if isinstance(listed, dict) else []
            return self
        except Exception:
            self.close()
            raise

    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            line = raw.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(message, dict):
                self._messages.put(message)

    def _send(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None:
            raise MCPError(f"{self.server_id}: MCP process is not running")
        if self.proc.poll() is not None:
            raise MCPError(f"{self.server_id}: MCP process exited with {self.proc.returncode}")
        try:
            self.proc.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            returncode = self.proc.poll()
            suffix = f" with {returncode}" if returncode is not None else ""
            raise MCPError(f"{self.server_id}: MCP process closed stdio{suffix}") from exc

    def request(self, method: str, params: dict[str, Any]) -> Any:
        with self._request_lock:
            self._ids += 1
            request_id = self._ids
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            deadline = time.monotonic() + self.timeout
            while True:
                if self.proc is not None and self.proc.poll() is not None and self._messages.empty():
                    raise MCPError(
                        f"{self.server_id}: MCP process exited with {self.proc.returncode} while waiting for {method}"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise MCPError(f"{self.server_id}: timeout waiting for {method}")
                try:
                    message = self._messages.get(timeout=min(remaining, 0.1))
                except queue.Empty:
                    continue
                if message.get("id") != request_id:
                    continue
                if "error" in message:
                    raise MCPError(f"{self.server_id}: {method} failed: {message['error']}")
                return message.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not any(tool.get("name") == name for tool in self.tools):
            raise MCPError(f"{self.server_id}: tool not advertised: {name}")
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        return result if isinstance(result, dict) else {"content": [{"type": "text", "text": str(result)}]}

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        if self._stderr_handle is not None:
            self._stderr_handle.close()
            self._stderr_handle = None

    def __enter__(self) -> "MCPStdioClient":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def _walk_results(value: Any, out: list[dict[str, str]]) -> None:
    if isinstance(value, dict):
        url = value.get("url") or value.get("href") or value.get("link")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            out.append(
                {
                    "url": url,
                    "title": str(value.get("title") or value.get("name") or url),
                    "snippet": str(value.get("description") or value.get("snippet") or value.get("body") or ""),
                }
            )
        for child in value.values():
            _walk_results(child, out)
    elif isinstance(value, list):
        for child in value:
            _walk_results(child, out)


def parse_search_results(text: str, limit: int = 10) -> list[dict[str, str]]:
    found: list[dict[str, str]] = []
    stripped = text.strip()
    if stripped:
        try:
            _walk_results(json.loads(stripped), found)
        except json.JSONDecodeError:
            pass
    if not found:
        for title, url in MD_LINK_RE.findall(text):
            found.append({"url": url, "title": title.strip() or url, "snippet": ""})
    if not found:
        for url in URL_RE.findall(text):
            found.append({"url": url.rstrip(".,;"), "title": url, "snippet": ""})
    unique: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in found:
        url = item["url"]
        if url in seen:
            continue
        seen.add(url)
        unique.append(item)
        if len(unique) >= limit:
            break
    return unique


class LabRetrieval:
    def __init__(self, output_dir: Path, *, config_path: Path | None = None, timeout: float = 45.0) -> None:
        self.output_dir = output_dir
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.config = load_mcp_config(config_path)
        self.timeout = timeout
        self.clients: dict[str, MCPStdioClient] = {}
        self.used_tools: set[str] = set()
        self.events_path = self.output_dir / "mcp-events.jsonl"
        self.metadata_path = self.output_dir / "mcp.json"
        self._event_lock = threading.Lock()

    def start(self) -> "LabRetrieval":
        try:
            for server_id in ("brave", "fetch"):
                spec = self.config["servers"][server_id]
                client = MCPStdioClient(
                    server_id,
                    spec,
                    timeout=self.timeout,
                    stderr_path=self.output_dir / f"mcp-{server_id}.stderr.log",
                ).start()
                self.clients[server_id] = client
            self._write_metadata()
            return self
        except Exception:
            self.close()
            raise

    def _write_metadata(self) -> None:
        servers: list[dict[str, Any]] = []
        for server_id in ("brave", "fetch"):
            spec = self.config["servers"][server_id]
            client = self.clients.get(server_id)
            item = public_server_metadata(server_id, spec)
            item["protocol_version"] = client.protocol_version if client else None
            item["server_info"] = client.server_info if client else {}
            item["available_tools"] = [tool.get("name") for tool in client.tools] if client else []
            servers.append(item)
        self.metadata_path.write_text(
            json.dumps(
                {
                    "schema": "research-harness-test.mcp-run.v1",
                    "servers": servers,
                    "used_tools": sorted(self.used_tools),
                },
                indent=2,
            )
            + "\n"
        )

    def _event(self, payload: dict[str, Any]) -> None:
        public = {"at": time.time(), **payload}
        with self._event_lock:
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(public, ensure_ascii=False) + "\n")

    def _tool(self, server_id: str, name: str, arguments: dict[str, Any], *, event: dict[str, Any]) -> str:
        started = time.monotonic()
        try:
            result = self.clients[server_id].call_tool(name, arguments)
            duration = time.monotonic() - started
            self.used_tools.add(f"{server_id}:{name}")
            self._event({**event, "server": server_id, "tool": name, "success": True, "duration_seconds": round(duration, 4)})
            self._write_metadata()
            return content_text(result)
        except Exception as exc:
            duration = time.monotonic() - started
            self._event(
                {
                    **event,
                    "server": server_id,
                    "tool": name,
                    "success": False,
                    "duration_seconds": round(duration, 4),
                    "error": str(exc)[:500],
                }
            )
            raise

    def search(self, query: str, count: int = 10) -> list[dict[str, str]]:
        text = self._tool(
            "brave",
            "brave_web_search",
            {"query": query, "count": max(1, min(int(count), 20))},
            event={"kind": "search", "query": query, "requested_count": count},
        )
        results = parse_search_results(text, limit=count)
        self._event({"kind": "search-normalized", "query": query, "result_count": len(results), "success": True})
        return results

    def news(self, query: str, count: int = 10) -> list[dict[str, str]]:
        tools = {tool.get("name") for tool in self.clients["brave"].tools}
        if "brave_news_search" not in tools:
            return self.search(query, count=count)
        text = self._tool(
            "brave",
            "brave_news_search",
            {"query": query, "count": max(1, min(int(count), 20))},
            event={"kind": "news-search", "query": query, "requested_count": count},
        )
        return parse_search_results(text, limit=count)

    def fetch(self, url: str, max_length: int = 20000) -> str:
        return self._tool(
            "fetch",
            "fetch",
            {"url": url, "max_length": max(1000, min(int(max_length), 100000))},
            event={"kind": "fetch", "url": url},
        )

    def search_with_content(self, query: str, max_results: int = 5) -> list[dict[str, str]]:
        rows: list[dict[str, str]] = []
        for item in self.search(query, count=max_results):
            url = item["url"]
            try:
                raw = self.fetch(url, max_length=16000)
            except Exception as exc:
                self._event({"kind": "fetch-normalized", "url": url, "success": False, "error": str(exc)[:500]})
                continue
            rows.append({"url": url, "raw_content": raw})
        return rows

    def close(self) -> None:
        for client in reversed(list(self.clients.values())):
            client.close()
        self.clients.clear()
        self._write_metadata()

    def __enter__(self) -> "LabRetrieval":
        return self.start()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()
