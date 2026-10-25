#!/usr/bin/env python3
"""Tests for outsource_agent.py using fake backends."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS_DIR / "outsource_agent.py"

CONFIG_TOML = """\
default_backend = "echo"

[backends.echo]
description = "echo prompt back"
command = ["cat"]

[backends.arg]
command = ["@@PYTHON@@", "-c", "import sys; print(sys.argv[1])", "{prompt}"]

[backends.fail]
command = ["sh", "-c", "echo boom >&2; exit 3"]

[backends.sleep]
command = ["sleep", "30"]

[backends.pwd]
command = ["sh", "-c", "pwd"]

[backends.think]
command = ["sh", "-c", "printf '</think>\\n\\nfinal answer\\n'"]
""".replace("@@PYTHON@@", sys.executable)


class OutsourceAgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="outsource-agent-test-")
        cls.root = Path(cls.tmp.name)
        cls.config = cls.root / "backends.toml"
        cls.config.write_text(CONFIG_TOML, encoding="utf-8")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def run_cli(
        self,
        *args: str,
        env: dict[str, str] | None = None,
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        full_env = os.environ.copy()
        full_env.pop("OUTSOURCE_AGENT_BACKEND", None)
        if env:
            full_env.update(env)
        return subprocess.run(
            [sys.executable, str(SCRIPT), "--config", str(self.config), *args],
            capture_output=True,
            text=True,
            timeout=60,
            env=full_env,
            input=stdin,
        )

    def test_echo_backend_returns_task(self) -> None:
        result = self.run_cli("run", "--backend", "echo", "--task", "hello world")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("hello world", result.stdout)
        self.assertIn("<task>", result.stdout)

    def test_task_from_stdin(self) -> None:
        result = self.run_cli(
            "run", "--backend", "echo", "--task", "-", stdin="stdin task"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("stdin task", result.stdout)

    def test_prompt_placeholder_mode(self) -> None:
        result = self.run_cli("run", "--backend", "arg", "--task", "argv task")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("argv task", result.stdout)

    def test_default_backend_from_config(self) -> None:
        result = self.run_cli("run", "--task", "default works")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("default works", result.stdout)

    def test_env_overrides_default(self) -> None:
        result = self.run_cli(
            "run", "--task", "x", env={"OUTSOURCE_AGENT_BACKEND": "fail"}
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("boom", result.stderr)

    def test_unknown_backend(self) -> None:
        result = self.run_cli("run", "--backend", "nope", "--task", "x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("未知后端", result.stderr)

    def test_backend_failure(self) -> None:
        result = self.run_cli("run", "--backend", "fail", "--task", "x")
        self.assertEqual(result.returncode, 1)
        self.assertIn("boom", result.stderr)

    def test_timeout(self) -> None:
        result = self.run_cli(
            "run", "--backend", "sleep", "--task", "x", "--timeout", "1"
        )
        self.assertEqual(result.returncode, 124)
        self.assertIn("超时", result.stderr)

    def test_json_output(self) -> None:
        result = self.run_cli(
            "run", "--backend", "echo", "--task", "json task", "--json"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["backend"], "echo")
        self.assertIn("json task", payload["result"])

    def test_empty_task(self) -> None:
        result = self.run_cli("run", "--backend", "echo", "--task", "   ")
        self.assertEqual(result.returncode, 2)
        self.assertIn("任务为空", result.stderr)

    def test_default_cwd_is_isolated(self) -> None:
        result = self.run_cli("run", "--backend", "pwd", "--task", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        cwd = Path(result.stdout.strip())
        self.assertTrue(cwd.name.startswith("outsource-agent-"))
        self.assertNotEqual(cwd, SCRIPTS_DIR.parent)
        self.assertFalse(cwd.exists(), "临时工作目录应在运行结束后清理")

    def test_dir_sets_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as workdir:
            result = self.run_cli(
                "run", "--backend", "pwd", "--task", "x", "--dir", workdir
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(result.stdout.strip()), Path(workdir).resolve())

    def test_missing_dir(self) -> None:
        result = self.run_cli(
            "run", "--backend", "pwd", "--task", "x", "--dir", "/no/such/dir"
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("--dir 不是目录", result.stderr)

    def test_think_tags_are_stripped(self) -> None:
        result = self.run_cli("run", "--backend", "think", "--task", "x")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "final answer")

    def test_backends_listing_json(self) -> None:
        result = self.run_cli("backends", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["config"], str(self.config))
        self.assertTrue(payload["exists"])
        self.assertIn("qoder", payload["backends"])
        self.assertIn("echo", payload["backends"])


if __name__ == "__main__":
    unittest.main()
