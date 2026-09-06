from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from mcp_client import (
    LabRetrieval,
    MCPError,
    MCPStdioClient,
    bound_brave_query,
    load_mcp_config,
    public_server_metadata,
)

FAKE_CONFIG = ROOT / "tests" / "fixtures" / "mcp-servers.fake.json"


class MCPTests(unittest.TestCase):
    def test_real_config_pins_expected_servers(self) -> None:
        config = load_mcp_config(ROOT / "mcp" / "servers.json")
        self.assertEqual(config["servers"]["brave"]["version"], "2.1.3")
        self.assertEqual(config["servers"]["fetch"]["version"], "2026.8.18")
        self.assertEqual(config["servers"]["brave"]["transport"], "stdio")
        self.assertEqual(config["servers"]["fetch"]["transport"], "stdio")

    def test_fake_discovery_search_fetch_and_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"RESEARCH_MCP_CONFIG": str(FAKE_CONFIG)}, clear=False
        ):
            output = Path(td)
            with LabRetrieval(output) as retrieval:
                self.assertIn("brave_web_search", {t["name"] for t in retrieval.clients["brave"].tools})
                self.assertIn("fetch", {t["name"] for t in retrieval.clients["fetch"].tools})
                results = retrieval.search("symbolic research", count=2)
                self.assertEqual(len(results), 2)
                self.assertEqual(results[0]["url"], "https://example.test/primary")
                fetched = retrieval.fetch(results[0]["url"])
                self.assertIn("Content from https://example.test/primary", fetched)
            metadata = json.loads((output / "mcp.json").read_text())
            self.assertIn("brave:brave_web_search", metadata["used_tools"])
            self.assertIn("fetch:fetch", metadata["used_tools"])
            events = (output / "mcp-events.jsonl").read_text()
            self.assertIn('"kind": "search"', events)
            self.assertIn('"kind": "fetch"', events)

    def test_brave_query_is_bounded_to_server_contract(self) -> None:
        query = " ".join(f"word{i}" for i in range(100))
        bounded = bound_brave_query(query)
        self.assertLessEqual(len(bounded), 400)
        self.assertLessEqual(len(bounded.split()), 50)
        self.assertTrue(bounded)

        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"RESEARCH_MCP_CONFIG": str(FAKE_CONFIG)}, clear=False
        ):
            output = Path(td)
            with LabRetrieval(output) as retrieval:
                self.assertEqual(len(retrieval.search(query, count=2)), 2)
            events = [json.loads(line) for line in (output / "mcp-events.jsonl").read_text().splitlines()]
            search = next(event for event in events if event["kind"] == "search")
            self.assertEqual(search["query"], query)
            self.assertEqual(search["sent_query"], bounded)

    def test_official_brave_json_line_shape_preserves_metadata(self) -> None:
        payload = json.loads(FAKE_CONFIG.read_text())
        payload["servers"]["brave"]["env"]["FAKE_MCP_MODE"] = "json-lines"
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps(payload))
            with patch.dict(os.environ, {"RESEARCH_MCP_CONFIG": str(config_path)}, clear=False):
                with LabRetrieval(Path(td) / "out") as retrieval:
                    results = retrieval.search("shape test", count=2)
        self.assertEqual(results[0]["title"], "Primary source for shape test")
        self.assertEqual(results[0]["snippet"], "primary")
        self.assertEqual(results[1]["url"], "https://example.test/secondary")

    def test_mcp_tool_is_error_is_failure_not_empty_success(self) -> None:
        payload = json.loads(FAKE_CONFIG.read_text())
        payload["servers"]["brave"]["env"]["FAKE_MCP_MODE"] = "tool-error"
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps(payload))
            output = Path(td) / "out"
            with patch.dict(os.environ, {"RESEARCH_MCP_CONFIG": str(config_path)}, clear=False):
                with LabRetrieval(output) as retrieval:
                    with self.assertRaisesRegex(MCPError, "No web results found"):
                        retrieval.search("anything")
            events = [json.loads(line) for line in (output / "mcp-events.jsonl").read_text().splitlines()]
            failed = next(event for event in events if event["kind"] == "search")
            self.assertFalse(failed["success"])

    def test_concurrent_isolated_clients(self) -> None:
        def one(index: int) -> int:
            with tempfile.TemporaryDirectory() as td, patch.dict(
                os.environ, {"RESEARCH_MCP_CONFIG": str(FAKE_CONFIG)}, clear=False
            ):
                with LabRetrieval(Path(td)) as retrieval:
                    return len(retrieval.search(f"query {index}", count=2))

        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertEqual(list(pool.map(one, range(4))), [2, 2, 2, 2])

    def test_malformed_search_result_is_empty_not_crash(self) -> None:
        payload = json.loads(FAKE_CONFIG.read_text())
        payload["servers"]["brave"]["env"]["FAKE_MCP_MODE"] = "malformed"
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps(payload))
            with patch.dict(os.environ, {"RESEARCH_MCP_CONFIG": str(config_path)}, clear=False):
                with LabRetrieval(Path(td) / "out") as retrieval:
                    self.assertEqual(retrieval.search("anything"), [])

    def test_timeout_cleans_probe_process(self) -> None:
        payload = json.loads(FAKE_CONFIG.read_text())
        payload["servers"]["brave"]["env"]["FAKE_MCP_MODE"] = "delay"
        spec = payload["servers"]["brave"]
        client = MCPStdioClient("brave", spec, timeout=0.05)
        with self.assertRaises(MCPError):
            client.start()
        self.assertIsNone(client.proc)

    def test_dead_mcp_process_fails_fast(self) -> None:
        payload = json.loads(FAKE_CONFIG.read_text())
        client = MCPStdioClient("brave", payload["servers"]["brave"], timeout=2.0).start()
        self.assertIsNotNone(client.proc)
        assert client.proc is not None
        client.proc.terminate()
        client.proc.wait(timeout=2)
        started = __import__("time").monotonic()
        with self.assertRaises(MCPError):
            client.call_tool("brave_web_search", {"query": "dead child", "count": 1})
        self.assertLess(__import__("time").monotonic() - started, 0.5)
        client.close()

    def test_partial_pair_startup_cleans_first_server(self) -> None:
        payload = json.loads(FAKE_CONFIG.read_text())
        payload["servers"]["fetch"]["command"] = "definitely-not-a-real-mcp-command"
        with tempfile.TemporaryDirectory() as td:
            config_path = Path(td) / "mcp.json"
            config_path.write_text(json.dumps(payload))
            retrieval = LabRetrieval(Path(td) / "out", config_path=config_path, timeout=0.5)
            with self.assertRaises(OSError):
                retrieval.start()
            self.assertEqual(retrieval.clients, {})

    def test_public_metadata_never_resolves_secrets(self) -> None:
        config = load_mcp_config(ROOT / "mcp" / "servers.json")
        with patch.dict(os.environ, {"BRAVE_API_KEY": "supersecret-value"}, clear=False):
            text = json.dumps(public_server_metadata("brave", config["servers"]["brave"]))
        self.assertNotIn("supersecret-value", text)
        self.assertNotIn("BRAVE_API_KEY", text)

    def test_controlled_path_has_no_forbidden_provider_credentials(self) -> None:
        text = "\n".join(
            (ROOT / path).read_text()
            for path in ["env.example", "config/harnesses.json", "scripts/adapter_entry.py"]
        )
        for name in (
            "TAVILY_API_KEY",
            "SERPER_KEY",
            "SERPER_API_KEY",
            "SERPAPI_API_KEY",
            "JINA_KEY",
            "MINERU_TOKEN",
            "HF_TOKEN",
        ):
            self.assertNotIn(name, text)
        self.assertNotIn("GoogleSearchTool", text)
        self.assertNotIn("SimpleTextBrowser", text)

    def test_dry_run_requires_no_credentials_or_setup(self) -> None:
        env = os.environ.copy()
        for key in ("OPENROUTER_API_KEY", "BRAVE_API_KEY", "AR_RUNTIME_SOURCE"):
            env.pop(key, None)
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "research_lab.py"),
                "matrix",
                "--case",
                "DR-001",
                "--parallel",
                "4",
                "--model",
                "z-ai/glm-5.3-flash",
                "--dry-run",
            ],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("4 planned", completed.stdout)


if __name__ == "__main__":
    unittest.main()
