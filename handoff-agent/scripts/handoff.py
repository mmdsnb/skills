#!/usr/bin/env uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""
Handoff Agent - Export/Import session between opencode and qoder
Usage:
    handoff.py export [output_dir]  - Export current session to markdown
    handoff.py import [output_dir]  - Find latest handoff file
"""

import json
import os
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


def get_project_dir(pwd: str) -> str:
    """Convert pwd to qoder project directory format."""
    return pwd.replace("/", "-")


def detect_and_get_session(pwd: str) -> tuple[str, str, str] | None:
    """Detect current tool and get session info. Returns (tool, session_id, title) or None."""

    # Detect which tool is running via environment variables
    is_qoder = os.environ.get("QODER_CLI") == "1"
    is_opencode = os.environ.get("OPENCODE_SESSION") is not None  # opencode may set this

    # Try qoder first if we detect we're running in qoder
    if is_qoder:
        qoder_projects = Path.home() / ".qoder/projects"
        if qoder_projects.exists():
            project_dir = get_project_dir(pwd)
            project_path = qoder_projects / project_dir
            if project_path.exists():
                jsonl_files = sorted(project_path.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
                if jsonl_files:
                    latest = jsonl_files[0]
                    session_id = latest.stem

                    # Try to get title from first real user message (skip commands)
                    title = session_id
                    try:
                        with open(latest) as f:
                            for line in f:
                                data = json.loads(line)
                                if data.get("type") == "user" and not data.get("isMeta"):
                                    content = data.get("message", {}).get("content", "")
                                    # Skip command messages
                                    if "<command-message>" in content or "<command-name>" in content:
                                        continue
                                    # Clean up content for title
                                    content = re.sub(r"<[^>]+>", "", content)
                                    content = content.strip()[:50]
                                    if content:
                                        title = content
                                    break
                    except Exception:
                        pass

                    return ("qoder", session_id, title)

    # Try opencode
    opencode_db = Path.home() / ".local/share/opencode/opencode.db"
    if opencode_db.exists():
        try:
            conn = sqlite3.connect(str(opencode_db))
            cursor = conn.execute(
                "SELECT id, title FROM session WHERE directory=? ORDER BY time_updated DESC LIMIT 1",
                (pwd,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                return ("opencode", row[0], row[1])
        except Exception:
            pass

    # Fallback: try qoder if not detected as qoder
    if not is_qoder:
        qoder_projects = Path.home() / ".qoder/projects"
        if qoder_projects.exists():
            project_dir = get_project_dir(pwd)
            project_path = qoder_projects / project_dir
            if project_path.exists():
                jsonl_files = sorted(project_path.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
                if jsonl_files:
                    latest = jsonl_files[0]
                    session_id = latest.stem
                    title = session_id
                    return ("qoder", session_id, title)

    return None


def export_opencode(session_id: str, title: str, output_file: Path, pwd: str) -> None:
    """Export opencode session to markdown."""
    import subprocess

    # Export to JSON
    result = subprocess.run(
        ["opencode", "export", session_id],
        capture_output=True, text=True
    )
    data = json.loads(result.stdout)

    with open(output_file, "w") as f:
        f.write(f"""# Session Handoff

- **Source Tool**: opencode
- **Session ID**: {session_id}
- **Session Title**: {title}
- **Directory**: {pwd}
- **Exported At**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Conversation

""")

        # Parse messages
        for msg in data.get("messages", []):
            role = msg.get("info", {}).get("role")
            if role not in ("user", "assistant"):
                continue

            for part in msg.get("parts", []):
                part_type = part.get("type")

                if part_type == "text":
                    text = part.get("text", "")
                    if text:
                        f.write(f"\n### {'User' if role == 'user' else 'Assistant'}\n\n{text}\n")

                elif part_type == "tool":
                    tool_name = part.get("tool", "unknown")
                    state = part.get("state", {})
                    cmd = state.get("input", {}).get("command", "N/A")
                    output = state.get("output", "N/A")
                    f.write(f"\n#### Tool Call: {tool_name}\n```\nCommand: {cmd}\nOutput:\n{output}\n```\n")


def export_qoder(session_id: str, title: str, output_file: Path, pwd: str) -> None:
    """Export qoder session to markdown."""
    project_dir = get_project_dir(pwd)
    jsonl_file = Path.home() / ".qoder/projects" / project_dir / f"{session_id}.jsonl"

    with open(output_file, "w") as f:
        f.write(f"""# Session Handoff

