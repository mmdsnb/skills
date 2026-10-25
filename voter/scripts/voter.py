#!/usr/bin/env python3
"""Single-question YES / NO / other vote over HTTP, with a Markdown proposition."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

SCRIPT_PATH = Path(__file__).resolve()
START_TIMEOUT = 10.0
SHUTDOWN_DELAY = 0.8
DEFAULT_RESULT = "voter-result.json"
POLL_INTERVAL = 0.2

CSP = (
    "default-src 'none'; img-src 'self' https: http: data:; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'"
)

FENCE_RE = re.compile(r"^\s*```\s*([\w+-]*)\s*$")
HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
UL_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
OL_RE = re.compile(r"^\s*(\d+)[.)]\s+(.*)$")
HR_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
QUOTE_RE = re.compile(r"^\s*>\s?(.*)$")
TABLE_SEP_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)

CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^\s)]+)\)")
LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
EM_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
PLACEHOLDER_RE = re.compile(r"\x00(\d+)\x00")


class UsageError(Exception):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_json(path: Path, data: dict) -> None:
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def starts_block(line: str) -> bool:
    return bool(
        FENCE_RE.match(line)
        or HEADING_RE.match(line)
        or HR_RE.match(line)
        or QUOTE_RE.match(line)
        or UL_RE.match(line)
        or OL_RE.match(line)
    )


def is_table_start(lines: list[str], index: int) -> bool:
    return (
        index + 1 < len(lines)
        and "|" in lines[index]
        and TABLE_SEP_RE.match(lines[index + 1]) is not None
    )


def split_table_row(line: str) -> list[str]:
    text = line.strip()
    if text.startswith("|"):
        text = text[1:]
    if text.endswith("|"):
        text = text[:-1]
    return [cell.strip() for cell in text.split("|")]


def render_inline(text: str) -> str:
    text = text.replace("\x00", "")
    store: list[str] = []

    def stash(fragment: str) -> str:
        store.append(fragment)
        return f"\x00{len(store) - 1}\x00"

    def fill(value: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: store[int(m.group(1))], value)

    out = html.escape(text, quote=True)
    out = CODE_SPAN_RE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), out)
    out = IMAGE_RE.sub(
        lambda m: stash(
            f'<img src="{m.group(2)}" alt="{fill(m.group(1))}" loading="lazy">'
        ),
        out,
    )
    out = LINK_RE.sub(
        lambda m: stash(
            f'<a href="{m.group(2)}" target="_blank" '
            f'rel="noopener noreferrer">{fill(m.group(1))}</a>'
        ),
        out,
    )
    out = BOLD_RE.sub(r"<strong>\1</strong>", out)
    out = EM_RE.sub(r"<em>\1</em>", out)
    out = STRIKE_RE.sub(r"<del>\1</del>", out)
    return fill(out)


def render_markdown(source: str) -> str:
    lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    i = 0
    total = len(lines)

    while i < total:
        line = lines[i]
        if not line.strip():
            i += 1
            continue

        fence = FENCE_RE.match(line)
        if fence:
            lang = fence.group(1)
            i += 1
            code: list[str] = []
            while i < total and not FENCE_RE.match(lines[i]):
                code.append(lines[i])
                i += 1
            if i < total:
                i += 1
            cls = f' class="language-{html.escape(lang, quote=True)}"' if lang else ""
            body = html.escape("\n".join(code))
            out.append(f"<pre><code{cls}>{body}</code></pre>")
            continue

        heading = HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            out.append(f"<h{level}>{render_inline(heading.group(2).strip())}</h{level}>")
            i += 1
            continue

        if HR_RE.match(line):
            out.append("<hr>")
            i += 1
            continue

        if QUOTE_RE.match(line):
            quoted: list[str] = []
            while i < total:
                qmatch = QUOTE_RE.match(lines[i])
                if not qmatch:
                    break
                quoted.append(qmatch.group(1))
                i += 1
            out.append(f"<blockquote>{render_markdown(chr(10).join(quoted))}</blockquote>")
            continue

        if UL_RE.match(line):
            items: list[str] = []
            while i < total:
                umatch = UL_RE.match(lines[i])
                if umatch:
                    items.append(umatch.group(1))
                    i += 1
                elif (
                    lines[i].strip()
                    and items
                    and not starts_block(lines[i])
                    and not is_table_start(lines, i)
                ):
                    items[-1] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            out.append("<ul>" + "".join(f"<li>{render_inline(x)}</li>" for x in items) + "</ul>")
            continue

        if OL_RE.match(line):
            items = []
            while i < total:
                omatch = OL_RE.match(lines[i])
                if omatch:
                    items.append(omatch.group(2))
                    i += 1
                elif (
                    lines[i].strip()
                    and items
                    and not starts_block(lines[i])
                    and not is_table_start(lines, i)
                ):
                    items[-1] += " " + lines[i].strip()
                    i += 1
                else:
                    break
            out.append("<ol>" + "".join(f"<li>{render_inline(x)}</li>" for x in items) + "</ol>")
            continue

        if is_table_start(lines, i):
            header = split_table_row(lines[i])
            i += 2
            rows: list[list[str]] = []
            while i < total and lines[i].strip() and "|" in lines[i]:
                rows.append(split_table_row(lines[i]))
                i += 1
            head = "".join(f"<th>{render_inline(cell)}</th>" for cell in header)
            body = []
            for row in rows:
                cells = (row + [""] * len(header))[: len(header)]
                body.append(
                    "<tr>" + "".join(f"<td>{render_inline(cell)}</td>" for cell in cells) + "</tr>"
                )
            out.append(
                '<div class="table-wrap"><table><thead><tr>'
                + head
                + "</tr></thead><tbody>"
                + "".join(body)
                + "</tbody></table></div>"
            )
            continue

        paragraph: list[str] = []
        while (
            i < total
            and lines[i].strip()
            and not starts_block(lines[i])
            and not is_table_start(lines, i)
        ):
            paragraph.append(lines[i].strip())
            i += 1
        out.append("<p>" + "<br>".join(render_inline(x) for x in paragraph) + "</p>")

    return "\n".join(out)


PAGE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { color-scheme: light; --bg:#f5f6f8; --card:#fff; --fg:#1c1e21; --muted:#6b7280; --line:#e3e6ea; --accent:#2563eb; --accent-soft:#eff5ff; --ok:#15803d; --err:#b91c1c; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
main { max-width:760px; margin:0 auto; padding:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
.meta { display:flex; align-items:center; gap:8px; flex-wrap:wrap; color:var(--muted); font-size:13px; margin-bottom:14px; }
.badge { background:var(--accent-soft); color:var(--accent); border-radius:999px; padding:2px 10px; font-weight:600; word-break:break-all; }
.proposition { margin:0 0 18px; }
.proposition > :first-child { margin-top:0; }
.proposition > :last-child { margin-bottom:0; }
.proposition h1,.proposition h2,.proposition h3,.proposition h4 { line-height:1.3; margin:1.1em 0 .5em; }
.proposition h1 { font-size:1.5em; } .proposition h2 { font-size:1.3em; } .proposition h3 { font-size:1.15em; }
.proposition p { margin:.6em 0; }
.proposition ul,.proposition ol { margin:.6em 0; padding-left:1.4em; }
.proposition code { background:#f0f2f5; border-radius:4px; padding:.1em .35em; font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:.9em; }
.proposition pre { background:#111827; color:#f9fafb; padding:12px 14px; border-radius:8px; overflow:auto; }
.proposition pre code { background:none; padding:0; color:inherit; }
.proposition blockquote { margin:.8em 0; padding:.2em 0 .2em 14px; border-left:3px solid var(--line); color:var(--muted); }
.proposition table { width:100%; border-collapse:collapse; margin:.8em 0; font-size:.95em; }
.proposition th,.proposition td { border:1px solid var(--line); padding:8px 10px; text-align:left; vertical-align:top; }
.proposition th { background:#f7f8fa; font-weight:600; }
.table-wrap { overflow-x:auto; }
.proposition img { max-width:100%; border-radius:8px; }
.proposition hr { border:0; border-top:1px solid var(--line); margin:1.2em 0; }
figure { margin:0 0 16px; }
figure img { display:block; width:100%; max-height:62vh; object-fit:contain; background:#fafafa; border:1px solid var(--line); border-radius:10px; }
figcaption { color:var(--muted); font-size:12px; margin-top:6px; word-break:break-all; }
.choices { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:10px; margin-top:6px; }
.choice { appearance:none; border:1.5px solid var(--line); background:#fff; border-radius:12px; padding:16px 12px; font:inherit; font-weight:600; color:var(--fg); cursor:pointer; transition:border-color .12s, background .12s, transform .06s; min-height:64px; }
.choice small { display:block; color:var(--muted); font-weight:400; font-size:12px; margin-top:2px; }
.choice:hover { border-color:#c7ccd3; }
.choice:active { transform:scale(.99); }
.choice.active { border-color:var(--accent); background:var(--accent-soft); color:var(--accent); }
.choice:disabled { opacity:.55; cursor:default; }
#other-box { margin-top:12px; }
#other-box label { display:block; color:var(--muted); font-size:13px; margin-bottom:6px; }
select { width:100%; padding:12px; border:1.5px solid var(--line); border-radius:10px; background:#fff; font:inherit; color:var(--fg); }
button[type=submit] { width:100%; margin-top:16px; padding:14px; border:0; border-radius:12px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
button[type=submit]:disabled { background:#c9d3e3; cursor:default; }
#status { min-height:1.5em; margin:10px 0 0; font-size:14px; }
#status.ok { color:var(--ok); } #status.err { color:var(--err); }
.hint { color:var(--muted); font-size:12px; margin:12px 0 0; }
</style>
</head>
<body>
<main>
  <div class="card">
    <div class="meta">__META__</div>
    <section class="proposition">__PROPOSITION__</section>
    __IMAGES__
    <form id="vote" novalidate>
      <div class="choices">
        <button type="button" class="choice" data-value="yes">是<small>命题成立</small></button>
        <button type="button" class="choice" data-value="no">否<small>命题不成立</small></button>
        __OTHER_BUTTON__
      </div>
      __OTHER_BOX__
      <button type="submit" id="submit" disabled>提交</button>
      <p id="status" role="status"></p>
    </form>
    <p class="hint">__HINT__</p>
  </div>
</main>
<script>
(function () {
  var buttons = Array.prototype.slice.call(document.querySelectorAll('.choice'));
  var otherBox = document.getElementById('other-box');
  var otherSelect = document.getElementById('other-select');
  var submit = document.getElementById('submit');
  var status = document.getElementById('status');
  var form = document.getElementById('vote');
  var current = null;

  function choose(value) {
    current = value;
    buttons.forEach(function (b) { b.classList.toggle('active', b.dataset.value === value); });
    if (otherBox) { otherBox.hidden = value !== 'other'; }
    submit.disabled = false;
    status.textContent = '';
    status.className = '';
  }

  buttons.forEach(function (b) {
    b.addEventListener('click', function () { choose(b.dataset.value); });
  });
  if (otherSelect) {
    otherSelect.addEventListener('change', function () { choose('other'); });
  }

  document.addEventListener('keydown', function (e) {
    var tag = e.target && e.target.tagName;
    if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') { return; }
    var key = (e.key || '').toLowerCase();
    if (key === 'y' || key === '1') { choose('yes'); }
    else if (key === 'n' || key === '2') { choose('no'); }
    else if ((key === 'o' || key === '3') && otherBox) { choose('other'); }
    else if (key === 'enter' && !submit.disabled) { e.preventDefault(); form.requestSubmit(); }
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (!current) { return; }
    var payload = { choice: current };
    if (current === 'other') {
      if (!otherSelect) { return; }
      if (!otherSelect.value) {
        status.textContent = '请选择具体选项';
        status.className = 'err';
        return;
      }
      payload.other = otherSelect.value;
    }
    submit.disabled = true;
    buttons.forEach(function (b) { b.disabled = true; });
    if (otherSelect) { otherSelect.disabled = true; }
    fetch('/vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(function (data) { return { res: res, data: data }; });
    }).then(function (x) {
      if (!x.res.ok || !x.data.ok) { throw new Error(x.data.error || ('HTTP ' + x.res.status)); }
      var label = x.data.result && x.data.result.choice_label ? x.data.result.choice_label : current;
      status.textContent = '已记录：' + label + '。可以关闭页面了。';
      status.className = 'ok';
      buttons.forEach(function (b) { b.classList.remove('active'); });
    }).catch(function (err) {
      status.textContent = '提交失败：' + err.message;
      status.className = 'err';
      submit.disabled = false;
      buttons.forEach(function (b) { b.disabled = false; });
      if (otherSelect) { otherSelect.disabled = false; }
    });
  });
})();
</script>
</body>
</html>
"""


