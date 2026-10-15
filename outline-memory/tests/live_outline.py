#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["httpx>=0.27,<1"]
# ///

import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from uuid import uuid4

import httpx


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "outline.py"
spec = importlib.util.spec_from_file_location("outline_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


class LiveTests:
    def __init__(self):
        self.url, self.key = cli.settings()
        self.marker = "outlinetest" + uuid4().hex
        self.collection_names = set()
        self.document_titles = set()
        self.collections = {}
        self.documents = {}
        self.passed = 0
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {self.key}", "Accept": "application/json"},
            timeout=30,
            follow_redirects=False,
        )

    def passed_case(self, name):
        self.passed += 1
        print(f"PASS {name}", flush=True)

    def invoke(self, *args, code=0, env=None, stdin=None):
        result = subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=45,
            env=env,
        )
        require(self.key not in result.stdout + result.stderr, "CLI output exposed API key")
        payload = json.loads(result.stdout)
        if args[0] == "create-collection" and not result.returncode:
            self.collections[payload["data"]["id"]] = args[1]
        if args[0] == "create" and not result.returncode:
            self.documents[payload["data"]["id"]] = args[1]
        require(result.returncode == code, f"{args[0]}: expected exit {code}, got {result.returncode}; {payload.get('error', 'unexpected success')}")
        require(not result.stderr, "CLI must emit JSON only on stdout")
        if code:
            require(payload.get("ok") is False and bool(payload.get("error")), "failed CLI call has no JSON error")
            if code != 2 and args[0] in cli.WRITES:
                require(bool(payload.get("warning")), "failed write has no retry warning")
        else:
            require(payload.get("ok") is True and ("data" in payload or "help" in payload), "invalid success response")
        return payload

    def api(self, endpoint, body, missing=False):
        response = self.client.post(f"{self.url}/{endpoint}", json=body)
        if missing:
            require(response.status_code == 404, f"{endpoint}: expected 404, got {response.status_code}")
            return None
        require(response.is_success, f"{endpoint}: HTTP {response.status_code}")
        payload = response.json()
        require(payload.get("ok") is not False, f"{endpoint}: API rejected request")
        return payload

    def pages(self, endpoint, body=None):
        offset = 0
        while True:
            payload = self.api(endpoint, {**(body or {}), "limit": 100, "offset": offset})
            data = payload["data"]
            yield from data
            if len(data) < 100:
                return
            offset += len(data)

    def new_collection(self, suffix):
        name = self.marker + " " + suffix
        self.collection_names.add(name)
        description = "仅用于接口测试，运行结束后永久删除。"
        data = self.invoke("create-collection", name, "--description", description)["data"]
        stored = self.api("collections.info", {"id": data["id"]})["data"]
        require(stored["name"] == name and stored["description"] == description, "collection fields were not persisted")
        return data["id"]

    def new_document(self, suffix, location, location_id, content_flag=None, content=None, stdin=None):
        title = self.marker + " " + suffix
        self.document_titles.add(title)
        args = (content_flag, content) if content_flag else ()
        return self.invoke("create", title, location, location_id, *args, stdin=stdin)["data"]

    def search_ids(self, *args):
        payload = self.invoke("search", *args)
        return {item["document"]["id"] for item in payload["data"]}, payload

    def exercise(self, fail_after_create):
        main = self.new_collection("primary")
        control = self.new_collection("control")
        self.passed_case("create collections and verify persisted fields")

        collected = set()
        offset = 0
        while True:
            page = self.invoke("collections", "--limit", "1", "--offset", str(offset))
            require(page["pagination"]["limit"] == 1 and page["pagination"]["offset"] == offset, "collection pagination is incorrect")
            ids = {item["id"] for item in page["data"]}
            require(not (collected & ids), "collection pages overlap")
            collected |= ids
            if not ids:
                break
            offset += 1
        require({main, control} <= collected, "created collections missing from CLI listing")
        self.passed_case("collections pagination, offset and empty last page")

        original = "原始长期事实：机器名是测试机。"
        parent = self.new_document("主题", "--collection", main)
        parent_body = self.invoke("read", parent["id"])["data"]["text"]
        require("分类目录节点" in parent_body, "omitted category body was not persisted")
        child = self.new_document("对象", "--parent", parent["id"], "--file", "-", stdin=original)
        sibling_text = "文件创建的中文正文。\n\n第二段包含 Unicode：é。"
        with tempfile.TemporaryDirectory(prefix="outline-live-") as directory:
            path = Path(directory) / "中文 body.md"
            path.write_text(sibling_text, encoding="utf-8")
            sibling = self.new_document("同级", "--collection", main, "--file", str(path))
            other = self.new_document("对照", "--collection", control, "--text", "用于验证集合筛选。")
            require(len(self.documents) == 4, "did not create four distinct documents")
            for document, collection_id, parent_id, fragments in (
                (parent, main, None, ["分类目录节点"]),
                (child, main, parent["id"], [original]),
                (sibling, main, None, ["文件创建的中文正文。", "第二段包含 Unicode：é。"]),
                (other, control, None, ["用于验证集合筛选。"]),
            ):
                stored = self.invoke("read", document["id"])["data"]
                require(stored["title"] == document["title"], "document title changed")
                require(stored["collectionId"] == collection_id, "document in wrong collection")
                require(stored.get("parentDocumentId") == parent_id, "wrong parent document")
                require(bool(stored["publishedAt"]) and not stored.get("deletedAt"), "created document is not published")
                require(all(fragment in stored["text"] for fragment in fragments), "document body was not persisted")
                alternative = self.invoke("read", stored["urlId"])["data"]
                require(alternative["id"] == stored["id"] and alternative["text"] == stored["text"], "urlId read differs from UUID read")
            self.passed_case("published category/stdin/file documents, persisted bodies and UUID/urlId reads")
            self.invoke("append", child["id"], code=2)
            help_payload = self.invoke("append", "--help")
            require("标准输入" in help_payload["help"], "help omits stdin support")
            self.passed_case("argument errors and help are valid stdout JSON without stderr")

            if fail_after_create:
                raise InterruptedError("injected failure after creation")

            tree = self.invoke("tree", main)["data"]
            roots = {node["id"]: node for node in tree}
            require(set(roots) == {parent["id"], sibling["id"]}, "incorrect tree roots")
            children = roots[parent["id"]]["children"]
            require([node["id"] for node in children] == [child["id"]], "child is missing or in wrong tree position")
            require(children[0]["title"] == child["title"] and not children[0]["children"], "incorrect child tree metadata")
            self.passed_case("collection tree preserves parent-child hierarchy")

            expected = {parent["id"], child["id"], sibling["id"]}
            found, _ = self.search_ids(self.marker, "--collection", main, "--limit", "100")
            require(found == expected, "filtered search missing primary documents or leaking control document")
            found, _ = self.search_ids(self.marker, "--collection", control, "--limit", "100")
            require(found == {other["id"]}, "control collection search leaked primary documents")
            found, _ = self.search_ids(self.marker, "--limit", "100")
            require(found == set(self.documents), "unfiltered search did not find all four test documents")
            self.passed_case("search and meaningful cross-collection filter isolation")

            paged = set()
            for offset in range(4):
                found, page = self.search_ids(self.marker, "--collection", main, "--limit", "1", "--offset", str(offset))
                require(page["pagination"]["limit"] == 1 and page["pagination"]["offset"] == offset, "search pagination metadata is incorrect")
                require(len(found) == (1 if offset < 3 else 0), "incorrect search page size")
                require(not (paged & found), "search pages overlap")
                paged |= found
            require(paged == expected, "paginated search lost documents")
            empty, _ = self.search_ids("absent" + uuid4().hex, "--collection", main)
            require(not empty, "nonexistent search query returned matches")
            self.passed_case("search pagination, disjoint pages and empty results")

            before = self.invoke("read", child["id"])["data"]
            additions = ["新增修订一：机器名改为测试机二。", "新增修订二：长期服务端口为 8443。"]
            code_block = '```sh\nprintf "%s" "$HOME"\n`command`\npath="C:\\data"\n```'
            path.write_text("### 文件修订\n\n" + additions[1] + "\n\n" + code_block + "\n", encoding="utf-8")
            for identifier, flag, value, fragment in (
                (child["id"], "--text", "### 文本修订\n\n" + additions[0], additions[0]),
                (child["urlId"], "--file", "-", additions[1]),
            ):
                stdin = path.read_text(encoding="utf-8") if value == "-" else None
                update = self.invoke("append", identifier, flag, value, stdin=stdin)["data"]
                after = self.invoke("read", child["id"])["data"]
                require(update["id"] == child["id"], "append returned the wrong document")
                require(after["text"].startswith(before["text"].rstrip()), "append overwrote or reordered existing body")
                require(after["text"].count(original) == 1 and after["text"].count(fragment) == 1, "append duplicated or lost content")
                heading = "### 文本修订" if flag == "--text" else "### 文件修订"
                require("\n\n" + heading + "\n" in after["text"], "append heading lacks a Markdown paragraph boundary")
                require(after["text"].index(fragment) > after["text"].index(original), "revision was not appended")
                require(after["revision"] > before["revision"], "persisted revision did not increase")
                require(all(after[field] == before[field] for field in ("id", "title", "collectionId", "parentDocumentId")), "append modified identity or hierarchy")
                before = after
            require(all(before["text"].count(item) == 1 for item in [original, *additions]), "final body lost a prior revision")
            require(before["text"].index(additions[0]) < before["text"].index(additions[1]), "append order is incorrect")
            require(code_block in before["text"], "file append altered code fences, quotes, variables or backslashes")
            require(self.invoke("read", parent["id"])["data"]["text"] == parent_body, "append changed parent body")
            self.passed_case("text/file append preserves headings, code blocks, originals, identity and parent")

            revision = before["revision"]
            path.write_bytes(b"\xff\xfe")
            for flag, value in (("--text", " \n"), ("--file", "-"), ("--file", str(Path(directory) / "missing.md")), ("--file", str(path))):
                self.invoke("append", child["id"], flag, value, code=1, stdin="" if value == "-" else None)
            unchanged = self.invoke("read", child["id"])["data"]
            require(unchanged["text"] == before["text"] and unchanged["revision"] == revision, "rejected input changed persisted document")
            self.passed_case("blank/missing/non-UTF8 content rejected without changing data")

            missing = str(uuid4())
            self.api("documents.info", {"id": missing}, missing=True)
            self.invoke("read", missing, code=1)
            self.invoke("append", missing, "--text", "不能写入不存在的文档。", code=1)
            self.passed_case("real nonexistent-document read/write errors and write warning")

            response = self.client.post(f"{self.url}/collections.documents", json={"id": "not-a-uuid"})
            require(response.status_code in {400, 422}, "invalid collection UUID was not rejected as a validation error")
            error_payload = response.json()
            detail = next((error_payload[field].strip() for field in ("message", "error")
                           if isinstance(error_payload.get(field), str) and error_payload[field].strip()), "")
            require(bool(detail), "validation response has no error detail")
            error = self.invoke("tree", "not-a-uuid", code=1)
            require(cli.redact(detail, self.key) in error["error"], "CLI swallowed server validation details")
            self.passed_case("real invalid-UUID validation detail is preserved in CLI output")

            invalid_env = {**os.environ, "OUTLINE_API_KEY": uuid4().hex}
            error = self.invoke("collections", code=1, env=invalid_env)
            require("HTTP 401" in error["error"] or "HTTP 403" in error["error"], "invalid token was not rejected by server")
            no_env = {name: value for name, value in os.environ.items() if name not in {"OUTLINE_API_KEY", "OUTLINE_BASE_URL"}}
            self.invoke("collections", code=1, env=no_env)
            require(self.invoke("read", child["id"])["data"]["text"] == before["text"], "error-path tests changed document")
            self.passed_case("real invalid authentication and missing environment errors")

    def cleanup(self):
        errors = []
        try:
            for collection in self.pages("collections.list"):
                if collection["name"] in self.collection_names:
                    self.collections[collection["id"]] = collection["name"]
        except Exception as error:
            errors.append(f"collection reconciliation: {type(error).__name__}")

        for collection_id, name in self.collections.items():
            try:
                stored = self.api("collections.info", {"id": collection_id})["data"]
                require(stored["name"] == name and name in self.collection_names, "refused cleanup of unowned collection")
                documents = list(self.pages("documents.list", {"collectionId": collection_id}))
                documents.extend(document for document in self.pages("documents.deleted") if document["collectionId"] == collection_id)
                require(all(document["collectionId"] == collection_id and document["title"] in self.document_titles for document in documents), "refused cleanup: collection contains unfamiliar documents")
                for document in documents:
                    self.documents[document["id"]] = document["title"]
                documents.sort(key=lambda document: document.get("parentDocumentId") is None)
                for document in documents:
                    if not document.get("deletedAt"):
                        result = self.api("documents.delete", {"id": document["id"]})
                        require(result.get("success") is True, "soft deletion did not confirm success")
                    result = self.api("documents.delete", {"id": document["id"], "permanent": True})
                    require(result.get("success") is True, "permanent deletion did not confirm success")
                require(not list(self.pages("documents.list", {"collectionId": collection_id})), "collection still contains documents")
                result = self.api("collections.delete", {"id": collection_id})
                require(result.get("success") is True, "collection deletion did not confirm success")
            except Exception as error:
                errors.append(f"collection {collection_id}: {type(error).__name__}: {cli.redact(str(error), self.key) if isinstance(error, AssertionError) else 'cleanup request failed'}")

        try:
            for document_id in self.documents:
                self.api("documents.info", {"id": document_id}, missing=True)
            for collection_id in self.collections:
                self.api("collections.info", {"id": collection_id}, missing=True)
            require(not any(collection["name"] in self.collection_names for collection in self.pages("collections.list")), "test collection remains visible")
            require(not any(document["id"] in self.documents or document["title"] in self.document_titles for document in self.pages("documents.deleted")), "test documents remain in trash")
            matches, _ = self.search_ids(self.marker, "--limit", "100")
            require(not matches, "test documents remain searchable")
        except Exception as error:
            errors.append(f"cleanup verification: {type(error).__name__}: {cli.redact(str(error), self.key) if isinstance(error, AssertionError) else 'verification request failed'}")
        self.client.close()
        if errors:
            for error in errors:
                print(f"FAIL {error}", file=sys.stderr)
            print(f"Test marker: {self.marker}", file=sys.stderr)
            return False
        self.passed_case(f"permanent cleanup verified: {len(self.documents)} documents, {len(self.collections)} collections; no trash/search residue")
        return True


def main():
    parser = argparse.ArgumentParser(description="显式运行真实 Outline 写入测试；只创建独立测试数据，并在 finally 中永久清理。")
    parser.add_argument("--run", action="store_true", required=True, help="确认运行真实接口读写与测试数据清理")
    parser.add_argument("--fail-after-create", action="store_true", help="注入创建后的失败，验证失败路径同样清理全部数据")
    args = parser.parse_args()
    tests = None
    succeeded = False
    try:
        tests = LiveTests()
        tests.exercise(args.fail_after_create)
        succeeded = True
    except InterruptedError:
        if args.fail_after_create:
            tests.passed_case("injected failure reached; finally cleanup must still succeed")
            succeeded = True
    except (Exception, KeyboardInterrupt) as error:
        message = cli.redact(str(error), tests.key) if tests and isinstance(error, (AssertionError, cli.CLIError)) else type(error).__name__
        print(f"FAIL {message}", file=sys.stderr)
    finally:
        if tests:
            succeeded = tests.cleanup() and succeeded
    if tests:
        print(f"{'OK' if succeeded else 'FAILED'}: {tests.passed} live checks", flush=True)
    return 0 if succeeded else 1


if __name__ == "__main__":
    sys.exit(main())
