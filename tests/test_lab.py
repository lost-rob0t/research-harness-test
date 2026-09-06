from __future__ import annotations

import importlib.util
import json
import py_compile
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
SPEC = importlib.util.spec_from_file_location("research_lab", ROOT / "scripts" / "research_lab.py")
assert SPEC and SPEC.loader
lab = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lab)


class ResearchLabTests(unittest.TestCase):
    def test_default_model_and_matrix(self) -> None:
        config = lab.load_config()
        self.assertEqual(config["default_model"], "z-ai/glm-5.3-flash")
        self.assertEqual(config["default_parallel"], 4)
        self.assertEqual(
            config["default_harnesses"],
            ["starintel", "researchharness", "gpt-researcher", "smolagents-odr"],
        )
        self.assertEqual(config["retrieval"]["search"], "mcp:brave/brave_web_search")
        self.assertEqual(config["retrieval"]["fetch"], "mcp:fetch/fetch")

    def test_case_parser(self) -> None:
        case_id, path, question = lab.resolve_case("DR-001")
        self.assertEqual(case_id, "DR-001")
        self.assertEqual(path.name, "DR-001.org")
        self.assertIn("deep-research", question)
        self.assertNotIn("Evaluation notes", question)

    def test_org_parser_stops_at_next_heading(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "case.org"
            path.write_text("#+lab_id: X\n* Question\nhello\n* Other\nnope\n")
            self.assertEqual(lab.read_org_section(path, "Question"), "hello")

    def test_request_contains_shared_contract_task_and_retrieval_policy(self) -> None:
        case_id, path, question = lab.resolve_case("DR-001")
        guidelines = (ROOT / "guidelines" / "auto-research.org").read_text()
        request = lab.build_request(
            case_id=case_id,
            case_path=path,
            question=question,
            model="z-ai/glm-5.3-flash",
            timeout=600,
            guidelines=guidelines,
        )
        self.assertEqual(request["provider"], "openrouter")
        self.assertEqual(request["model"], "z-ai/glm-5.3-flash")
        self.assertEqual(request["search"], "mcp:brave/brave_web_search")
        self.assertEqual(request["fetch"], "mcp:fetch/fetch")
        self.assertIn("AUTO RESEARCH CONTRACT", request["prompt"])
        self.assertIn("CONTROLLED RETRIEVAL CONTRACT", request["prompt"])
        self.assertIn(question, request["prompt"])

    def test_config_is_json_refs_are_pinned_and_remotes_follow_host_policy(self) -> None:
        config = json.loads((ROOT / "config" / "harnesses.json").read_text())

        self.assertEqual(
            config["harnesses"]["starintel"]["repo"],
            "https://git.starintel.actor/starintel-labs/auto-research-llm-cl.git",
        )

        for harness_id, spec in config["harnesses"].items():
            repo = spec["repo"]
            if harness_id == "starintel":
                self.assertTrue(
                    repo.startswith("https://git.starintel.actor/"),
                    f"{harness_id} must use Forgejo HTTPS, got {repo}",
                )
            else:
                self.assertTrue(
                    repo.startswith("ssh://git@ssh.github.com:443/"),
                    f"{harness_id} must use GitHub SSH over 443, got {repo}",
                )

        for harness_id in config["default_harnesses"]:
            ref = config["harnesses"][harness_id]["ref"]
            self.assertRegex(ref, r"^[0-9a-f]{40}$")
            self.assertIn("BRAVE_API_KEY", config["harnesses"][harness_id]["required_env"])

    def test_nix_shell_exposes_openssl_and_prolog_rlm_runtime(self) -> None:
        flake = (ROOT / "flake.nix").read_text()
        self.assertIn("openssl", flake)
        self.assertIn("LD_LIBRARY_PATH", flake)
        self.assertIn("makeLibraryPath [ pkgs.openssl ]", flake)
        self.assertIn("swi-prolog", flake)
        self.assertIn("PROLOG_RLM_SWIPL", flake)
        self.assertIn("git+https://git.starintel.actor/nsaspy/prolog-rlm.git", flake)
        self.assertIn("e1ed6c2b5f717d9bb380fcf22d144737299197f8", flake)

    def test_python_entrypoints_compile(self) -> None:
        for path in (
            "scripts/research_lab.py",
            "scripts/setup_harnesses.py",
            "scripts/adapter_entry.py",
            "scripts/mcp_client.py",
            "scripts/research_mcp.py",
            "scripts/starintel_entry.py",
            "tests/fake_mcp_server.py",
        ):
            py_compile.compile(str(ROOT / path), doraise=True)


if __name__ == "__main__":
    unittest.main()
