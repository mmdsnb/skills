#!/usr/bin/env python3
"""Single-question and batch review votes over HTTP, with Markdown propositions."""

from __future__ import annotations

import argparse
import html
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCRIPT_PATH = Path(__file__).resolve()
START_TIMEOUT = 10.0
SHUTDOWN_DELAY = 0.8
DEFAULT_RESULT = "voter-result.json"
POLL_INTERVAL = 0.2
DEFAULT_YES = "是"
DEFAULT_NO = "否"
DEFAULT_WAIT = 900.0
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

CSP = (
    "default-src 'none'; img-src 'self' https: http: data:; "
    "style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; "
    "frame-ancestors 'none'"
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


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")


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


def drop_none(data: dict) -> dict:
    return {key: value for key, value in data.items() if value is not None}


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


CSS = """
:root { color-scheme: light; --bg:#f5f6f8; --card:#fff; --fg:#1c1e21; --muted:#6b7280; --line:#e3e6ea; --accent:#2563eb; --accent-soft:#eff5ff; --ok:#15803d; --err:#b91c1c; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.6 -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif; }
main { max-width:760px; margin:0 auto; padding:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:14px; padding:20px; box-shadow:0 1px 2px rgba(0,0,0,.04); }
.meta { display:flex; align-items:center; gap:8px; flex-wrap:wrap; color:var(--muted); font-size:13px; margin-bottom:12px; }
.badge { background:var(--accent-soft); color:var(--accent); border-radius:999px; padding:2px 10px; font-weight:600; word-break:break-all; }
.badge.progress { background:#fff7ed; color:#c2410c; }
.badge.current { background:#ecfdf5; color:#047857; }
.bar { height:4px; background:var(--line); border-radius:999px; margin:0 0 16px; overflow:hidden; }
.bar > span { display:block; height:100%; background:var(--accent); transition:width .2s; }
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
figure a { display:block; }
figure img { display:block; width:100%; max-height:62vh; object-fit:contain; background:#fafafa; border:1px solid var(--line); border-radius:10px; cursor:zoom-in; }
figcaption { color:var(--muted); font-size:12px; margin-top:6px; word-break:break-all; }
.choices { display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; margin-top:6px; }
.choice { appearance:none; border:1.5px solid var(--line); background:#fff; border-radius:12px; padding:16px 12px; font:inherit; font-weight:600; color:var(--fg); cursor:pointer; transition:border-color .12s, background .12s, transform .06s; min-height:64px; }
.choice small { display:block; color:var(--muted); font-weight:400; font-size:12px; margin-top:2px; }
.choice:hover { border-color:#c7ccd3; }
.choice:active { transform:scale(.99); }
.choice.active { border-color:var(--accent); background:var(--accent-soft); color:var(--accent); }
.choice.skip { border-style:dashed; color:var(--muted); }
.choice:disabled { opacity:.55; cursor:default; }
#other-box { margin-top:12px; }
#other-box label { display:block; color:var(--muted); font-size:13px; margin-bottom:6px; }
select { width:100%; padding:12px; border:1.5px solid var(--line); border-radius:10px; background:#fff; font:inherit; color:var(--fg); }
.primary { width:100%; margin-top:16px; padding:14px; border:0; border-radius:12px; background:var(--accent); color:#fff; font:inherit; font-weight:600; cursor:pointer; }
.primary:disabled { background:#c9d3e3; cursor:default; }
.nav-row { display:flex; justify-content:space-between; align-items:center; gap:12px; margin:0 0 12px; font-size:13px; }
.nav-row a { color:var(--accent); text-decoration:none; }
.muted { color:var(--muted); }
.ledger { width:100%; border-collapse:collapse; font-size:14px; }
.ledger th,.ledger td { border-bottom:1px solid var(--line); padding:9px 8px; text-align:left; vertical-align:top; }
.ledger th { color:var(--muted); font-weight:600; font-size:12px; }
.ledger a { color:var(--accent); text-decoration:none; }
.summary-lead { margin:0 0 14px; color:var(--muted); }
#status { min-height:1.5em; margin:10px 0 0; font-size:14px; }
#status.ok { color:var(--ok); } #status.err { color:var(--err); }
.hint { color:var(--muted); font-size:12px; margin:12px 0 0; }
[hidden] { display:none !important; }
"""

SHELL = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
__CSS__
</style>
</head>
<body>
<main>
  <div class="card">
__BODY__
  </div>
</main>
<script>
__SCRIPT__
</script>
</body>
</html>
"""

SINGLE_SCRIPT = r"""
(function () {
  var buttons = Array.prototype.slice.call(document.querySelectorAll('.choice'));
  var otherBox = document.getElementById('other-box');
  var otherSelect = document.getElementById('other-select');
  var submit = document.getElementById('submit');
  var status = document.getElementById('status');
  var form = document.getElementById('vote');
  var current = null;
  var busy = false;

  function setStatus(text, cls) {
    status.textContent = text || '';
    status.className = cls || '';
  }

  function refreshSubmit() {
    submit.disabled = busy || !current ||
      (current === 'other' && (!otherSelect || !otherSelect.value));
  }

  function choose(value) {
    current = value;
    buttons.forEach(function (b) {
      b.classList.toggle('active', b.dataset.value === value);
    });
    if (otherBox) { otherBox.hidden = value !== 'other'; }
    setStatus('');
    refreshSubmit();
    if (value === 'other' && otherSelect) { otherSelect.focus(); }
  }

  function send(value) {
    if (busy) { return; }
    var payload = { choice: value };
    if (value === 'other') {
      if (!otherSelect || !otherSelect.value) {
        setStatus('请选择具体选项', 'err');
        return;
      }
      payload.other = otherSelect.value;
    }
    busy = true;
    submit.disabled = true;
    buttons.forEach(function (b) { b.disabled = true; });
    if (otherSelect) { otherSelect.disabled = true; }
    fetch('vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(function (data) { return { res: res, data: data }; });
    }).then(function (x) {
      if (!x.res.ok || !x.data.ok) {
        throw new Error(x.data.error || ('HTTP ' + x.res.status));
      }
      var result = x.data.result || {};
      var label = result.choice_label || value;
      setStatus('已记录：' + label + '。可以关闭页面了。', 'ok');
      buttons.forEach(function (b) { b.classList.remove('active'); });
    }).catch(function (err) {
      busy = false;
      setStatus('提交失败：' + err.message, 'err');
      buttons.forEach(function (b) { b.disabled = false; });
      if (otherSelect) { otherSelect.disabled = false; }
      refreshSubmit();
    });
  }

  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      if (b.dataset.immediate) { send(b.dataset.value); return; }
      choose(b.dataset.value);
    });
  });
  if (otherSelect) {
    otherSelect.addEventListener('change', function () {
      choose('other');
      refreshSubmit();
    });
  }

  document.addEventListener('keydown', function (e) {
    var tag = e.target && e.target.tagName;
    if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') { return; }
    var key = (e.key || '').toLowerCase();
    if (key === 'y' || key === '1') { choose('yes'); }
    else if (key === 'n' || key === '2') { choose('no'); }
    else if ((key === 'o' || key === '3') && otherBox) { choose('other'); }
    else if (key === 's') { e.preventDefault(); send('skip'); }
    else if (key === 'enter') {
      if (!submit.disabled && current) { e.preventDefault(); send(current); }
    }
  });

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (current) { send(current); }
  });
})();
"""

BATCH_ITEM_SCRIPT = r"""
(function () {
  var index = __INDEX__;
  var buttons = Array.prototype.slice.call(document.querySelectorAll('.choice'));
  var otherBox = document.getElementById('other-box');
  var otherSelect = document.getElementById('other-select');
  var submit = document.getElementById('submit');
  var status = document.getElementById('status');
  var form = document.getElementById('vote');
  var busy = false;

  function setStatus(text, cls) {
    status.textContent = text || '';
    status.className = cls || '';
  }

  function setBusy(value) {
    busy = value;
    buttons.forEach(function (b) { b.disabled = value; });
    if (otherSelect) { otherSelect.disabled = value; }
    if (value) { submit.disabled = true; }
  }

  function revealOther() {
    if (!otherBox) { return; }
    otherBox.hidden = false;
    submit.hidden = false;
    buttons.forEach(function (b) {
      b.classList.toggle('active', b.dataset.value === 'other');
    });
    submit.disabled = !otherSelect || !otherSelect.value;
    if (otherSelect) { otherSelect.focus(); }
  }

  function send(value) {
    if (busy) { return; }
    var payload = { index: index, choice: value };
    if (value === 'other') {
      if (!otherSelect || !otherSelect.value) {
        setStatus('请选择具体选项', 'err');
        return;
      }
      payload.other = otherSelect.value;
    }
    setBusy(true);
    setStatus('');
    fetch('vote', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(function (data) { return { res: res, data: data }; });
    }).then(function (x) {
      if (!x.res.ok || !x.data.ok) {
        throw new Error(x.data.error || ('HTTP ' + x.res.status));
      }
      var next = x.data.next;
      window.location.href = (next === null || next === undefined) ? './' : '?item=' + next;
    }).catch(function (err) {
      setBusy(false);
      setStatus('提交失败：' + err.message, 'err');
      if (otherBox && !otherBox.hidden) {
        submit.disabled = !otherSelect || !otherSelect.value;
      }
    });
  }

  buttons.forEach(function (b) {
    b.addEventListener('click', function () {
      var value = b.dataset.value;
      if (value === 'other') { revealOther(); return; }
      send(value);
    });
  });
  if (otherSelect) {
    otherSelect.addEventListener('change', function () {
      if (!otherBox.hidden) {
        submit.disabled = !otherSelect.value;
      }
    });
  }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (otherBox && !otherBox.hidden) { send('other'); }
  });

  document.addEventListener('keydown', function (e) {
    var tag = e.target && e.target.tagName;
    if (tag === 'SELECT' || tag === 'INPUT' || tag === 'TEXTAREA') { return; }
    var key = (e.key || '').toLowerCase();
    if (key === 'y' || key === '1') { send('yes'); }
    else if (key === 'n' || key === '2') { send('no'); }
    else if (key === 's') { e.preventDefault(); send('skip'); }
    else if (key === 'o' || key === '3') { revealOther(); }
    else if (key === 'enter' && otherBox && !otherBox.hidden) {
      e.preventDefault();
      send('other');
    }
  });
})();
"""

SUMMARY_SCRIPT = r"""
(function () {
  var button = document.getElementById('submit-all');
  var status = document.getElementById('status');
  if (!button) { return; }
  button.addEventListener('click', function () {
    button.disabled = true;
    fetch('submit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }).then(function (res) {
      return res.json().then(function (data) { return { res: res, data: data }; });
    }).then(function (x) {
      if (!x.res.ok || !x.data.ok) {
        throw new Error(x.data.error || ('HTTP ' + x.res.status));
      }
      status.textContent = '已提交，可以关闭页面了。';
      status.className = 'ok';
      button.textContent = '已提交';
    }).catch(function (err) {
      status.textContent = '提交失败：' + err.message;
      status.className = 'err';
      button.disabled = false;
    });
  });
})();
"""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def render_shell(title: str, body: str, script: str) -> str:
    page = SHELL.replace("__TITLE__", esc(title))
    page = page.replace("__CSS__", CSS)
    page = page.replace("__BODY__", body)
    page = page.replace("__SCRIPT__", script)
    return page


def badge(text: str, kind: str = "") -> str:
    cls = f"badge {kind}".strip()
    return f'<span class="{cls}">{esc(text)}</span>'


def choice_button(label: str, value: str, hint: str = "", extra: str = "") -> str:
    small = f"<small>{esc(hint)}</small>" if hint else ""
    cls = f"choice {extra}".strip()
    return (
        f'<button type="button" class="{cls}" data-value="{esc(value)}">'
        f"{esc(label)}{small}</button>"
    )


def other_controls(options: list[str]) -> tuple[str, str]:
    if not options:
        return "", ""
    button = choice_button("其他", "other", "从其他选项中选")
    rendered = "".join(
        f'<option value="{esc(option)}">{esc(option)}</option>' for option in options
    )
    box = (
        '<div id="other-box" hidden>'
        '<label for="other-select">实际选项</label>'
        '<select id="other-select">'
        '<option value="" selected disabled>请选择…</option>'
        f"{rendered}</select></div>"
    )
    return button, box


def figures_html(images: list[dict], prefix: str) -> str:
    cards = []
    for index, image in enumerate(images):
        path = str(image.get("path", ""))
        caption = str(image.get("caption") or Path(path).name)
        src = f"{prefix}{index}"
        cards.append(
            "<figure>"
            f'<a href="{src}" target="_blank" rel="noopener noreferrer">'
            f'<img src="{src}" alt="{esc(caption)}" loading="lazy"></a>'
            f"<figcaption>{esc(caption)}</figcaption></figure>"
        )
    return "".join(cards)


def build_single_page(state: dict) -> str:
    labels = state["labels"]
    other_button, other_box = other_controls(state["other_options"])
    buttons = [
        choice_button(labels["yes"], "yes", labels["yes_hint"]),
        choice_button(labels["no"], "no", labels["no_hint"]),
    ]
    if other_button:
        buttons.append(other_button)
    buttons.append(choice_button("跳过", "skip", "本次不判断", "skip"))

    meta = []
    if state.get("id"):
        meta.append(badge(state["id"]))
    if state.get("progress"):
        meta.append(badge(state["progress"], "progress"))
    meta.append("<span>单选一次</span>")

    hint = f"提交后服务自动关闭。快捷键：Y {labels['yes']} · N {labels['no']} · S 跳过"
    if state["other_options"]:
        hint += " · O 其他"

    body = f"""
    <div class="meta">{''.join(meta)}</div>
    <section class="proposition">{state['proposition_html']}</section>
    {figures_html(state['images'], 'image/')}
    <form id="vote" novalidate>
      <div class="choices">{''.join(buttons)}</div>
      {other_box}
      <button type="submit" id="submit" class="primary" disabled>提交</button>
      <p id="status" role="status"></p>
    </form>
    <p class="hint">{esc(hint)}</p>
