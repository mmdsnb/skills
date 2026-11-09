#!/usr/bin/env python3
"""Extract human prompts from qoder / opencode session history (read-only)."""

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

SKIP_PREFIXES = (
    "This session is being continued",
    "Output token limit hit",
    "[SYSTEM NOTIFICATION",
    "[Request interrupted",
    "[Image #",
    "Base directory for this skill",
    "<command-name>",
    "<local-command",
)


class ArgError(Exception):
    pass


def slug(project: Path) -> str:
    text = str(project)
    for char in ("/", "\\", "."):
        text = text.replace(char, "-")
    return text


def local_ts(value):
    try:
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value / 1000).strftime("%Y-%m-%d %H:%M")
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, OSError, OverflowError):
        return str(value)


def epoch_of(value) -> float:
    try:
        if isinstance(value, (int, float)):
            return float(value) / 1000
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, OSError, OverflowError):
        return 0.0


def clean(text: str) -> str:
    text = text.strip()
    if not text or any(text.startswith(prefix) for prefix in SKIP_PREFIXES):
        return ""
    return text


def message_text(message):
    content = (message or {}).get("content")
    if isinstance(content, str):
        yield content
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                yield block.get("text", "")


def qoder_messages(project: Path, qoder_home: Path):
    base = qoder_home / "projects" / slug(project)
    if not base.is_dir():
        return []
    rows = []
    for session_file in sorted(base.glob("*.jsonl")):
        with session_file.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("type") != "user" or data.get("isSidechain"):
                    continue
                if (data.get("origin") or {}).get("kind") != "human":
                    continue
                text = clean((data.get("humanInput") or {}).get("text") or "")
                if not text:
                    text = clean("\n".join(message_text(data.get("message"))))
                if text:
                    rows.append({
                        "source": "qoder",
                        "session": session_file.stem,
                        "ts": data.get("timestamp", ""),
                        "epoch": epoch_of(data.get("timestamp", "")),
                        "text": text,
                    })
    return rows


def opencode_messages(project: Path, db_path: Path):
    if not db_path.is_file():
        return []
    uri = f"file:{db_path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=10)
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            """
            SELECT s.id, m.time_created, m.data
            FROM session s
            JOIN session_message m ON m.session_id = s.id
            WHERE s.directory IN (?, ?)
              AND (s.parent_id IS NULL OR s.parent_id = '')
              AND m.type = 'user'
            ORDER BY s.time_created, m.seq
            """,
            (str(project), str(project) + "/"),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()

    out = []
    for session_id, created, raw in rows:
        try:
            data = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            continue
        text = clean(data.get("text") or "")
        if text:
            out.append({
                "source": "opencode",
                "session": session_id,
                "ts": created,
                "epoch": epoch_of(created),
                "text": text,
            })
    return out


def dedup(messages):
    seen = {}
    order = []
    for item in messages:
        key = re.sub(r"\s+", " ", item["text"]).strip()
        if key in seen:
            seen[key]["repeats"] += 1
            continue
        entry = dict(item, repeats=1)
        seen[key] = entry
        order.append(entry)
    return order


def digest(messages, project, max_chars):
    lines = [f"project: {project}", f"prompts: {len(messages)}", ""]
    for item in messages:
        flat = re.sub(r"\s+", " ", item["text"]).strip()
        if len(flat) > max_chars:
            flat = flat[:max_chars] + "…"
        tag = item["source"]
        if item["repeats"] > 1:
            tag += f" x{item['repeats']}"
        lines.append(f"[{local_ts(item['ts'])}|{tag}] {flat}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="提取 qoder / opencode 历史会话中的用户原话")
    parser.add_argument("--project", default=".", help="项目目录，默认当前目录")
    parser.add_argument("--source", choices=["auto", "qoder", "opencode", "all"], default="auto")
    parser.add_argument("--days", type=int, default=0, help="只取最近 N 天，0 为全部")
    parser.add_argument("--qoder-home", default=str(Path.home() / ".qoder"))
    parser.add_argument("--opencode-db", default=str(Path.home() / ".local/share/opencode/opencode.db"))
    parser.add_argument("--json", action="store_true", help="输出 JSON，默认输出可读摘要")
    parser.add_argument("--out", help="写入文件而不是 stdout")
    parser.add_argument("--max-chars", type=int, default=400)
    args = parser.parse_args(argv)

    if args.days < 0:
        print(json.dumps({"ok": False, "error": "--days 不能为负数"}, ensure_ascii=False))
        return 2
    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(json.dumps({"ok": False, "error": f"项目目录不存在: {project}"}, ensure_ascii=False))
        return 2

    use_qoder = args.source in ("auto", "qoder", "all")
    use_opencode = args.source in ("auto", "opencode", "all")
    messages = []
    if use_qoder:
        messages += qoder_messages(project, Path(args.qoder_home).expanduser())
    if use_opencode:
        messages += opencode_messages(project, Path(args.opencode_db).expanduser())

    if not messages:
        print(json.dumps({
            "ok": False,
            "error": f"没有找到 {project} 的历史会话；qoder 与 opencode 均可指定 --source",
        }, ensure_ascii=False))
        return 1

    if args.days:
        cutoff = datetime.now().timestamp() - args.days * 86400
        messages = [item for item in messages if not item["epoch"] or item["epoch"] >= cutoff]

    messages.sort(key=lambda item: item["epoch"])
    unique = dedup(messages)
    for item in unique:
        item.pop("epoch", None)
    stats = {
        "prompts": len(messages),
        "unique": len(unique),
        "duplicates": len(messages) - len(unique),
        "sessions": len({(item["source"], item["session"]) for item in messages}),
    }

    if args.json:
        body = json.dumps({
            "ok": True,
            "project": str(project),
            "stats": stats,
            "messages": unique,
        }, ensure_ascii=False, indent=2)
    else:
        body = digest(unique, project, args.max_chars)

    if args.out:
        Path(args.out).expanduser().write_text(body, encoding="utf-8")
        print(json.dumps({"ok": True, "out": args.out, **stats}, ensure_ascii=False))
    else:
        print(body)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
