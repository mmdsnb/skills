"""Tests for history.py using fake qoder logs and a fake opencode database."""

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "history.py"


def slug(path):
    text = str(path)
    for char in ("/", "\\", "."):
        text = text.replace(char, "-")
    return text


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")


class HistoryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "proj"
        self.project.mkdir()
        self.home = self.root / "qoder"
        self.session_dir = self.home / "projects" / slug(self.project)
        self.session_dir.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_script(self, *args):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT), "--project", str(self.project), *args],
            capture_output=True, text=True, timeout=30,
        )
        return proc

    def test_qoder_filters_noise_and_sidechain(self):
        write_jsonl(self.session_dir / "s1.jsonl", [
            {"type": "user", "origin": {"kind": "human"},
             "humanInput": {"text": "把规范写成 SKILL.md"}, "timestamp": "2026-11-01T10:00:00Z"},
            {"type": "user", "origin": {"kind": "system"},
             "humanInput": {"text": "系统注入"}, "timestamp": "2026-11-01T10:01:00Z"},
            {"type": "user", "origin": {"kind": "human"}, "isSidechain": True,
             "humanInput": {"text": "子 agent 提示"}, "timestamp": "2026-11-01T10:02:00Z"},
            {"type": "user", "origin": {"kind": "human"},
             "humanInput": {"text": "This session is being continued from a previous conversation"},
             "timestamp": "2026-11-01T10:03:00Z"},
            {"type": "user", "origin": {"kind": "human"},
             "message": {"role": "user", "content": [{"type": "tool_result", "content": "ok"}]},
             "timestamp": "2026-11-01T10:04:00Z"},
        ])
        proc = self.run_script("--qoder-home", str(self.home), "--source", "qoder", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["stats"]["prompts"], 1)
        self.assertEqual(data["messages"][0]["text"], "把规范写成 SKILL.md")

    def test_dedup_counts_repeats(self):
        for name in ("a", "b"):
            write_jsonl(self.session_dir / f"{name}.jsonl", [
                {"type": "user", "origin": {"kind": "human"},
                 "humanInput": {"text": "不要重复解释 AI 已知的内容"}, "timestamp": "2026-11-02T10:00:00Z"},
            ])
        proc = self.run_script("--qoder-home", str(self.home), "--source", "qoder", "--json")
        data = json.loads(proc.stdout)
        self.assertEqual(data["stats"]["unique"], 1)
        self.assertEqual(data["stats"]["duplicates"], 1)
        self.assertEqual(data["messages"][0]["repeats"], 2)

    def test_digest_output(self):
        write_jsonl(self.session_dir / "s.jsonl", [
            {"type": "user", "origin": {"kind": "human"},
             "humanInput": {"text": "验收看真实任务，不看页面效果"}, "timestamp": "2026-11-03T10:00:00Z"},
        ])
        out = self.root / "digest.txt"
        proc = self.run_script("--qoder-home", str(self.home), "--source", "qoder", "--out", str(out))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("验收看真实任务", out.read_text(encoding="utf-8"))

    def test_opencode_database(self):
        db = self.root / "opencode.db"
        conn = sqlite3.connect(db)
        conn.executescript("""
            CREATE TABLE session (id TEXT, parent_id TEXT, directory TEXT, time_created INTEGER);
            CREATE TABLE session_message (session_id TEXT, type TEXT, seq INTEGER, time_created INTEGER, data TEXT);
        """)
        conn.execute("INSERT INTO session VALUES ('s1', NULL, ?, 1)", (str(self.project),))
        conn.execute("INSERT INTO session VALUES ('s2', 's1', ?, 2)", (str(self.project),))
        conn.execute("INSERT INTO session_message VALUES ('s1', 'user', 0, 10, ?)",
                     (json.dumps({"text": "先找现成工具"}, ensure_ascii=False),))
        conn.execute("INSERT INTO session_message VALUES ('s1', 'assistant', 1, 11, '{}')")
        conn.execute("INSERT INTO session_message VALUES ('s2', 'user', 0, 12, ?)",
                     (json.dumps({"text": "子会话不算"}, ensure_ascii=False),))
        conn.commit()
        conn.close()

        proc = self.run_script("--opencode-db", str(db), "--source", "opencode", "--json")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertEqual(data["stats"]["prompts"], 1)
        self.assertEqual(data["messages"][0]["text"], "先找现成工具")

    def test_missing_history_fails_clearly(self):
        proc = self.run_script("--qoder-home", str(self.root / "empty"), "--source", "qoder")
        self.assertEqual(proc.returncode, 1)
        data = json.loads(proc.stdout)
        self.assertFalse(data["ok"])
        self.assertIn("没有找到", data["error"])

    def test_negative_days_is_argument_error(self):
        proc = self.run_script("--days", "-1")
        self.assertEqual(proc.returncode, 2)
        self.assertFalse(json.loads(proc.stdout)["ok"])


if __name__ == "__main__":
    unittest.main()