"""
    title = f"投票 · {state['id']}" if state.get("id") else "投票"
    return render_shell(title, body, SINGLE_SCRIPT)


def build_batch_item_page(state: dict, index: int) -> str:
    items = state["items"]
    total = len(items)
    item = items[index]
    answer = state["answers"][index]
    labels = state["labels"]
    done = sum(1 for value in state["answers"] if value is not None)
    percent = 100 if total == 0 else int(round(done * 100 / total))

    other_button, other_box = other_controls(item["other_options"])
    buttons = [
        choice_button(labels["yes"], "yes", labels["yes_hint"]),
        choice_button(labels["no"], "no", labels["no_hint"]),
    ]
    if other_button:
        buttons.append(other_button)
    buttons.append(choice_button("跳过", "skip", "本次不判断", "skip"))

    meta = [badge(f"第 {index + 1} / {total} 题", "progress")]
    if item.get("id"):
        meta.append(badge(item["id"]))
    meta.append(f"<span>已完成 {done} / {total}</span>")
    if answer:
        meta.append(f'<span class="badge current">当前：{esc(answer["choice_label"])}</span>')

    left = f'<a href="?item={index - 1}">← 上一题</a>' if index > 0 else "<span></span>"
    if done == total:
        right = '<a href="./">查看总览</a>'
    else:
        right = f'<span class="muted">答完自动进入下一题</span>'

    hint = (
        "点击是/否直接记录并进入下一题，跳过也直接记录。"
        "快捷键 Y/N/S"
    )
    if item["other_options"]:
        hint += "；O 其他"
    hint += "；上一题可修改。"

    body = f"""
    <div class="meta">{''.join(meta)}</div>
    <div class="bar" aria-hidden="true"><span style="width:{percent}%"></span></div>
    <section class="proposition">{item['proposition_html']}</section>
    {figures_html(item['images'], f'image/{index}/')}
    <div class="nav-row">{left}{right}</div>
    <form id="vote" novalidate>
      <div class="choices">{''.join(buttons)}</div>
      {other_box}
      <button type="submit" id="submit" class="primary" hidden>确认其他</button>
      <p id="status" role="status"></p>
    </form>
    <p class="hint">{esc(hint)}</p>
