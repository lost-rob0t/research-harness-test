#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from mcp_client import LabRetrieval

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def load_request(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def finish(output: Path, report: Any, metadata: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    text = report if isinstance(report, str) else str(report)
    (output / "report.md").write_text(text.rstrip() + "\n")
    metadata = {
        **metadata,
        "retrieval_contract": "brave-search-mcp+fetch-mcp",
        "mcp_metadata": "mcp.json",
        "mcp_events": "mcp-events.jsonl",
    }
    (output / "adapter-result.json").write_text(json.dumps(metadata, indent=2) + "\n")


def run_researchharness(upstream: Path, request: dict[str, Any], output: Path) -> None:
    sys.path.insert(0, str(upstream))
    from researchharness import run_agent, tool

    with LabRetrieval(output) as retrieval:
        @tool(name="brave_search", description="Search the public web using the lab's pinned Brave Search MCP server.", timeout_seconds=60)
        def brave_search(query: str, count: int = 10) -> str:
            return json.dumps(retrieval.search(query, count=count), ensure_ascii=False)

        @tool(name="fetch_url", description="Fetch and convert a public web URL using the lab's pinned Fetch MCP server.", timeout_seconds=60)
        def fetch_url(url: str, max_length: int = 20000) -> str:
            return retrieval.fetch(url, max_length=max_length)

        role_prompt = (
            request["guidelines"]
            + "\n\nCONTROLLED RETRIEVAL: use only brave_search for discovery and fetch_url for page retrieval. "
              "Do not invoke any other network/search mechanism. Preserve source URLs in the final report."
        )
        report = run_agent(
            request["question"],
            workspace_root=str(output / "workspace"),
            model_name=request["model"],
            api_key=os.environ["OPENROUTER_API_KEY"],
            api_base=OPENROUTER_BASE,
            timeout_seconds=min(240, int(request["timeout_seconds"])),
            max_runtime_seconds=int(request["timeout_seconds"]),
            trace_dir=str(output / "traces"),
            role_prompt=role_prompt,
            tools=[brave_search, fetch_url],
            omit_generate_params=["presence_penalty"],
            require_env=False,
        )
    finish(
        output,
        report,
        {
            "adapter": "researchharness",
            "model": request["model"],
            "provider": "openrouter",
            "guidelines_surface": "role_prompt",
            "retrieval_mode": "native-custom-tools",
            "allowed_network_tools": ["brave_search", "fetch_url"],
            "trace_dir": "traces",
        },
    )


def _retriever_handler(retrieval: LabRetrieval):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query).get("query", [""])[0]
            try:
                rows = retrieval.search_with_content(query, max_results=3)
                body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                self.send_response(502)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return Handler


async def run_gpt_researcher_async(upstream: Path, request: dict[str, Any], output: Path) -> None:
    sys.path.insert(0, str(upstream))
    model = f"openrouter:{request['model']}"
    os.environ["OPENAI_BASE_URL"] = OPENROUTER_BASE
    os.environ["OPENAI_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    os.environ["FAST_LLM"] = model
    os.environ["SMART_LLM"] = model
    os.environ["STRATEGIC_LLM"] = model
    os.environ["EMBEDDING"] = "openrouter:openai/text-embedding-3-small"
    os.environ["RETRIEVER"] = "custom"
    os.environ.setdefault("OPENROUTER_LIMIT_RPS", "4")

    with LabRetrieval(output) as retrieval:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _retriever_handler(retrieval))
        thread = threading.Thread(target=server.serve_forever, name="gpt-researcher-mcp-retriever", daemon=True)
        thread.start()
        os.environ["RETRIEVER_ENDPOINT"] = f"http://127.0.0.1:{server.server_port}/search"
        try:
            from gpt_researcher import GPTResearcher

            researcher = GPTResearcher(
                query=request["prompt"],
                report_type="research_report",
                config_path=None,
            )
            await researcher.conduct_research()
            report = await researcher.write_report()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    finish(
        output,
        report,
        {
            "adapter": "gpt-researcher",
            "model": model,
            "provider": "openrouter",
            "embedding": "openrouter:openai/text-embedding-3-small",
            "guidelines_surface": "query-prefix",
            "retriever": "custom",
            "retrieval_mode": "custom-retriever-bridge",
            "note": "GPT Researcher's custom retriever receives Brave Search MCP results whose URLs are hydrated by Fetch MCP. No Tavily/Serper/Jina path is enabled.",
        },
    )


def run_smolagents_odr(upstream: Path, request: dict[str, Any], output: Path) -> None:
    sys.path.insert(0, str(upstream / "src"))
    from smolagents import CodeAgent, LiteLLMModel, ToolCallingAgent, tool

    model = LiteLLMModel(
        model_id=f"openrouter/{request['model']}",
        api_key=os.environ["OPENROUTER_API_KEY"],
        api_base=OPENROUTER_BASE,
        custom_role_conversions={"tool-call": "assistant", "tool-response": "user"},
        max_completion_tokens=8192,
    )

    with LabRetrieval(output) as retrieval:
        @tool
        def brave_search(query: str, count: int = 10) -> str:
            """Search the public web with Brave Search MCP.

            Args:
                query: Research query to search for.
                count: Number of results to return, from 1 to 20.
            """
            return json.dumps(retrieval.search(query, count=count), ensure_ascii=False)

        @tool
        def fetch_url(url: str, max_length: int = 20000) -> str:
            """Fetch a public web page through Fetch MCP and return markdown-like text.

            Args:
                url: Public HTTP or HTTPS URL discovered through Brave Search.
                max_length: Maximum amount of extracted page text to return.
            """
            return retrieval.fetch(url, max_length=max_length)

        search_agent = ToolCallingAgent(
            model=model,
            tools=[brave_search, fetch_url],
            max_steps=20,
            verbosity_level=2,
            planning_interval=4,
            name="search_agent",
            description=(
                "Research the web using only brave_search and fetch_url. Search broadly, fetch primary sources, "
                "preserve contradictory evidence, and return findings with source URLs."
            ),
            provide_run_summary=True,
        )
        manager = CodeAgent(
            model=model,
            tools=[],
            max_steps=12,
            verbosity_level=2,
            additional_authorized_imports=["*"],
            planning_interval=4,
            managed_agents=[search_agent],
        )
        prompt = (
            request["prompt"]
            + "\n\nCONTROLLED RETRIEVAL: the managed search_agent has exactly two network capabilities: "
              "Brave Search MCP and Fetch MCP. Do not perform network access in generated Python code."
        )
        report = manager.run(prompt)

    finish(
        output,
        report,
        {
            "adapter": "smolagents-open-deep-research",
            "model": f"openrouter/{request['model']}",
            "provider": "openrouter",
            "guidelines_surface": "task-prefix",
            "retrieval_mode": "managed-agent-mcp-tools",
            "allowed_network_tools": ["brave_search", "fetch_url"],
            "mode": "text-only-controlled",
        },
    )


def run_scaffolded(adapter: str) -> None:
    raise SystemExit(f"adapter '{adapter}' is scaffolded but not enabled for controlled runs yet")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("adapter")
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    request = load_request(args.request)
    if args.adapter == "researchharness":
        run_researchharness(args.upstream, request, args.output)
    elif args.adapter == "gpt_researcher":
        asyncio.run(run_gpt_researcher_async(args.upstream, request, args.output))
    elif args.adapter == "smolagents_odr":
        run_smolagents_odr(args.upstream, request, args.output)
    else:
        run_scaffolded(args.adapter)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
