#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27,<1"]
# ///

import argparse
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

import httpx


ENDPOINTS = {
    "collections": "collections.list",
    "tree": "collections.documents",
    "read": "documents.info",
    "search": "documents.search",
    "create-collection": "collections.create",
    "create": "documents.create",
    "append": "documents.update",
}
WRITES = {"create-collection", "create", "append"}


class CLIError(Exception):
    pass


def settings():
    url = os.environ.get("OUTLINE_BASE_URL", "").strip().rstrip("/")
    key = os.environ.get("OUTLINE_API_KEY", "").strip()
    if not url or not key:
        raise CLIError("请设置 OUTLINE_BASE_URL 和 OUTLINE_API_KEY 环境变量。")
    try:
        parsed = urlsplit(url)
        valid = parsed.scheme in {"https", "http"} and parsed.hostname and parsed.port != 0
    except ValueError:
        valid = False
    if not valid or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CLIError("OUTLINE_BASE_URL 必须是 Outline 实例地址，不含凭据、查询参数或片段。")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise CLIError("远程 Outline 请使用 HTTPS，HTTP 仅允许本机回环地址。")
    if not key.isascii() or any(character.isspace() for character in key):
        raise CLIError("OUTLINE_API_KEY 必须是无空白的 ASCII 字符串。")
    if not parsed.path.endswith("/api"):
        url += "/api"
    return url, key


def limit_value(value):
    value = int(value)
    if not 1 <= value <= 100:
        raise argparse.ArgumentTypeError("limit 必须在 1–100 之间")
    return value


def offset_value(value):
    value = int(value)
    if value < 0:
        raise argparse.ArgumentTypeError("offset 不能为负数")
    return value


def parser():
    result = argparse.ArgumentParser(description="直接调用 Outline HTTP API，读取 OUTLINE_BASE_URL / OUTLINE_API_KEY。")
    commands = result.add_subparsers(dest="command", required=True)
    collections = commands.add_parser("collections", help="列出可见集合")
    tree = commands.add_parser("tree", help="查看集合中的文档层级")
    tree.add_argument("id", help="集合 ID")
    read = commands.add_parser("read", help="读取文档及已追加的修订")
    read.add_argument("id", help="文档 UUID 或 urlId")
    search = commands.add_parser("search", help="搜索文档")
    search.add_argument("query")
    search.add_argument("--collection", help="限定集合 ID")
    for command, default in ((collections, 25), (search, 10)):
        command.add_argument("--limit", type=limit_value, default=default)
        command.add_argument("--offset", type=offset_value, default=0)
    collection = commands.add_parser("create-collection", help="仅在用户授权初始化时创建集合")
    collection.add_argument("name")
    collection.add_argument("--description")
    create = commands.add_parser("create", help="在指定分类下新增并发布文档")
    create.add_argument("title")
    location = create.add_mutually_exclusive_group(required=True)
    location.add_argument("--collection", help="集合 ID")
    location.add_argument("--parent", help="父文档 ID")
    append = commands.add_parser("append", help="追加修改，不覆盖已有正文")
    append.add_argument("id", help="文档 UUID 或 urlId")
    for command in (create, append):
        content = command.add_mutually_exclusive_group(required=True)
        content.add_argument("--text", help="新增 Markdown 内容")
        content.add_argument("--file", help="包含新增内容的 UTF-8 文件")
    return result


def arguments(args):
    if args.command == "collections":
        return {"limit": args.limit, "offset": args.offset}
    if args.command == "search":
        body = {"query": args.query, "limit": args.limit, "offset": args.offset}
        if args.collection:
            body["filters"] = [{"field": "collectionId", "operator": "eq", "value": args.collection}]
        return body
    if args.command in {"tree", "read"}:
        return {"id": args.id}
    if args.command == "create-collection":
        body = {"name": args.name}
        if args.description:
            body["description"] = args.description
        return body
    try:
        text = Path(args.file).expanduser().read_text(encoding="utf-8") if args.file else args.text
    except (OSError, UnicodeError):
        raise CLIError("无法读取内容文件，请检查路径和 UTF-8 编码。") from None
    if not text.strip():
        raise CLIError("新增内容不能为空。")
    if args.command == "append":
        return {"id": args.id, "text": "\n\n" + text.lstrip("\r\n"), "editMode": "append"}
    body = {"title": args.title, "text": text, "publish": True}
    body["parentDocumentId" if args.parent else "collectionId"] = args.parent or args.collection
    return body


def redact(value, key):
    if isinstance(value, str):
        return value.replace(key, "[REDACTED]") if key else value
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, dict):
        return {redact(name, key): redact(item, key) for name, item in value.items()}
    return value


def main(argv=None):
    args = parser().parse_args(argv)
    key = ""
    code = 0
    try:
        url, key = settings()
        body = arguments(args)
        if args.command in WRITES and any(key in value for value in body.values() if isinstance(value, str)):
            raise CLIError("待写入内容包含 API key，已拒绝写入。")
        with httpx.Client(headers={"Authorization": f"Bearer {key}", "Accept": "application/json"}, timeout=30, follow_redirects=False) as client:
            response = client.post(f"{url}/{ENDPOINTS[args.command]}", json=body)
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or "data" not in payload or payload.get("ok") is False:
            raise CLIError("Outline API 未返回有效数据，请检查实例地址和权限。")
    except KeyboardInterrupt:
        code = 130
        payload = {"error": "调用已中断。"}
    except (CLIError, httpx.HTTPError, json.JSONDecodeError) as error:
        code = 1
        if isinstance(error, CLIError):
            message = str(error)
        elif isinstance(error, httpx.HTTPStatusError):
            detail = ""
            try:
                error_payload = error.response.json()
            except ValueError:
                error_payload = None
            if isinstance(error_payload, dict):
                for field in ("message", "error"):
                    value = error_payload.get(field)
                    if isinstance(value, str) and value.strip():
                        detail = f" ({value.strip()})"
                        break
            message = f"Outline API 返回 HTTP {error.response.status_code}{detail}，请检查地址、权限和请求参数。"
        elif isinstance(error, httpx.TimeoutException):
            message = "Outline API 请求超时。"
        elif isinstance(error, httpx.HTTPError):
            message = "无法连接 Outline API，请检查网络和 TLS 配置。"
        else:
            message = "Outline API 未返回 JSON，请检查 OUTLINE_BASE_URL。"
        payload = {"error": message}
    if code and args.command in WRITES:
        payload["warning"] = "不自动重试；先搜索或读取目标，确认是否已写入。"
    print(json.dumps(redact(payload, key), ensure_ascii=False, indent=2), file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main())