"""
    title = f"{state['title']} · 第 {index + 1}/{total} 题"
    script = BATCH_ITEM_SCRIPT.replace("__INDEX__", str(index))
    return render_shell(title, body, script)


def build_batch_summary_page(state: dict) -> str:
    total = len(state["items"])
    done = sum(1 for value in state["answers"] if value is not None)
    rows = []
    for index, item in enumerate(state["items"]):
        answer = state["answers"][index]
        label = answer["choice_label"] if answer else "未答"
        rows.append(
            "<tr>"
            f"<td>{index + 1}</td>"
            f"<td>{esc(item['id'])}</td>"
            f"<td>{esc(label)}</td>"
            f'<td><a href="?item={index}" aria-label="修改 {esc(item["id"])}">修改</a></td>'
            "</tr>"
        )

    meta = [
        badge(f"已完成 {done} / {total}", "progress"),
        badge(state["title"]),
    ]
    body = f"""
    <div class="meta">{''.join(meta)}</div>
    <div class="bar" aria-hidden="true"><span style="width:100%"></span></div>
    <p class="summary-lead">共 {total} 项，全部已记录。核对后提交；点「修改」可回到原题重新作答。</p>
    <div class="table-wrap">
      <table class="ledger">
        <thead><tr><th>#</th><th>ID</th><th>选择</th><th></th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
    </div>
    <button type="button" id="submit-all" class="primary">提交结果</button>
    <p id="status" role="status"></p>
    <p class="hint">提交后服务自动关闭，结果写入 JSON 文件。</p>
