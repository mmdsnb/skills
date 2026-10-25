from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "voter.py"
sys.path.insert(0, str(SCRIPT.parent))

import voter  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def run_cli(*args: str, input_text: str | None = None, timeout: float = 30):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def http_json(url: str, method: str = "GET", payload: dict | None = None):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with OPENER.open(request, timeout=5) as response:
            body = response.read()
            try:
                parsed = json.loads(body) if body else None
            except ValueError:
                parsed = None
            return response.status, parsed, body
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        return exc.code, parsed, body


class MarkdownTests(unittest.TestCase):
    def test_emphasis_and_code(self):
        out = voter.render_markdown("**bold** and *soft* and `a<b>`")
        self.assertIn("<strong>bold</strong>", out)
        self.assertIn("<em>soft</em>", out)
        self.assertIn("<code>a&lt;b&gt;</code>", out)

    def test_html_is_escaped(self):
        out = voter.render_markdown("<script>alert(1)</script>")
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_javascript_link_is_not_rendered(self):
        out = voter.render_markdown("[x](javascript:alert(1))")
        self.assertNotIn("href", out)

    def test_link_url_with_asterisk_is_not_mangled(self):
        out = voter.render_markdown("[x](https://e.com/a*b)")
        self.assertIn('href="https://e.com/a*b"', out)
        self.assertNotIn("<em>", out)

    def test_image_and_link(self):
        out = voter.render_markdown("![alt](https://e.com/a.png) [x](https://e.com)")
        self.assertIn('<img src="https://e.com/a.png" alt="alt" loading="lazy">', out)
        self.assertIn('href="https://e.com"', out)

    def test_blocks(self):
        source = "# Title\n\n- one\n- two\n\n1. first\n2. second\n\n> quoted\n\n---\n\n```python\nprint(1 < 2)\n```"
        out = voter.render_markdown(source)
        self.assertIn("<h1>Title</h1>", out)
        self.assertIn("<ul><li>one</li><li>two</li></ul>", out)
        self.assertIn("<ol><li>first</li><li>second</li></ol>", out)
        self.assertIn("<blockquote><p>quoted</p></blockquote>", out)
        self.assertIn("<hr>", out)
        self.assertIn('<pre><code class="language-python">print(1 &lt; 2)</code></pre>', out)

    def test_table(self):
        source = "| 名称 | 说明 |\n|---|---|\n| **cat** | 猫 |\n| dog | 狗 |"
        out = voter.render_markdown(source)
        self.assertIn("<table>", out)
        self.assertIn("<th>名称</th>", out)
        self.assertIn("<th>说明</th>", out)
        self.assertIn("<td><strong>cat</strong></td>", out)
        self.assertIn("<td>dog</td>", out)
        self.assertNotIn("---", out)
        self.assertNotIn("| 名称", out)

    def test_paragraph_line_breaks(self):
        out = voter.render_markdown("line one\nline two")
        self.assertIn("<p>line one<br>line two</p>", out)


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="voter-test-")
        self.dir = Path(self.tmp.name)
        self.servers: list[tuple[int, str]] = []

    def tearDown(self):
        self.stop_servers()
        self.tmp.cleanup()

    def stop_servers(self):
        for pid, result_path in self.servers:
            state = voter.read_json(Path(result_path))
            if state and state.get("pending") is True:
                try:
                    os.kill(pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        self.servers.clear()

    def start(self, *extra: str, proposition: str = "**命题**", result_name: str = "result.json"):
        result_path = self.dir / result_name
        args = [
            "start",
            "--proposition",
            proposition,
            "--host",
            "127.0.0.1",
            "--result",
            str(result_path),
            "--timeout",
            "20",
            *extra,
        ]
        proc = run_cli(*args)
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        state = json.loads(proc.stdout.strip().splitlines()[-1])
        self.assertTrue(state["url"])
        self.servers.append((state["pid"], str(result_path)))
        return state, result_path

    def test_vote_flow(self):
        image = self.dir / "frame.png"
        image_bytes = b"\x89PNG\r\n\x1a\nfake"
        image.write_bytes(image_bytes)
        state, result_path = self.start(
            "--other",
            "cat",
            "--other",
            "dog",
            "--id",
            "frame-001",
            "--image",
            str(image),
        )
        base = state["url"]

        status, _, body = http_json(base)
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn("<strong>命题</strong>", page)
        self.assertIn("cat", page)
        self.assertIn("请选择…", page)
        self.assertIn("frame-001", page)

        status, data, _ = http_json(base + "health")
        self.assertEqual((status, data["pending"]), (200, True))

        status, data, _ = http_json(base + "result")
        self.assertEqual((status, data["pending"]), (202, True))

        with OPENER.open(base + "image/0", timeout=5) as response:
            self.assertEqual(response.headers["Content-Type"], "image/png")
            self.assertEqual(response.read(), image_bytes)
        self.assertEqual(http_json(base + "image/1")[0], 404)

        self.assertEqual(http_json(base + "vote", "POST", {"choice": "maybe"})[0], 400)
        self.assertEqual(http_json(base + "vote", "POST", {"choice": "other"})[0], 400)
        self.assertEqual(
            http_json(base + "vote", "POST", {"choice": "other", "other": "bird"})[0],
            400,
        )

        status, data, _ = http_json(
            base + "vote", "POST", {"choice": "other", "other": "cat"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(data["result"]["choice"], "cat")
        self.assertEqual(data["result"]["choice_label"], "cat")
        self.assertEqual(data["result"]["choice_type"], "other")
        self.assertEqual(data["result"]["id"], "frame-001")

        self.assertEqual(
            http_json(base + "vote", "POST", {"choice": "yes"})[0], 409
        )

        saved = voter.read_json(result_path)
        self.assertEqual(saved["session"], state["session"])
        self.assertEqual(saved["images"], [str(image.resolve())])
        self.assertFalse(saved["pending"])

        waited = run_cli("wait", "--result", str(result_path), "--timeout", "5")
        self.assertEqual(waited.returncode, 0, msg=waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["choice"], "cat")

    def test_yes_and_no(self):
        for choice in ("yes", "no"):
            with self.subTest(choice=choice):
                state, result_path = self.start(result_name=f"{choice}.json")
                status, data, _ = http_json(
                    state["url"] + "vote", "POST", {"choice": choice}
                )
                self.assertEqual(status, 200)
                self.assertEqual(data["result"]["choice"], choice)
                self.assertEqual(data["result"]["choice_type"], choice)

    def test_server_timeout(self):
        state, result_path = self.start("--timeout", "0.6", result_name="timeout.json")
        deadline = time.monotonic() + 5
        saved = None
        while time.monotonic() < deadline:
            saved = voter.read_json(result_path)
            if saved and saved.get("pending") is False:
                break
            time.sleep(0.05)
        self.assertIsNotNone(saved)
        self.assertFalse(saved["pending"])
        self.assertEqual(saved["error"], "timeout")

        waited = run_cli("wait", "--result", str(result_path), "--timeout", "5")
        self.assertEqual(waited.returncode, 124)
        self.assertEqual(json.loads(waited.stdout)["error"], "timeout")

    def test_wait_without_server_times_out(self):
        missing = self.dir / "missing.json"
        started = time.monotonic()
        proc = run_cli("wait", "--result", str(missing), "--timeout", "0.4")
        elapsed = time.monotonic() - started
        self.assertEqual(proc.returncode, 124)
        self.assertEqual(json.loads(proc.stdout)["error"], "timeout")
        self.assertLess(elapsed, 5)

    def test_proposition_from_stdin(self):
        result_path = self.dir / "stdin.json"
        proc = run_cli(
            "start",
            "--proposition-file",
            "-",
            "--host",
            "127.0.0.1",
            "--result",
            str(result_path),
            "--timeout",
            "20",
            input_text="# 来自 stdin\n\n内容",
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr + proc.stdout)
        state = json.loads(proc.stdout.strip().splitlines()[-1])
        self.servers.append((state["pid"], str(result_path)))
        _, _, body = http_json(state["url"])
        self.assertIn("<h1>来自 stdin</h1>", body.decode())


class CliTests(unittest.TestCase):
    def test_missing_proposition_is_usage_error(self):
        proc = run_cli("start")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("命题", json.loads(proc.stdout)["error"])

    def test_missing_image_is_usage_error(self):
        proc = run_cli("start", "-p", "x", "-i", "/nope/missing.png")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("图片不存在", json.loads(proc.stdout)["error"])

    def test_bad_port_is_usage_error(self):
        proc = run_cli("start", "-p", "x", "--port", "70000")
        self.assertEqual(proc.returncode, 2)


if __name__ == "__main__":
    unittest.main()
