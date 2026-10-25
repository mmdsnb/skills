#!/usr/bin/env python3
"""Run a one-shot task on a cheap model backend with read-only directory access."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path

ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)
STRAY_THINK_TAG_RE = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)
PROMPT_PLACEHOLDER = "{prompt}"
PREAMBLE = (
    "You are a one-shot worker. Produce only the requested output. Do not add "
    "explanations, headings, or code fences unless the task asks for them."
)

BUILTIN_BACKENDS: dict[str, dict] = {
    "qoder": {
        "description": "Qoder CLI 经济模型 + Glob/Read，只读指定目录",
        "command": [
            "qoder",
            "-p",
            "-m",
            "efficient",
            "--tools",
            "Glob,Read",
            "--no-session-persistence",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
        ],
    },
}


class UsageError(Exception):
    pass


class ConfigError(Exception):
    pass


@dataclass
class RunResult:
    ok: bool
    result: str | None
    error: str | None
    exit_code: int
    elapsed: float


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / "outsource-agent" / "backends.toml"


def resolve_config_path(arg: Path | None) -> Path:
    if arg is not None:
        return arg.expanduser()
    env = os.environ.get("OUTSOURCE_AGENT_CONFIG")
    if env:
        return Path(env).expanduser()
    return default_config_path()


def load_config(path: Path) -> tuple[dict[str, dict], str | None]:
    backends = {name: dict(cfg) for name, cfg in BUILTIN_BACKENDS.items()}
    file_default: str | None = None
    if not path.exists():
        return backends, file_default
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"无法读取配置 {path}: {exc}") from exc

    file_default = data.get("default_backend")
    user_backends = data.get("backends", {})
    if not isinstance(user_backends, dict):
        raise ConfigError(f"配置 {path} 中 [backends] 必须是表")
    for name, cfg in user_backends.items():
        if not isinstance(cfg, dict):
            raise ConfigError(f"后端 {name} 的配置必须是表")
        backends[name] = cfg
    return backends, file_default


def select_backend(
    backends: dict[str, dict], cli_name: str | None, file_default: str | None
) -> tuple[str, dict]:
    name = (
        cli_name
        or os.environ.get("OUTSOURCE_AGENT_BACKEND")
        or file_default
        or "qoder"
    )
    if name not in backends:
        available = ", ".join(sorted(backends))
        raise ConfigError(f"未知后端 {name!r}，可用：{available}")
    backend = backends[name]
    command = backend.get("command")
    if (
        not isinstance(command, list)
        or not command
        or not all(isinstance(part, str) for part in command)
    ):
        raise ConfigError(f"后端 {name!r} 的 command 必须是非空字符串数组")
    env = backend.get("env", {})
    if not isinstance(env, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in env.items()
    ):
        raise ConfigError(f"后端 {name!r} 的 env 必须是字符串键值表")
    return name, backend


def build_prompt(task: str) -> str:
    return f"{PREAMBLE}\n\n<task>\n{task.strip()}\n</task>\n"


def execute(
    backend: dict,
    prompt: str,
    cwd: Path,
    timeout: float,
    debug: bool,
) -> RunResult:
    cmd: list[str] = []
    prompt_via_stdin = True
    for arg in backend["command"]:
        if PROMPT_PLACEHOLDER in arg:
            cmd.append(arg.replace(PROMPT_PLACEHOLDER, prompt))
            prompt_via_stdin = False
        else:
            cmd.append(arg)

    env = os.environ.copy()
    env.update(backend.get("env") or {})

    if debug:
        shown = [
            part.replace(prompt, f"<prompt:{len(prompt)} chars>")
            for part in cmd
        ]
        print("+ " + " ".join(shlex.quote(part) for part in shown), file=sys.stderr)
        print(f"  cwd={cwd}", file=sys.stderr)

    start = time.monotonic()
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            env=env,
            stdin=subprocess.PIPE if prompt_via_stdin else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise UsageError(f"找不到后端命令 {cmd[0]!r}: {exc}") from exc

    try:
        out, err = proc.communicate(
            prompt if prompt_via_stdin else None, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        elapsed = time.monotonic() - start
        return RunResult(False, None, f"后端超时（{timeout:g}s）", 124, elapsed)

    elapsed = time.monotonic() - start
    out = THINK_BLOCK_RE.sub("", out)
    out = STRAY_THINK_TAG_RE.sub("", out)
    out = ANSI_RE.sub("", out).strip()
    err = ANSI_RE.sub("", err).strip()

    if proc.returncode != 0:
        detail = err or out or f"无输出（退出码 {proc.returncode}）"
        return RunResult(False, None, detail, 1, elapsed)
    if not out:
        return RunResult(False, None, err or "后端没有返回内容", 1, elapsed)
    return RunResult(True, out, None, 0, elapsed)


def cmd_backends(config_path: Path, as_json: bool) -> int:
    backends, file_default = load_config(config_path)
    default_name = (
        os.environ.get("OUTSOURCE_AGENT_BACKEND") or file_default or "qoder"
    )
    if as_json:
        payload = {
            "config": str(config_path),
            "exists": config_path.exists(),
            "default_backend": default_name,
            "backends": {
                name: {
                    "description": cfg.get("description", ""),
                    "command": cfg.get("command", []),
                    "env_keys": sorted((cfg.get("env") or {}).keys()),
                }
                for name, cfg in sorted(backends.items())
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"config: {config_path} ({'found' if config_path.exists() else 'missing, using builtins'})")
    for name, cfg in sorted(backends.items()):
        marker = "*" if name == default_name else " "
        print(f"{marker} {name}\t{cfg.get('description', '')}")
    return 0


def cmd_run(args: argparse.Namespace, config_path: Path) -> int:
    backends, file_default = load_config(config_path)
    backend_name, backend = select_backend(backends, args.backend, file_default)

    if args.task == "-":
        task = sys.stdin.read()
    else:
        task = args.task
    if not task.strip():
        raise UsageError("任务为空")

    prompt = build_prompt(task)
    timeout = args.timeout
    if timeout <= 0:
        raise UsageError("--timeout 必须大于 0")

    with (
        nullcontext(Path(args.dir).expanduser().resolve())
        if args.dir
        else tempfile.TemporaryDirectory(prefix="outsource-agent-")
    ) as workdir:
        target = Path(workdir)
        if args.dir and not target.is_dir():
            raise UsageError(f"--dir 不是目录: {target}")
        result = execute(backend, prompt, target, timeout, args.debug)

    if args.json:
        payload = {
            "ok": result.ok,
            "backend": backend_name,
            "elapsed_sec": round(result.elapsed, 2),
            "result": result.result,
            "error": result.error,
        }
        print(json.dumps(payload, ensure_ascii=False))
    else:
        if result.ok:
            sys.stdout.write((result.result or "") + "\n")
        else:
            print(f"outsource-agent: {result.error}", file=sys.stderr)
    return result.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="outsource-agent",
        description="One-shot task delegation to a cheap model backend.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="后端配置文件（默认 ~/.config/outsource-agent/backends.toml）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="执行单次任务")
    run.add_argument("--task", "-t", required=True, help="任务文本；传 - 从 stdin 读取")
    run.add_argument("--backend", "-b", help="后端名（默认取环境变量/配置/builtin）")
    run.add_argument(
        "--dir",
        help="后端工作目录；缺省为空临时目录",
    )
    run.add_argument("--timeout", type=float, default=180, help="超秒数（默认 180）")
    run.add_argument("--json", action="store_true", help="输出 JSON")
    run.add_argument("--debug", action="store_true", help="把实际命令打印到 stderr")

    listing = sub.add_parser("backends", help="列出后端")
    listing.add_argument("--json", action="store_true", help="输出 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    config_path = resolve_config_path(args.config)
    try:
        if args.command == "backends":
            return cmd_backends(config_path, args.json)
        return cmd_run(args, config_path)
    except (UsageError, ConfigError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"outsource-agent: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