def build_page(state: dict) -> str:
    other_button = ""
    other_box = ""
    hint = "提交后服务自动关闭。快捷键：Y 是 · N 否"
    if state["other_options"]:
        other_button = (
            '<button type="button" class="choice" data-value="other">'
            "其他<small>从其他选项中选</small></button>"
        )
        options = '<option value="" selected disabled>请选择…</option>' + "".join(
            f'<option value="{html.escape(o, quote=True)}">{html.escape(o)}</option>'
            for o in state["other_options"]
        )
        other_box = (
            '<div id="other-box" hidden>'
            '<label for="other-select">实际选项</label>'
            f'<select id="other-select">{options}</select>'
            "</div>"
        )
        hint += " · O 其他"

    figures = []
    for index, path in enumerate(state["images"]):
        name = Path(path).name
        figures.append(
            f'<figure><img src="/image/{index}" alt="{html.escape(name, quote=True)}">'
            f"<figcaption>{html.escape(name)}</figcaption></figure>"
        )

    meta = []
    if state.get("id"):
        meta.append(f'<span class="badge">{html.escape(state["id"])}</span>')
    meta.append("<span>单选一次</span>")

    title = f"投票 · {state['id']}" if state.get("id") else "投票"
    page = PAGE
    page = page.replace("__TITLE__", html.escape(title))
    page = page.replace("__META__", "".join(meta))
    page = page.replace("__PROPOSITION__", state["proposition_html"])
    page = page.replace("__IMAGES__", "".join(figures))
    page = page.replace("__OTHER_BUTTON__", other_button)
    page = page.replace("__OTHER_BOX__", other_box)
    page = page.replace("__HINT__", hint)
    return page


class VoterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: dict):
        self.state = state
        super().__init__(address, VoteHandler)


class VoteHandler(BaseHTTPRequestHandler):
    server_version = "voter/1.0"
    sys_version = ""

    @property
    def state(self) -> dict:
        return self.server.state  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args) -> None:
        pass

    def send_body(
        self,
        status: int,
        body: bytes,
        content_type: str = "application/json; charset=utf-8",
        extra: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json_body(self, status: int, data: dict, extra: dict[str, str] | None = None) -> None:
        self.send_body(status, json.dumps(data, ensure_ascii=False).encode("utf-8"), extra=extra)

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        state = self.state

        if path == "/":
            page = build_page(state).encode("utf-8")
            self.send_body(
                200, page, "text/html; charset=utf-8", {"Content-Security-Policy": CSP}
            )
        elif path == "/health":
            self.json_body(200, {"ok": True, "pending": not state["finished"]})
        elif path == "/result":
            data = read_json(state["result_path"])
            if (
                data
                and data.get("session") == state["session"]
                and data.get("pending") is False
            ):
                self.json_body(200, data)
            else:
                self.json_body(
                    202,
                    {"ok": False, "pending": True, "session": state["session"]},
                )
        elif path.startswith("/image/"):
            self.serve_image(path[len("/image/") :])
        else:
            self.json_body(404, {"ok": False, "error": "not found"})

    def serve_image(self, raw_index: str) -> None:
        try:
            index = int(raw_index)
        except ValueError:
            self.json_body(404, {"ok": False, "error": "not found"})
            return
        images = self.state["images"]
        if index < 0 or index >= len(images):
            self.json_body(404, {"ok": False, "error": "not found"})
            return
        path = Path(images[index])
        try:
            data = path.read_bytes()
        except OSError:
            self.json_body(404, {"ok": False, "error": "image not readable"})
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_body(200, data, content_type)

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/vote":
            self.json_body(404, {"ok": False, "error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self.json_body(400, {"ok": False, "error": "请求体无效"})
            return
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self.json_body(400, {"ok": False, "error": "JSON 无效"})
            return
        if not isinstance(payload, dict):
            self.json_body(400, {"ok": False, "error": "JSON 无效"})
            return

        choice_type = payload.get("choice")
        if choice_type not in ("yes", "no", "other"):
            self.json_body(400, {"ok": False, "error": "choice 必须是 yes、no 或 other"})
            return

        state = self.state
        choice = choice_type
        label = {"yes": "是", "no": "否"}.get(choice_type, "")
        if choice_type == "other":
            selected = payload.get("other")
            if not isinstance(selected, str) or selected not in state["other_options"]:
                self.json_body(400, {"ok": False, "error": "other 不在候选项中"})
                return
            choice = selected
            label = selected

        with state["lock"]:
            if state["finished"]:
                self.json_body(409, {"ok": False, "error": "投票已结束"})
                return
            result = {
                "ok": True,
                "pending": False,
                "session": state["session"],
                "id": state["id"],
                "choice": choice,
                "choice_type": choice_type,
                "choice_label": label,
                "proposition": state["proposition"],
                "other_options": state["other_options"],
                "images": state["images"],
                "url": state["url"],
                "voted_at": now_iso(),
            }
            write_json(state["result_path"], result)
            state["finished"] = True
            timer = state.get("timeout_timer")
            if timer is not None:
                timer.cancel()

        self.json_body(200, {"ok": True, "result": result})
        threading.Timer(SHUTDOWN_DELAY, self.server.shutdown).start()


def make_urls(host: str, port: int) -> tuple[str, str]:
    local_url = f"http://127.0.0.1:{port}/"
    if host in ("0.0.0.0", ""):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                lan = sock.getsockname()[0]
        except OSError:
            lan = "127.0.0.1"
        return f"http://{lan}:{port}/", local_url
    return f"http://{host}:{port}/", local_url


def schedule_timeout(server: VoterServer, timeout: float) -> threading.Timer:
    state = server.state

    def expire() -> None:
        with state["lock"]:
            if state["finished"]:
                return
            state["finished"] = True
            write_json(
                state["result_path"],
                {
                    "ok": False,
                    "pending": False,
                    "error": "timeout",
                    "session": state["session"],
                    "id": state["id"],
                    "proposition": state["proposition"],
                    "timed_out_at": now_iso(),
                },
            )
        server.shutdown()

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    return timer


def public_state(state: dict) -> dict:
    keys = (
        "ok",
        "pending",
        "session",
        "id",
        "url",
        "local_url",
        "host",
        "port",
        "result",
        "other_options",
        "images",
        "started_at",
        "pid",
    )
    return {key: state[key] for key in keys if key in state}


def cmd_serve(args: argparse.Namespace) -> int:
    result_path = Path(args.result).expanduser().resolve()
    state = {
        "session": args.session,
        "id": args.id,
        "proposition": args.proposition,
        "proposition_html": render_markdown(args.proposition),
        "other_options": args.other,
        "images": [str(Path(p).expanduser().resolve()) for p in args.image],
        "result_path": result_path,
        "lock": threading.Lock(),
        "finished": False,
        "started_at": now_iso(),
        "pid": os.getpid(),
    }

    try:
        server = VoterServer((args.host, args.port), state)
    except OSError as exc:
        write_json(
            result_path,
            {
                "ok": False,
                "pending": False,
                "session": args.session,
                "error": f"无法监听 {args.host}:{args.port}: {exc}",
            },
        )
        return 1

    port = server.server_address[1]
    url, local_url = make_urls(args.host, port)
    state.update(
        {
            "ok": False,
            "pending": True,
            "url": url,
            "local_url": local_url,
            "host": args.host,
            "port": port,
            "result": str(result_path),
        }
    )
    write_json(result_path, public_state(state))
    print(json.dumps(public_state(state), ensure_ascii=False), flush=True)

    timer = None
    if args.timeout > 0:
        timer = schedule_timeout(server, args.timeout)
        state["timeout_timer"] = timer

    try:
        server.serve_forever(poll_interval=POLL_INTERVAL)
    except KeyboardInterrupt:
        pass
    finally:
        if timer is not None:
            timer.cancel()
        server.server_close()
    return 0


def load_proposition(text: str | None, file: str | None) -> str:
    if text is not None and file is not None:
        raise UsageError("--proposition 与 --proposition-file 只能给一个")
    if file is not None:
        if file == "-":
            content = sys.stdin.read()
        else:
            try:
                content = Path(file).expanduser().read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise UsageError(f"无法读取命题文件 {file}: {exc}") from exc
    elif text is not None:
        content = text
    else:
        raise UsageError("缺少命题：用 --proposition 或 --proposition-file（可用 - 从 stdin 读取）")
    if not content.strip():
        raise UsageError("命题为空")
    return content


def normalize_options(values: list[str]) -> list[str]:
    result: list[str] = []
    for raw in values:
        value = raw.strip()
        if value and value not in result:
            result.append(value)
    return result


def cmd_start(args: argparse.Namespace) -> int:
    proposition = load_proposition(args.proposition, args.proposition_file)
    others = normalize_options(args.other)
    images: list[str] = []
    for raw in args.image:
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise UsageError(f"图片不存在: {path}")
        images.append(str(path))
    if args.timeout <= 0:
        raise UsageError("--timeout 必须大于 0")
    if args.port < 0 or args.port > 65535:
        raise UsageError("--port 必须在 0–65535 之间")

    result_path = Path(args.result).expanduser().resolve()
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"无法创建结果目录 {result_path.parent}: {exc}") from exc
    log_path = result_path.with_name(result_path.name + ".log")
    session = uuid.uuid4().hex[:12]

    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "_serve",
        "--session",
        session,
        "--proposition",
        proposition,
        "--result",
        str(result_path),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--timeout",
        str(args.timeout),
    ]
    if args.id:
        cmd += ["--id", args.id]
    for option in others:
        cmd += ["--other", option]
    for image in images:
        cmd += ["--image", image]

    try:
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
    except OSError as exc:
        raise UsageError(f"无法启动服务: {exc}") from exc

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = read_json(result_path)
        if state and state.get("session") == session:
            if state.get("url"):
                print(json.dumps(public_state(state), ensure_ascii=False))
                return 0
            if state.get("pending") is False:
                print(json.dumps(state, ensure_ascii=False))
                print(f"voter: {state.get('error', '服务启动失败')}", file=sys.stderr)
                return 1
        if proc.poll() is not None:
            state = read_json(result_path)
            error = None
            if state and state.get("session") == session:
                error = state.get("error")
            payload = {
                "ok": False,
                "error": error or f"服务启动失败，查看 {log_path}",
                "result": str(result_path),
            }
            print(json.dumps(payload, ensure_ascii=False))
            print(f"voter: {payload['error']}", file=sys.stderr)
            return 1
        time.sleep(0.05)

    proc.terminate()
    payload = {
        "ok": False,
        "error": f"服务启动超时，查看 {log_path}",
        "result": str(result_path),
    }
    print(json.dumps(payload, ensure_ascii=False))
    print(f"voter: {payload['error']}", file=sys.stderr)
    return 1


def cmd_wait(args: argparse.Namespace) -> int:
    if args.timeout <= 0:
        raise UsageError("--timeout 必须大于 0")
    result_path = Path(args.result).expanduser().resolve()
    deadline = time.monotonic() + args.timeout

    while True:
        state = read_json(result_path)
        if state and state.get("pending") is False:
            print(json.dumps(state, ensure_ascii=False))
            if state.get("ok") is True:
                return 0
            return 124 if state.get("error") == "timeout" else 1

        if state and state.get("pending") is True:
            pid = state.get("pid")
            if isinstance(pid, int) and not pid_alive(pid):
                payload = {
                    "ok": False,
                    "pending": False,
                    "error": "服务已退出但未写入结果",
                    "session": state.get("session"),
                    "result": str(result_path),
                }
                print(json.dumps(payload, ensure_ascii=False))
                return 1

        if time.monotonic() >= deadline:
            payload = {
                "ok": False,
                "pending": False,
                "error": "timeout",
                "result": str(result_path),
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 124

        time.sleep(0.2)


def add_common_args(parser: argparse.ArgumentParser, with_proposition: bool = True) -> None:
    if with_proposition:
        parser.add_argument("--proposition", "-p", help="命题文本（Markdown）")
        parser.add_argument(
            "--proposition-file",
            "-f",
            metavar="PATH",
            help="从文件读取命题；传 - 从 stdin 读取",
        )
    parser.add_argument("--image", "-i", action="append", default=[], metavar="PATH", help="附加图片，可重复")
    parser.add_argument("--other", "-o", action="append", default=[], metavar="TEXT", help="除是/否外的其他选项，可重复")
    parser.add_argument("--id", help="本次投票的标识，写入结果 JSON")
    parser.add_argument("--result", default=DEFAULT_RESULT, help=f"会话/结果文件（默认 {DEFAULT_RESULT}）")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voter",
        description="Single-question YES / NO / other vote over HTTP.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start", help="启动投票服务（后台运行，输出 URL）", allow_abbrev=False)
    add_common_args(start)
    start.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    start.add_argument("--port", type=int, default=0, help="监听端口（默认 0，自动分配）")
    start.add_argument("--timeout", type=float, default=900, help="服务存活秒数（默认 900）")

    wait = sub.add_parser("wait", help="等待并输出投票结果", allow_abbrev=False)
    wait.add_argument("--result", default=DEFAULT_RESULT, help=f"会话/结果文件（默认 {DEFAULT_RESULT}）")
    wait.add_argument("--timeout", type=float, default=900, help="等待秒数（默认 900）")

    serve = sub.add_parser("_serve", help=argparse.SUPPRESS, allow_abbrev=False)
    serve.add_argument("--session", required=True)
    serve.add_argument("--proposition", required=True)
    add_common_args(serve, with_proposition=False)
    serve.add_argument("--host", required=True)
    serve.add_argument("--port", type=int, required=True)
    serve.add_argument("--timeout", type=float, default=900)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "start":
            return cmd_start(args)
        if args.command == "wait":
            return cmd_wait(args)
        return cmd_serve(args)
    except UsageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        print(f"voter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