- **Source Tool**: qoder
- **Session ID**: {session_id}
- **Session Title**: {title}
- **Directory**: {pwd}
- **Exported At**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

## Conversation

""")

        with open(jsonl_file) as jf:
            for line in jf:
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get("type")

                if msg_type == "user":
                    if data.get("isMeta"):
                        continue
                    content = data.get("message", {}).get("content", "")

                    # Check if content is a list (tool results)
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "tool_result":
                                    result_content = block.get("content", "")
                                    if isinstance(result_content, list):
                                        texts = []
                                        for rb in result_content:
                                            if isinstance(rb, dict) and rb.get("type") == "text":
                                                texts.append(rb.get("text", ""))
                                            elif isinstance(rb, str):
                                                texts.append(rb)
                                        result_content = "\n".join(texts)
                                    if result_content:
                                        f.write(f"\n**Tool Result:**\n```\n{result_content}\n```\n")
                        continue

                    # Skip command messages
                    if "<command-message>" in content or "<command-name>" in content:
                        continue
                    if content:
                        f.write(f"\n### User\n\n{content}\n")

                elif msg_type == "assistant":
                    content_blocks = data.get("message", {}).get("content", [])
                    if isinstance(content_blocks, list):
                        for block in content_blocks:
                            block_type = block.get("type")
                            if block_type == "text":
                                text = block.get("text", "")
                                if text:
                                    f.write(f"\n### Assistant\n\n{text}\n")
                            elif block_type == "tool_use":
                                tool_name = block.get("name", "unknown")
                                tool_input = json.dumps(block.get("input", {}), ensure_ascii=False, indent=2)
                                f.write(f"\n#### Tool Call: {tool_name}\n```json\n{tool_input}\n```\n")
                    # Skip thinking blocks

                elif msg_type == "tool_result":
                    content = data.get("content", "")
                    if isinstance(content, list):
                        # Extract text from content blocks
                        texts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                texts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                texts.append(block)
                        content = "\n".join(texts)
                    if content:
                        f.write(f"\n**Tool Result:**\n```\n{content}\n```\n")


def do_export(output_dir: str) -> None:
    """Main export logic."""
    pwd = os.getcwd()
    result = detect_and_get_session(pwd)

    if not result:
        print("Error: Could not find session for current directory", file=sys.stderr)
        sys.exit(1)

    tool, session_id, title = result
    print(f"Detected tool: {tool}")
    print(f"Session: {title} ({session_id})")

    # Create output directory
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    # Sanitize title for filename
    safe_title = re.sub(r'[\/\\:*?"<>|]', "-", title)[:50]
    output_file = out_path / f"{safe_title}-latest.md"

    print(f"Exporting to: {output_file}")

    if tool == "opencode":
        export_opencode(session_id, title, output_file, pwd)
    elif tool == "qoder":
        export_qoder(session_id, title, output_file, pwd)

    print(f"Done! Exported to {output_file}")


def do_import(output_dir: str) -> None:
    """Find latest handoff file."""
    out_path = Path(output_dir)

    if not out_path.exists():
        print(f"No handoff directory found at {output_dir}", file=sys.stderr)
        sys.exit(1)

    handoff_files = sorted(out_path.glob("*-latest.md"), key=lambda f: f.stat().st_mtime, reverse=True)

    if not handoff_files:
        print(f"No handoff files found in {output_dir}", file=sys.stderr)
        sys.exit(1)

    print(handoff_files[0])


def main():
    action = sys.argv[1] if len(sys.argv) > 1 else "export"
    output_dir = sys.argv[2] if len(sys.argv) > 2 else ".handoff"

    if action == "export":
        do_export(output_dir)
    elif action == "import":
        do_import(output_dir)
    else:
        print(f"Usage: {sys.argv[0]} {{export|import}} [output_dir]", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
