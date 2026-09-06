#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time

KIND = os.environ.get("FAKE_MCP_KIND", "brave")
MODE = os.environ.get("FAKE_MCP_MODE", "normal")


def reply(request_id, result=None, error=None):
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def tools():
    if KIND == "brave":
        return [
            {"name": "brave_web_search", "description": "fake web search", "inputSchema": {"type": "object"}},
            {"name": "brave_news_search", "description": "fake news search", "inputSchema": {"type": "object"}},
        ]
    return [{"name": "fetch", "description": "fake fetch", "inputSchema": {"type": "object"}}]


def call_tool(name, arguments):
    if MODE == "delay":
        time.sleep(1.0)
    if MODE == "malformed":
        return {"content": [{"type": "text", "text": "not json and no url"}]}
    if KIND == "brave" and name in {"brave_web_search", "brave_news_search"}:
        query = arguments.get("query", "")
        body = [
            {"title": f"Primary source for {query}", "url": "https://example.test/primary", "description": "primary"},
            {"title": f"Secondary source for {query}", "url": "https://example.test/secondary", "description": "secondary"},
        ]
        return {"content": [{"type": "text", "text": json.dumps(body)}]}
    if KIND == "fetch" and name == "fetch":
        url = arguments.get("url", "")
        return {"content": [{"type": "text", "text": f"# fetched\n\nContent from {url}"}]}
    return {"content": [{"type": "text", "text": "unsupported"}], "isError": True}


for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    try:
        message = json.loads(line)
    except json.JSONDecodeError:
        continue
    if "id" not in message:
        continue
    request_id = message["id"]
    method = message.get("method")
    params = message.get("params") or {}
    if method == "initialize":
        reply(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion", "2025-06-18"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": f"fake-{KIND}", "version": "1"},
            },
        )
    elif method == "tools/list":
        if MODE == "delay":
            time.sleep(1.0)
        reply(request_id, {"tools": tools()})
    elif method == "tools/call":
        reply(request_id, call_tool(params.get("name"), params.get("arguments") or {}))
    else:
        reply(request_id, error={"code": -32601, "message": f"unknown method: {method}"})
