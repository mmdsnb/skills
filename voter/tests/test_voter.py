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


def http_json(
    url: str,
    method: str = "GET",
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
):
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload).encode()
        request_headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=data, headers=request_headers, method=method
    )
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


def http_fetch(url: str):
    with OPENER.open(url, timeout=5) as response:
        return response.status, response.headers, response.read()


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

    def start(
        self,
        *extra: str,
        proposition: str = "**命题**",
        result_name: str = "result.json",
    ):
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
        self.assertTrue(state["ok"])
        self.assertTrue(state["pending"])
        self.assertTrue(state["url"].endswith("/"))
        self.servers.append((state["pid"], str(result_path)))
        return state, result_path

    def start_batch(self, manifest: dict | Path, *extra: str, result_name="batch.json"):
        if isinstance(manifest, dict):
            manifest_path = self.dir / result_name.replace(".json", ".manifest.json")
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
            )
        else:
            manifest_path = manifest
        result_path = self.dir / result_name
        args = [
            "batch",
            "--manifest",
            str(manifest_path),
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
        self.assertTrue(state["ok"])
        self.assertTrue(state["pending"])
        self.servers.append((state["pid"], str(result_path)))
        return state, result_path

    def wait_final(self, result_path: Path, timeout: float = 8):
        deadline = time.monotonic() + timeout
        state = None
        while time.monotonic() < deadline:
            state = voter.read_json(result_path)
            if state and state.get("pending") is False:
                return state
            time.sleep(0.05)
        self.fail(f"结果未在 {timeout}s 内完成: {state}")

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
            f"裁剪框={image}",
            "--yes-label",
            "保留",
            "--no-label",
            "删除",
            "--progress",
            "7/18",
        )
        base = state["url"]
        self.assertEqual(state["mode"], "single")
        self.assertEqual(
            state["images"],
            [{"path": str(image.resolve()), "caption": "裁剪框"}],
        )
        self.assertGreater(state["expires_epoch"], time.time())

        status, headers, body = http_fetch(base)
        self.assertEqual(status, 200)
        page = body.decode()
        self.assertIn("<strong>命题</strong>", page)
        self.assertIn("cat", page)
        self.assertIn("请选择…", page)
        self.assertIn("frame-001", page)
        self.assertIn("裁剪框", page)
        self.assertIn("保留", page)
        self.assertIn("删除", page)
        self.assertIn("7/18", page)
        self.assertIn('src="image/0"', page)
        self.assertIn('href="image/0"', page)
        self.assertIn("跳过", page)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertEqual(headers["X-Frame-Options"], "DENY")

        status, data, _ = http_json(base + "health")
        self.assertEqual((status, data["pending"]), (200, True))
        self.assertEqual(data["mode"], "single")

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

        self.assertEqual(http_json(base + "vote", "POST", {"choice": "yes"})[0], 409)

        saved = voter.read_json(result_path)
        self.assertEqual(saved["session"], state["session"])
        self.assertEqual(
            saved["images"],
            [{"path": str(image.resolve()), "caption": "裁剪框"}],
        )
        self.assertFalse(saved["pending"])

        waited = run_cli(
            "wait",
            "--result",
            str(result_path),
            "--session",
            state["session"],
        )
        self.assertEqual(waited.returncode, 0, msg=waited.stderr)
        compact = json.loads(waited.stdout)
        self.assertEqual(compact["choice"], "cat")
        self.assertNotIn("proposition", compact)

        full = run_cli(
            "wait",
            "--result",
            str(result_path),
            "--session",
            state["session"],
            "--full",
        )
        self.assertEqual(full.returncode, 0, msg=full.stderr)
        self.assertIn("命题", json.loads(full.stdout)["proposition"])

    def test_yes_and_no_labels(self):
        for choice in ("yes", "no"):
            with self.subTest(choice=choice):
                state, _ = self.start(
                    "--yes-label", "通过", "--no-label", "驳回",
                    result_name=f"{choice}.json",
                )
                page = http_fetch(state["url"])[2].decode()
                self.assertIn("通过", page)
                self.assertIn("驳回", page)
                self.assertNotIn("命题成立", page)
                status, data, _ = http_json(
                    state["url"] + "vote", "POST", {"choice": choice}
                )
                self.assertEqual(status, 200)
                self.assertEqual(data["result"]["choice"], choice)
                self.assertEqual(data["result"]["choice_type"], choice)
                self.assertEqual(
                    data["result"]["choice_label"],
                    "通过" if choice == "yes" else "驳回",
                )

    def test_skip(self):
        state, result_path = self.start()
        status, data, _ = http_json(
            state["url"] + "vote", "POST", {"choice": "skip"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["result"]["choice_type"], "skip")
        self.assertEqual(data["result"]["choice_label"], "跳过")
        self.assertEqual(self.wait_final(result_path)["choice"], "skip")

    def test_token_and_origin_checks(self):
        state, _ = self.start()
        root = f"http://127.0.0.1:{state['port']}/"
        self.assertEqual(http_json(root)[0], 404)
        self.assertEqual(
            http_json(f"{root}t/aaaaaaaaaaaaaaaa/")[0], 404
        )
        self.assertEqual(http_json(root + "health")[0], 200)
        self.assertEqual(
            http_json(
                state["url"] + "vote",
                "POST",
                {"choice": "yes"},
                headers={"Origin": "http://evil.example"},
            )[0],
            403,
        )
        self.assertEqual(
            http_json(
                state["url"] + "vote",
                "POST",
                {"choice": "yes"},
                headers={"Sec-Fetch-Site": "cross-site"},
            )[0],
            403,
        )
        status, _, _ = http_json(
            state["url"] + "vote",
            "POST",
            {"choice": "yes"},
            headers={"Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(status, 200)

    def test_result_path_in_use_is_rejected(self):
        _, result_path = self.start()
        proc = run_cli(
            "start",
            "-p",
            "x",
            "--host",
            "127.0.0.1",
            "--result",
            str(result_path),
            "--timeout",
            "20",
        )
        self.assertEqual(proc.returncode, 2, msg=proc.stderr + proc.stdout)
        self.assertIn("占用", json.loads(proc.stdout)["error"])

    def test_wait_session_binding(self):
        state, result_path = self.start()
        http_json(state["url"] + "vote", "POST", {"choice": "no"})
        self.wait_final(result_path)
        waited = run_cli(
            "wait",
            "--result",
            str(result_path),
            "--session",
            "deadbeefdead",
        )
        self.assertEqual(waited.returncode, 1)
        self.assertEqual(json.loads(waited.stdout)["error"], "结果文件属于其他会话")

    def test_server_timeout(self):
        state, result_path = self.start("--timeout", "2", result_name="timeout.json")
        saved = self.wait_final(result_path)
        self.assertEqual(saved["error"], "timeout")
        self.assertFalse(saved["ok"])

        waited = run_cli(
            "wait",
            "--result",
            str(result_path),
            "--session",
            state["session"],
        )
        self.assertEqual(waited.returncode, 124)
        self.assertEqual(json.loads(waited.stdout)["error"], "timeout")

    def test_wait_default_follows_server_expiry(self):
        _, result_path = self.start("--timeout", "2", result_name="wait-native.json")
        started = time.monotonic()
        waited = run_cli("wait", "--result", str(result_path))
        elapsed = time.monotonic() - started
        self.assertEqual(waited.returncode, 124)
        self.assertLess(elapsed, 5)
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
        page = http_fetch(state["url"])[2].decode()
        self.assertIn("<h1>来自 stdin</h1>", page)

    def test_batch_flow(self):
        (self.dir / "a.png").write_bytes(b"\x89PNG\r\n\x1a\nA")
        (self.dir / "b.png").write_bytes(b"\x89PNG\r\n\x1a\nB")
        manifest = {
            "title": "YOLO 复核",
            "labels": {"yes": "保留", "no": "删除"},
            "other_options": ["cat", "dog"],
            "items": [
                {
                    "id": "778",
                    "proposition": "第一项是否 cat？",
                    "images": [
                        {"path": "a.png", "caption": "任务 778 裁剪"}
                    ],
                },
                {
                    "id": "805",
                    "proposition": "第二项是否 cat？",
                    "images": ["b.png"],
                    "other_options": ["bird"],
                },
            ],
        }
        state, result_path = self.start_batch(manifest)
        base = state["url"]
        self.assertEqual(state["total"], 2)
        self.assertEqual(state["answered"], 0)
        self.assertEqual(state["mode"], "batch")

        page = http_fetch(base)[2].decode()
        self.assertIn("第 1 / 2 题", page)
        self.assertIn("任务 778 裁剪", page)
        self.assertIn('src="image/0/0"', page)
        self.assertIn('href="image/0/0"', page)
        self.assertIn("保留", page)
        self.assertIn("cat", page)
        self.assertNotIn("bird", page)

        status, data, _ = http_json(
            base + "vote", "POST", {"index": 0, "choice": "yes"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["next"], 1)
        self.assertFalse(data["all_answered"])
        self.assertEqual(data["answer"]["choice_label"], "保留")

        page = http_fetch(base + "?item=1")[2].decode()
        self.assertIn("第 2 / 2 题", page)
        self.assertIn("bird", page)
        self.assertIn('src="image/1/0"', page)

        status, data, _ = http_json(
            base + "vote",
            "POST",
            {"index": 1, "choice": "other", "other": "bird"},
        )
        self.assertEqual(status, 200)
        self.assertIsNone(data["next"])
        self.assertTrue(data["all_answered"])
        self.assertEqual(data["summary"]["counts"]["yes"], 1)
        self.assertEqual(data["summary"]["counts"]["other"], 1)

        summary = http_fetch(base)[2].decode()
        self.assertIn("提交结果", summary)
        self.assertIn("已完成 2 / 2", summary)
        self.assertIn("保留", summary)
        self.assertIn("bird", summary)
        self.assertIn('href="?item=0"', summary)

        status, final, _ = http_json(base + "submit", "POST", {})
        self.assertEqual(status, 200)
        self.assertTrue(final["ok"])
        self.assertEqual(final["summary"]["answered"], 2)
        self.assertEqual(
            final["summary"]["other_counts"], {"bird": 1}
        )

        waited = run_cli(
            "wait",
            "--result",
            str(result_path),
            "--session",
            state["session"],
        )
        self.assertEqual(waited.returncode, 0, msg=waited.stderr)
        compact = json.loads(waited.stdout)
        self.assertEqual(compact["summary"]["total"], 2)
        self.assertEqual(compact["answers"][0]["choice"], "yes")
        self.assertEqual(compact["answers"][1]["choice"], "bird")

    def test_batch_skip_and_revision(self):
        manifest = {
            "title": "复核",
            "items": [
                {"id": "a", "proposition": "A？"},
                {"id": "b", "proposition": "B？"},
            ],
        }
        state, _ = self.start_batch(manifest)
        base = state["url"]
        status, data, _ = http_json(
            base + "vote", "POST", {"index": 0, "choice": "skip"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["next"], 1)
        status, data, _ = http_json(
            base + "vote", "POST", {"index": 1, "choice": "no"}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(data["next"])

        page = http_fetch(base + "?item=0")[2].decode()
        self.assertIn("当前：跳过", page)
        self.assertIn("查看总览", page)

        status, data, _ = http_json(
            base + "vote", "POST", {"index": 0, "choice": "yes"}
        )
        self.assertEqual(status, 200)
        self.assertIsNone(data["next"])
        self.assertEqual(data["summary"]["counts"]["skip"], 0)
        self.assertEqual(data["summary"]["counts"]["yes"], 1)

        status, final, _ = http_json(base + "submit", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(final["answers"][0]["choice"], "yes")

    def test_batch_submit_early_and_bad_index(self):
        manifest = {
            "items": [
                {"id": "a", "proposition": "A？"},
                {"id": "b", "proposition": "B？"},
            ]
        }
        state, _ = self.start_batch(manifest)
        base = state["url"]
        self.assertEqual(http_json(base + "submit", "POST", {})[0], 409)
        self.assertEqual(
            http_json(base + "vote", "POST", {"index": 5, "choice": "yes"})[0], 400
        )
        self.assertEqual(
            http_json(base + "vote", "POST", {"choice": "yes"})[0], 400
        )
        self.assertEqual(
            http_json(
                base + "vote", "POST", {"index": 0, "choice": "other", "other": "x"}
            )[0],
            400,
        )

    def test_batch_timeout_keeps_partial_answers(self):
        manifest = {
            "items": [
                {"id": "a", "proposition": "A？"},
                {"id": "b", "proposition": "B？"},
            ]
        }
        state, result_path = self.start_batch(
            manifest, "--timeout", "2", result_name="batch-timeout.json"
        )
        http_json(state["url"] + "vote", "POST", {"index": 0, "choice": "yes"})
        saved = self.wait_final(result_path)
        self.assertEqual(saved["error"], "timeout")
        self.assertTrue(saved["partial"])
        self.assertEqual(saved["summary"]["answered"], 1)
        self.assertIsNotNone(saved["answers"][0])
        self.assertIsNone(saved["answers"][1])

        waited = run_cli(
            "wait",
            "--result",
            str(result_path),
            "--session",
            state["session"],
        )
        self.assertEqual(waited.returncode, 124)
        compact = json.loads(waited.stdout)
        self.assertEqual(compact["summary"]["answered"], 1)
        self.assertTrue(compact["partial"])

    def test_batch_manifest_errors(self):
        cases = {
            "empty": {"items": []},
            "no-proposition": {"items": [{"id": "1"}]},
            "duplicate-id": {
                "items": [
                    {"id": "1", "proposition": "A"},
                    {"id": "1", "proposition": "B"},
                ]
            },
            "missing-image": {
                "items": [
                    {"id": "1", "proposition": "A", "images": ["nope.png"]}
                ]
            },
        }
        for name, manifest in cases.items():
            with self.subTest(name=name):
                path = self.dir / f"{name}.json"
                path.write_text(
                    json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
                )
                proc = run_cli(
                    "batch",
                    "--manifest",
                    str(path),
                    "--result",
                    str(self.dir / f"{name}-result.json"),
                )
                self.assertEqual(proc.returncode, 2, msg=proc.stderr + proc.stdout)
                self.assertTrue(json.loads(proc.stdout)["error"])


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

    def test_bad_token_is_usage_error(self):
        proc = run_cli("start", "-p", "x", "--token", "bad token")
        self.assertEqual(proc.returncode, 2)
        self.assertIn("--token", json.loads(proc.stdout)["error"])


if __name__ == "__main__":
    unittest.main()