"""
    return render_shell(f"{state['title']} · 总览", body, SUMMARY_SCRIPT)


def summarize(answers: list[dict | None]) -> dict:
    counts = {"yes": 0, "no": 0, "other": 0, "skip": 0}
    other_counts: dict[str, int] = {}
    for answer in answers:
        if answer is None:
            continue
        choice_type = answer.get("choice_type")
        if choice_type in counts:
            counts[choice_type] += 1
        if choice_type == "other":
            selected = str(answer.get("choice"))
            other_counts[selected] = other_counts.get(selected, 0) + 1
    return {
        "total": len(answers),
        "answered": sum(1 for answer in answers if answer is not None),
        "counts": counts,
        "other_counts": other_counts,
    }


def first_unanswered(state: dict, after: int | None = None) -> int | None:
    answers = state["answers"]
    size = len(answers)
    if size == 0:
        return None
    if after is None:
        for index, answer in enumerate(answers):
            if answer is None:
                return index
        return None
    for step in range(1, size + 1):
        index = (after + step) % size
        if answers[index] is None:
            return index
    return None


def make_urls(host: str, port: int, token: str) -> tuple[str, str]:
    suffix = f"/t/{token}/"
    local_url = f"http://127.0.0.1:{port}{suffix}"
    if host in ("0.0.0.0", ""):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                sock.connect(("8.8.8.8", 80))
                lan = sock.getsockname()[0]
        except OSError:
            lan = "127.0.0.1"
        return f"http://{lan}:{port}{suffix}", local_url
    return f"http://{host}:{port}{suffix}", local_url


def pending_snapshot(state: dict) -> dict:
    data = {
        "ok": True,
        "pending": True,
        "mode": state["mode"],
        "session": state["session"],
        "url": state.get("url"),
        "local_url": state.get("local_url"),
        "host": state.get("host"),
        "port": state.get("port"),
        "result": str(state["result_path"]),
        "started_at": state.get("started_at"),
        "expires_at": state.get("expires_at"),
        "expires_epoch": state.get("expires_epoch"),
        "pid": state.get("pid"),
    }
    if state["mode"] == "single":
        data.update(
            {
                "id": state.get("id") or None,
                "other_options": state["other_options"],
                "images": state["images"],
                "labels": state["labels"],
                "progress": state.get("progress") or None,
            }
        )
    else:
        data.update(
            {
                "title": state["title"],
                "total": len(state["items"]),
                "answered": sum(1 for answer in state["answers"] if answer is not None),
                "answers": list(state["answers"]),
                "summary": summarize(state["answers"]),
            }
        )
    return drop_none(data)


def single_result(state: dict, choice: str, choice_type: str, label: str) -> dict:
    return drop_none(
        {
            "ok": True,
            "pending": False,
            "mode": "single",
            "session": state["session"],
            "id": state.get("id") or None,
            "choice": choice,
            "choice_type": choice_type,
            "choice_label": label,
            "proposition": state["proposition"],
            "other_options": state["other_options"],
            "images": state["images"],
            "labels": state["labels"],
            "progress": state.get("progress") or None,
            "url": state.get("url"),
            "result": str(state["result_path"]),
            "started_at": state.get("started_at"),
            "voted_at": now_iso(),
        }
    )


def batch_result(state: dict) -> dict:
    return drop_none(
        {
            "ok": True,
            "pending": False,
            "mode": "batch",
            "session": state["session"],
            "title": state["title"],
            "summary": summarize(state["answers"]),
            "answers": list(state["answers"]),
            "url": state.get("url"),
            "result": str(state["result_path"]),
            "started_at": state.get("started_at"),
            "finished_at": now_iso(),
        }
    )


def timeout_snapshot(state: dict) -> dict:
    data = {
        "ok": False,
        "pending": False,
        "mode": state["mode"],
        "error": "timeout",
        "session": state["session"],
        "url": state.get("url"),
        "result": str(state["result_path"]),
        "started_at": state.get("started_at"),
        "timed_out_at": now_iso(),
    }
    if state["mode"] == "single":
        data.update(
            {
                "id": state.get("id") or None,
                "proposition": state["proposition"],
            }
        )
    else:
        data.update(
            {
                "title": state["title"],
                "partial": True,
                "summary": summarize(state["answers"]),
                "answers": list(state["answers"]),
            }
        )
    return drop_none(data)


def resolve_choice(
    payload: dict, labels: dict, options: list[str]
) -> tuple[str, str, str]:
    choice_type = payload.get("choice")
    if choice_type not in ("yes", "no", "other", "skip"):
        raise ValueError("choice 必须是 yes、no、other 或 skip")
    if choice_type == "skip":
        return "skip", "skip", "跳过"
    if choice_type == "other":
        selected = payload.get("other")
        if not isinstance(selected, str) or selected not in options:
            raise ValueError("other 不在候选项中")
        return selected, "other", selected
    return choice_type, choice_type, labels[choice_type]


def schedule_shutdown(server: ThreadingHTTPServer) -> None:
    timer = threading.Timer(SHUTDOWN_DELAY, server.shutdown)
    timer.daemon = True
    timer.start()


class VoterServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], state: dict):
        self.state = state
        super().__init__(address, VoteHandler)


class VoteHandler(BaseHTTPRequestHandler):
    server_version = "voter/2.0"
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
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def json_body(self, status: int, data: dict) -> None:
        self.send_body(
            status, json.dumps(data, ensure_ascii=False).encode("utf-8")
        )

    def redirect(self, location: str) -> None:
        self.send_body(302, b"", "text/plain; charset=utf-8", {"Location": location})

    def same_origin(self) -> bool:
        site = self.headers.get("Sec-Fetch-Site")
        if site and site not in ("same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin:
            if urlsplit(origin).netloc != self.headers.get("Host", ""):
                return False
        return True

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        path = parsed.path
        state = self.state
        if path == "/health":
            self.send_health()
            return

        prefix = f"/t/{state['token']}"
        if path == prefix:
            self.redirect(prefix + "/")
            return
        if not path.startswith(prefix + "/"):
            self.json_body(404, {"ok": False, "error": "not found"})
            return

        sub = path[len(prefix) + 1 :]
        if sub == "":
            self.serve_page(parsed.query)
        elif sub == "health":
            self.send_health()
        elif sub == "result":
            self.send_result()
        elif sub.startswith("image/"):
            self.serve_image(sub[len("image/") :])
        else:
            self.json_body(404, {"ok": False, "error": "not found"})

    def serve_page(self, query: str) -> None:
        state = self.state
        with state["lock"]:
            if state["mode"] == "single":
                page = build_single_page(state)
            else:
                raw = parse_qs(query).get("item", [None])[0]
                index: int | None = None
                if raw is not None:
                    try:
                        index = int(raw)
                    except ValueError:
                        index = -1
                    if index < 0 or index >= len(state["items"]):
                        self.json_body(400, {"ok": False, "error": "item 超出范围"})
                        return
                if index is None:
                    index = first_unanswered(state)
                if index is None:
                    page = build_batch_summary_page(state)
                else:
                    page = build_batch_item_page(state, index)
        self.send_body(
            200,
            page.encode("utf-8"),
            "text/html; charset=utf-8",
            {"Content-Security-Policy": CSP},
        )

    def send_health(self) -> None:
        state = self.state
        data = {
            "ok": True,
            "pending": not state["finished"],
            "mode": state["mode"],
        }
        if state["mode"] == "batch":
            data.update(
                {
                    "total": len(state["items"]),
                    "answered": sum(
                        1 for answer in state["answers"] if answer is not None
                    ),
                }
            )
        self.json_body(200, data)

    def send_result(self) -> None:
        state = self.state
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

    def serve_image(self, raw: str) -> None:
        state = self.state
        parts = raw.strip("/").split("/")
        try:
            if state["mode"] == "single":
                if len(parts) != 1:
                    raise ValueError
                image_index = int(parts[0])
                images = state["images"]
            else:
                if len(parts) != 2:
                    raise ValueError
                item_index = int(parts[0])
                image_index = int(parts[1])
                if item_index < 0 or item_index >= len(state["items"]):
                    raise ValueError
                images = state["items"][item_index]["images"]
            if image_index < 0 or image_index >= len(images):
                raise ValueError
        except ValueError:
            self.json_body(404, {"ok": False, "error": "not found"})
            return

        path = Path(images[image_index]["path"])
        try:
            data = path.read_bytes()
        except OSError:
            self.json_body(404, {"ok": False, "error": "image not readable"})
            return
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        self.send_body(200, data, content_type)

    def do_POST(self) -> None:
        if not self.same_origin():
            self.json_body(403, {"ok": False, "error": "来源校验失败"})
            return
        path = urlsplit(self.path).path
        prefix = f"/t/{self.state['token']}"
        if not path.startswith(prefix + "/"):
            self.json_body(404, {"ok": False, "error": "not found"})
            return
        sub = path[len(prefix) + 1 :]
        if sub == "vote":
            self.handle_vote()
        elif sub == "submit":
            self.handle_submit()
        else:
            self.json_body(404, {"ok": False, "error": "not found"})

    def read_payload(self) -> dict | None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > 65536:
            self.json_body(400, {"ok": False, "error": "请求体无效"})
            return None
        try:
            payload = json.loads(self.rfile.read(length))
        except (ValueError, UnicodeDecodeError):
            self.json_body(400, {"ok": False, "error": "JSON 无效"})
            return None
        if not isinstance(payload, dict):
            self.json_body(400, {"ok": False, "error": "JSON 无效"})
            return None
        return payload

    def handle_vote(self) -> None:
        payload = self.read_payload()
        if payload is None:
            return
        state = self.state
        index: int | None = None
        if state["mode"] == "batch":
            raw_index = payload.get("index")
            if (
                not isinstance(raw_index, int)
                or isinstance(raw_index, bool)
                or raw_index < 0
                or raw_index >= len(state["items"])
            ):
                self.json_body(400, {"ok": False, "error": "index 无效"})
                return
            index = raw_index
            options = state["items"][index]["other_options"]
        else:
            options = state["other_options"]

        try:
            choice, choice_type, label = resolve_choice(
                payload, state["labels"], options
            )
        except ValueError as exc:
            self.json_body(400, {"ok": False, "error": str(exc)})
            return

        if state["mode"] == "single":
            with state["lock"]:
                if state["finished"]:
                    self.json_body(409, {"ok": False, "error": "投票已结束"})
                    return
                result = single_result(state, choice, choice_type, label)
                write_json(state["result_path"], result)
                state["finished"] = True
                timer = state.get("timeout_timer")
                if timer is not None:
                    timer.cancel()
            try:
                self.json_body(200, {"ok": True, "result": result})
            finally:
                schedule_shutdown(self.server)
            return

        assert index is not None
        item = state["items"][index]
        answer = {
            "index": index,
            "id": item["id"],
            "choice": choice,
            "choice_type": choice_type,
            "choice_label": label,
            "voted_at": now_iso(),
        }
        with state["lock"]:
            if state["finished"]:
                self.json_body(409, {"ok": False, "error": "投票已结束"})
                return
            state["answers"][index] = answer
            write_json(state["result_path"], pending_snapshot(state))
            all_answered = all(value is not None for value in state["answers"])
            next_index = None if all_answered else first_unanswered(state, after=index)
            summary = summarize(state["answers"])
        self.json_body(
            200,
            {
                "ok": True,
                "answer": answer,
                "next": next_index,
                "all_answered": all_answered,
                "summary": summary,
            },
        )

    def handle_submit(self) -> None:
        payload = self.read_payload()
        if payload is None:
            return
        state = self.state
        if state["mode"] != "batch":
            self.json_body(404, {"ok": False, "error": "not found"})
            return
        with state["lock"]:
            if state["finished"]:
                self.json_body(409, {"ok": False, "error": "投票已结束"})
                return
            if any(answer is None for answer in state["answers"]):
                self.json_body(409, {"ok": False, "error": "还有未答题目"})
                return
            result = batch_result(state)
            write_json(state["result_path"], result)
            state["finished"] = True
            timer = state.get("timeout_timer")
            if timer is not None:
                timer.cancel()
        try:
            self.json_body(200, result)
        finally:
            schedule_shutdown(self.server)


def schedule_timeout(server: VoterServer, timeout: float) -> threading.Timer:
    state = server.state

    def expire() -> None:
        with state["lock"]:
            if state["finished"]:
                return
            state["finished"] = True
            write_json(state["result_path"], timeout_snapshot(state))
        server.shutdown()

    timer = threading.Timer(timeout, expire)
    timer.daemon = True
    timer.start()
    return timer


def build_state(config: dict, args: argparse.Namespace) -> dict:
    mode = config.get("mode")
    result_path = Path(args.result).expanduser().resolve()
    state = {
        "mode": mode,
        "session": args.session,
        "token": args.token,
        "result_path": result_path,
        "lock": threading.Lock(),
        "finished": False,
        "started_at": now_iso(),
        "pid": os.getpid(),
        "url": None,
        "local_url": None,
        "host": args.host,
        "port": None,
        "expires_at": None,
        "expires_epoch": None,
    }
    labels = config.get("labels") or make_labels()
    if not isinstance(labels, dict):
        raise UsageError("labels 必须是对象")
    if mode == "single":
        proposition = config.get("proposition")
        if not isinstance(proposition, str) or not proposition.strip():
            raise UsageError("单题配置缺少 proposition")
        images = config.get("images") or []
        other_options = config.get("other_options") or []
        if not isinstance(images, list) or not isinstance(other_options, list):
            raise UsageError("单题配置格式无效")
        state.update(
            {
                "id": config.get("id") or "",
                "proposition": proposition,
                "proposition_html": render_markdown(proposition),
                "images": images,
                "other_options": other_options,
                "labels": labels,
                "progress": config.get("progress") or "",
            }
        )
        return state

    if mode != "batch":
        raise UsageError("未知模式")
    raw_items = config.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise UsageError("批量配置缺少 items")
    items = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise UsageError(f"第 {index + 1} 项格式无效")
        proposition = raw.get("proposition")
        if not isinstance(proposition, str) or not proposition.strip():
            raise UsageError(f"第 {index + 1} 项缺少 proposition")
        images = raw.get("images") or []
        other_options = raw.get("other_options") or []
        if not isinstance(images, list) or not isinstance(other_options, list):
            raise UsageError(f"第 {index + 1} 项格式无效")
        items.append(
            {
                "id": str(raw.get("id") or index + 1),
                "proposition": proposition,
                "proposition_html": render_markdown(proposition),
                "images": images,
                "other_options": other_options,
            }
        )
    state.update(
        {
            "title": config.get("title") or "批量复核",
            "labels": labels,
            "items": items,
            "answers": [None] * len(items),
        }
    )
    return state


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        raw = sys.stdin.buffer.read()
        config = json.loads(raw.decode("utf-8"))
        if not isinstance(config, dict):
            raise ValueError("配置必须是对象")
        state = build_state(config, args)
    except (OSError, ValueError, TypeError, UsageError) as exc:
        print(f"voter: 无法读取配置: {exc}", file=sys.stderr)
        return 1

    result_path = state["result_path"]
    try:
        server = VoterServer((args.host, args.port), state)
    except OSError as exc:
        write_json(
            result_path,
            {
                "ok": False,
                "pending": False,
                "mode": state["mode"],
                "session": args.session,
                "error": f"无法监听 {args.host}:{args.port}: {exc}",
            },
        )
        return 1

    port = server.server_address[1]
    url, local_url = make_urls(args.host, port, args.token)
    state.update({"url": url, "local_url": local_url, "port": port})
    if args.timeout > 0:
        expires = time.time() + args.timeout
        state["expires_epoch"] = expires
        state["expires_at"] = iso_from_epoch(expires)

    snapshot = pending_snapshot(state)
    write_json(result_path, snapshot)
    print(json.dumps(snapshot, ensure_ascii=False), flush=True)

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


def normalize_options(values: object) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        raise UsageError("选项必须是字符串数组")
    result: list[str] = []
    for raw in values:
        if not isinstance(raw, str):
            raise UsageError("选项必须是字符串")
        value = raw.strip()
        if value and value not in result:
            result.append(value)
    return result


def make_labels(
    yes: str | None = None,
    no: str | None = None,
    yes_hint: str | None = None,
    no_hint: str | None = None,
) -> dict:
    for field, value in (
        ("yes", yes),
        ("no", no),
        ("yes_hint", yes_hint),
        ("no_hint", no_hint),
    ):
        if value is not None and not isinstance(value, str):
            raise UsageError(f"labels.{field} 必须是字符串")
    yes_label = (yes or "").strip() or DEFAULT_YES
    no_label = (no or "").strip() or DEFAULT_NO

    def hint(value: str | None, label: str, default_label: str, default_hint: str) -> str:
        if value is None:
            return default_hint if label == default_label else ""
        return value.strip()

    return {
        "yes": yes_label,
        "no": no_label,
        "yes_hint": hint(yes_hint, yes_label, DEFAULT_YES, "命题成立"),
        "no_hint": hint(no_hint, no_label, DEFAULT_NO, "命题不成立"),
    }


def parse_image_spec(raw: str) -> dict:
    text = raw
    caption = ""
    if "=" in raw:
        left, right = raw.split("=", 1)
        if left.strip() and right.strip() and not Path(raw).expanduser().is_file():
            caption = left.strip()
            text = right
    path = Path(text).expanduser().resolve()
    if not path.is_file():
        raise UsageError(f"图片不存在: {path}")
    return {"path": str(path), "caption": caption}


def load_single_config(args: argparse.Namespace) -> dict:
    proposition = load_proposition(args.proposition, args.proposition_file)
    images = [parse_image_spec(raw) for raw in args.image]
    progress = args.progress
    if progress is not None:
        progress = progress.strip()
        if not progress:
            raise UsageError("--progress 不能为空")
    return {
        "mode": "single",
        "id": (args.id or "").strip(),
        "proposition": proposition,
        "images": images,
        "other_options": normalize_options(args.other),
        "labels": make_labels(
            args.yes_label, args.no_label, args.yes_hint, args.no_hint
        ),
        "progress": progress or "",
    }


def load_manifest(path: Path) -> dict:
    data = read_json(path)
    if data is None:
        raise UsageError(f"无法读取 manifest: {path}")
    raw_items = data.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        raise UsageError("manifest 需要非空 items 数组")

    raw_labels = data.get("labels") or {}
    if not isinstance(raw_labels, dict):
        raise UsageError("labels 必须是对象")
    labels = make_labels(
        raw_labels.get("yes"),
        raw_labels.get("no"),
        raw_labels.get("yes_hint"),
        raw_labels.get("no_hint"),
    )
    global_other = normalize_options(data.get("other_options"))
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        raise UsageError("title 必须是字符串")

    base_dir = path.parent
    items: list[dict] = []
    seen_ids: set[str] = set()
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            raise UsageError(f"第 {index + 1} 项必须是对象")
        proposition = raw.get("proposition")
        if not isinstance(proposition, str) or not proposition.strip():
            raise UsageError(f"第 {index + 1} 项缺少 proposition")
        item_id = str(raw.get("id") if raw.get("id") is not None else index + 1).strip()
        if not item_id:
            item_id = str(index + 1)
        if item_id in seen_ids:
            raise UsageError(f"item id 重复: {item_id}")
        seen_ids.add(item_id)

        if "other_options" in raw:
            options = normalize_options(raw.get("other_options"))
        else:
            options = list(global_other)

        raw_images = raw.get("images") or []
        if not isinstance(raw_images, list):
            raise UsageError(f"第 {index + 1} 项 images 必须是数组")
        images = []
        for image in raw_images:
            if isinstance(image, str):
                path_text = image
                caption = ""
            elif isinstance(image, dict):
                path_text = image.get("path")
                caption = image.get("caption") or ""
            else:
                raise UsageError(f"第 {index + 1} 项图片格式无效")
            if not isinstance(path_text, str) or not path_text.strip():
                raise UsageError(f"第 {index + 1} 项图片缺少 path")
            if not isinstance(caption, str):
                raise UsageError(f"第 {index + 1} 项图片 caption 必须是字符串")
            image_path = Path(path_text).expanduser()
            if not image_path.is_absolute():
                image_path = base_dir / image_path
            image_path = image_path.resolve()
            if not image_path.is_file():
                raise UsageError(f"第 {index + 1} 项图片不存在: {image_path}")
            images.append({"path": str(image_path), "caption": caption.strip()})

        items.append(
            {
                "id": item_id,
                "proposition": proposition.strip(),
                "images": images,
                "other_options": options,
            }
        )

    return {
        "mode": "batch",
        "title": (title or "").strip() or "批量复核",
        "labels": labels,
        "items": items,
    }


def validate_server_args(args: argparse.Namespace) -> None:
    if args.timeout <= 0:
        raise UsageError("--timeout 必须大于 0")
    if args.port < 0 or args.port > 65535:
        raise UsageError("--port 必须在 0–65535 之间")


def prepare_result(args: argparse.Namespace) -> Path:
    result_path = Path(args.result).expanduser().resolve()
    try:
        result_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UsageError(f"无法创建结果目录 {result_path.parent}: {exc}") from exc
    return result_path


def ensure_result_free(result_path: Path) -> None:
    state = read_json(result_path)
    if not state or state.get("pending") is not True:
        return
    pid = state.get("pid")
    if isinstance(pid, int) and pid_alive(pid):
        raise UsageError(
            f"结果文件已被运行中的会话 {state.get('session')} 占用；"
            "换一个 --result，或先等它结束"
        )


def resolve_token(raw: str | None) -> str:
    token = raw or secrets.token_urlsafe(16)
    if not TOKEN_RE.match(token):
        raise UsageError("--token 需为 8–128 位字母、数字、_ 或 -")
    return token


def launch_server(
    config: dict,
    args: argparse.Namespace,
    result_path: Path,
    session: str,
    token: str,
) -> int:
    log_path = result_path.with_name(result_path.name + ".log")
    cmd = [
        sys.executable,
        str(SCRIPT_PATH),
        "_serve",
        "--session",
        session,
        f"--token={token}",
        "--result",
        str(result_path),
        "--host",
        args.host,
        "--port",
        str(args.port),
        "--timeout",
        str(args.timeout),
    ]
    payload = json.dumps(config, ensure_ascii=False).encode("utf-8")
    try:
        with log_path.open("wb") as log:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        try:
            if proc.stdin is not None:
                proc.stdin.write(payload)
                proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
    except OSError as exc:
        raise UsageError(f"无法启动服务: {exc}") from exc

    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        state = read_json(result_path)
        if state and state.get("session") == session:
            if state.get("url"):
                print(json.dumps(state, ensure_ascii=False))
                return 0
            if state.get("pending") is False:
                print(json.dumps(state, ensure_ascii=False))
                print(
                    f"voter: {state.get('error', '服务启动失败')}",
                    file=sys.stderr,
                )
                return 1
        if proc.poll() is not None:
            state = read_json(result_path)
            error = None
            if state and state.get("session") == session:
                error = state.get("error")
            payload_out = {
                "ok": False,
                "error": error or f"服务启动失败，查看 {log_path}",
                "result": str(result_path),
            }
            print(json.dumps(payload_out, ensure_ascii=False))
            print(f"voter: {payload_out['error']}", file=sys.stderr)
            return 1
        time.sleep(0.05)

    proc.terminate()
    payload_out = {
        "ok": False,
        "error": f"服务启动超时，查看 {log_path}",
        "result": str(result_path),
    }
    print(json.dumps(payload_out, ensure_ascii=False))
    print(f"voter: {payload_out['error']}", file=sys.stderr)
    return 1


def cmd_start(args: argparse.Namespace) -> int:
    validate_server_args(args)
    config = load_single_config(args)
    result_path = prepare_result(args)
    ensure_result_free(result_path)
    return launch_server(
        config,
        args,
        result_path,
        uuid.uuid4().hex[:12],
        resolve_token(args.token),
    )


def cmd_batch(args: argparse.Namespace) -> int:
    validate_server_args(args)
    manifest = Path(args.manifest).expanduser().resolve()
    config = load_manifest(manifest)
    result_path = prepare_result(args)
    ensure_result_free(result_path)
    return launch_server(
        config,
        args,
        result_path,
        uuid.uuid4().hex[:12],
        resolve_token(args.token),
    )


COMPACT_KEYS = (
    "ok",
    "pending",
    "mode",
    "session",
    "id",
    "title",
    "choice",
    "choice_type",
    "choice_label",
    "voted_at",
    "error",
    "partial",
    "summary",
    "answers",
    "total",
    "answered",
    "started_at",
    "finished_at",
    "timed_out_at",
    "result",
    "url",
)


def compact_state(state: dict) -> dict:
    return {key: state[key] for key in COMPACT_KEYS if key in state}


def cmd_wait(args: argparse.Namespace) -> int:
    if args.timeout is not None and args.timeout <= 0:
        raise UsageError("--timeout 必须大于 0")
    result_path = Path(args.result).expanduser().resolve()
    started = time.monotonic()
    deadline = (
        started + args.timeout if args.timeout is not None else None
    )
    fallback_deadline = started + DEFAULT_WAIT
    expected_session = args.session

    while True:
        state = read_json(result_path)
        now = time.monotonic()
        if state:
            if expected_session is None and state.get("pending") is True:
                expected_session = state.get("session")
            if expected_session and state.get("session") != expected_session:
                payload = {
                    "ok": False,
                    "pending": state.get("pending"),
                    "error": "结果文件属于其他会话",
                    "session": state.get("session"),
                    "expected_session": expected_session,
                    "result": str(result_path),
                }
                print(json.dumps(payload, ensure_ascii=False))
                return 1
            if state.get("pending") is False:
                data = state if args.full else compact_state(state)
                print(json.dumps(data, ensure_ascii=False))
                if state.get("ok") is True:
                    return 0
                return 124 if state.get("error") == "timeout" else 1
            if state.get("pending") is True:
                if deadline is None:
                    expires = state.get("expires_epoch")
                    if isinstance(expires, (int, float)) and not isinstance(
                        expires, bool
                    ):
                        deadline = float(expires) + 15
                    elif now >= fallback_deadline:
                        deadline = now
                pid = state.get("pid")
                if isinstance(pid, int) and not pid_alive(pid):
                    payload = dict(state)
                    payload.update(
                        {
                            "ok": False,
                            "pending": False,
                            "partial": True,
                            "error": "服务已退出但未写入结果",
                        }
                    )
                    data = payload if args.full else compact_state(payload)
                    print(json.dumps(data, ensure_ascii=False))
                    return 1
        elif deadline is None and now >= fallback_deadline:
            deadline = now

        if deadline is not None and now >= deadline:
            payload = {
                "ok": False,
                "pending": False,
                "error": "timeout",
                "session": expected_session,
                "result": str(result_path),
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 124

        time.sleep(0.2)


def add_server_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=0, help="监听端口（默认 0，自动分配）")
    parser.add_argument("--timeout", type=float, default=900, help="服务存活秒数（默认 900）")
    parser.add_argument(
        "--result",
        default=DEFAULT_RESULT,
        help=f"会话/结果文件（默认 {DEFAULT_RESULT}）",
    )
    parser.add_argument("--token", help=argparse.SUPPRESS)


def add_choice_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--other",
        "-o",
        action="append",
        default=[],
        metavar="TEXT",
        help="除是/否外的其他选项，可重复",
    )
    parser.add_argument("--id", help="本次投票的标识，写入结果 JSON")
    parser.add_argument("--yes-label", help="「是」按钮文字（默认 是）")
    parser.add_argument("--yes-hint", help="「是」按钮的副标题；自定义 label 时默认不显示")
    parser.add_argument("--no-label", help="「否」按钮文字（默认 否）")
    parser.add_argument("--no-hint", help="「否」按钮的副标题；自定义 label 时默认不显示")
    parser.add_argument("--progress", help="页面进度标记，例如 7/18")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="voter",
        description="Single-question or batch review votes over HTTP.",
        allow_abbrev=False,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser(
        "start", help="启动单题投票（后台运行，输出 URL）", allow_abbrev=False
    )
    start.add_argument("--proposition", "-p", help="命题文本（Markdown）")
    start.add_argument(
        "--proposition-file",
        "-f",
        metavar="PATH",
        help="从文件读取命题；传 - 从 stdin 读取",
    )
    start.add_argument(
        "--image",
        "-i",
        action="append",
        default=[],
        metavar="[图注=]PATH",
        help="附加图片，可重复；用 图注=路径 给图片加说明",
    )
    add_choice_args(start)
    add_server_args(start)

    batch = sub.add_parser(
        "batch", help="启动批量逐条复核（manifest 驱动）", allow_abbrev=False
    )
    batch.add_argument(
        "--manifest",
        required=True,
        metavar="PATH",
        help="清单 JSON：{title, labels, other_options, items[]}",
    )
    add_server_args(batch)

    wait = sub.add_parser("wait", help="等待并输出投票结果", allow_abbrev=False)
    wait.add_argument(
        "--result",
        default=DEFAULT_RESULT,
        help=f"会话/结果文件（默认 {DEFAULT_RESULT}）",
    )
    wait.add_argument("--session", help="只接受该会话的结果，防止读到旧结果")
    wait.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="最长等待秒数；默认跟随服务的 expires_at",
    )
    wait.add_argument("--full", action="store_true", help="输出完整结果（含命题）")

    serve = sub.add_parser("_serve", help=argparse.SUPPRESS, allow_abbrev=False)
    serve.add_argument("--session", required=True)
    serve.add_argument("--token", required=True)
    serve.add_argument("--result", required=True)
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
        if args.command == "batch":
            return cmd_batch(args)
        if args.command == "wait":
            return cmd_wait(args)
        return cmd_serve(args)
    except UsageError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        print(f"voter: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

