#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

OPENROUTER_BASE = "https://openrouter.ai/api/v1"


def load_request(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def finish(output: Path, report: Any, metadata: dict[str, Any]) -> None:
    output.mkdir(parents=True, exist_ok=True)
    text = report if isinstance(report, str) else str(report)
    (output / "report.md").write_text(text.rstrip() + "\n")
    (output / "adapter-result.json").write_text(json.dumps(metadata, indent=2) + "\n")


def run_researchharness(upstream: Path, request: dict[str, Any], output: Path) -> None:
    sys.path.insert(0, str(upstream))
    from researchharness import run_agent

    report = run_agent(
        request["question"],
        workspace_root=str(output / "workspace"),
        model_name=request["model"],
        api_key=os.environ["OPENROUTER_API_KEY"],
        api_base=OPENROUTER_BASE,
        timeout_seconds=min(240, int(request["timeout_seconds"])),
        max_runtime_seconds=int(request["timeout_seconds"]),
        trace_dir=str(output / "traces"),
        role_prompt=request["guidelines"],
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
            "trace_dir": "traces",
        },
    )


async def run_gpt_researcher_async(upstream: Path, request: dict[str, Any], output: Path) -> None:
    sys.path.insert(0, str(upstream))
    model = f"openrouter:{request['model']}"
    os.environ["OPENROUTER_API_KEY"] = os.environ["OPENROUTER_API_KEY"]
    os.environ["OPENAI_BASE_URL"] = OPENROUTER_BASE
    os.environ["FAST_LLM"] = model
    os.environ["SMART_LLM"] = model
    os.environ["STRATEGIC_LLM"] = model
    os.environ.setdefault("OPENROUTER_LIMIT_RPS", "4")
    os.environ.setdefault("RETRIEVER", "tavily")

    from gpt_researcher import GPTResearcher

    researcher = GPTResearcher(
        query=request["prompt"],
        report_type="research_report",
        config_path=None,
    )
    await researcher.conduct_research()
    report = await researcher.write_report()
    finish(
        output,
        report,
        {
            "adapter": "gpt-researcher",
            "model": model,
            "provider": "openrouter",
            "guidelines_surface": "query-prefix",
            "retriever": os.environ.get("RETRIEVER", "tavily"),
        },
    )


def run_smolagents_odr(upstream: Path, request: dict[str, Any], output: Path) -> None:
    example = upstream / "examples" / "open_deep_research"
    sys.path.insert(0, str(example))
    sys.path.insert(0, str(upstream / "src"))

    if os.environ.get("SERPER_KEY") and not os.environ.get("SERPER_API_KEY"):
        os.environ["SERPER_API_KEY"] = os.environ["SERPER_KEY"]

    from scripts.text_inspector_tool import TextInspectorTool
    from scripts.text_web_browser import (
        ArchiveSearchTool,
        FinderTool,
        FindNextTool,
        PageDownTool,
        PageUpTool,
        SimpleTextBrowser,
        VisitTool,
    )
    from smolagents import CodeAgent, GoogleSearchTool, LiteLLMModel, ToolCallingAgent

    model = LiteLLMModel(
        model_id=f"openrouter/{request['model']}",
        api_key=os.environ["OPENROUTER_API_KEY"],
        api_base=OPENROUTER_BASE,
        custom_role_conversions={"tool-call": "assistant", "tool-response": "user"},
        max_completion_tokens=8192,
    )
    downloads = output / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    browser = SimpleTextBrowser(
        viewport_size=1024 * 5,
        downloads_folder=str(downloads),
        request_kwargs={
            "headers": {
                "User-Agent": "Mozilla/5.0 ResearchHarnessTest/1.0",
            },
            "timeout": 300,
        },
        serpapi_key=os.environ.get("SERPAPI_API_KEY"),
    )
    text_limit = 100000
    web_tools = [
        GoogleSearchTool(provider="serper"),
        VisitTool(browser),
        PageUpTool(browser),
        PageDownTool(browser),
        FinderTool(browser),
        FindNextTool(browser),
        ArchiveSearchTool(browser),
        TextInspectorTool(model, text_limit),
    ]
    search_agent = ToolCallingAgent(
        model=model,
        tools=web_tools,
        max_steps=20,
        verbosity_level=2,
        planning_interval=4,
        name="search_agent",
        description=(
            "Search the web for primary and high-quality sources, inspect the pages, "
            "preserve contradictory evidence, and return source URLs with findings."
        ),
        provide_run_summary=True,
    )
    manager = CodeAgent(
        model=model,
        tools=[TextInspectorTool(model, text_limit)],
        max_steps=12,
        verbosity_level=2,
        additional_authorized_imports=["*"],
        planning_interval=4,
        managed_agents=[search_agent],
    )
    report = manager.run(request["prompt"])
    finish(
        output,
        report,
        {
            "adapter": "smolagents-open-deep-research",
            "model": f"openrouter/{request['model']}",
            "provider": "openrouter",
            "guidelines_surface": "task-prefix",
            "search_provider": "serper",
            "mode": "text-only-controlled",
            "note": "Keeps the upstream text-browser/search-agent architecture while omitting the Hugging Face visualizer for text-only controlled cases.",
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
