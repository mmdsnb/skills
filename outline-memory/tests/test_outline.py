import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "outline.py"
spec = importlib.util.spec_from_file_location("outline_cli", SCRIPT)
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)
HTTP_CLIENT = httpx.Client
ENV = {"OUTLINE_BASE_URL": "http://localhost:8080", "OUTLINE_API_KEY": "test-secret-key"}
live_spec = importlib.util.spec_from_file_location("outline_live", Path(__file__).with_name("live_outline.py"))
live = importlib.util.module_from_spec(live_spec)
live_spec.loader.exec_module(live)


class OutlineTests(unittest.TestCase):
    def invoke(self, argv, response=None, env=None):
        requests = []
        def handler(request):
            requests.append(request)
            if isinstance(response, BaseException):
                raise response
            return response if response is not None else httpx.Response(200, json={"ok": True, "data": []})
        def client(**kwargs):
            return HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs)
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ENV if env is None else env, clear=True), \
             patch.object(cli.httpx, "Client", side_effect=client), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main(argv)
        output = json.loads(stderr.getvalue() if code else stdout.getvalue())
        self.assertEqual(stdout.getvalue() == "", bool(code))
        return code, output, requests

    def test_read_and_search_routes(self):
        cases = [
            (["collections", "--limit", "10", "--offset", "20"], "collections.list", {"limit": 10, "offset": 20}),
            (["tree", "collection-id"], "collections.documents", {"id": "collection-id"}),
            (["read", "document-id"], "documents.info", {"id": "document-id"}),
            (["search", "项目", "--collection", "collection-id", "--offset", "10"], "documents.search",
             {"query": "项目", "filters": [{"field": "collectionId", "operator": "eq", "value": "collection-id"}], "limit": 10, "offset": 10}),
            (["search", "项目"], "documents.search", {"query": "项目", "limit": 10, "offset": 0}),
        ]
        for argv, endpoint, body in cases:
            with self.subTest(endpoint=endpoint):
                code, output, requests = self.invoke(argv)
                self.assertEqual(code, 0)
                self.assertEqual(output["data"], [])
                self.assertEqual(len(requests), 1)
                self.assertEqual(requests[0].method, "POST")
                self.assertEqual(str(requests[0].url), f"http://localhost:8080/api/{endpoint}")
                self.assertEqual(requests[0].headers["Authorization"], "Bearer test-secret-key")
                self.assertEqual(requests[0].headers["Accept"], "application/json")
                self.assertEqual(requests[0].headers["Content-Type"], "application/json")
                self.assertEqual(json.loads(requests[0].content), body)

    def test_base_url_accepts_instance_and_api_paths(self):
        for path, expected in (("", "/api/collections.list"), ("/", "/api/collections.list"),
                               ("/api", "/api/collections.list"), ("/api/", "/api/collections.list"),
                               ("/outline", "/outline/api/collections.list"), ("/outline/api/", "/outline/api/collections.list")):
            with self.subTest(path=path):
                code, _, requests = self.invoke(["collections"], env={**ENV, "OUTLINE_BASE_URL": ENV["OUTLINE_BASE_URL"] + path})
                self.assertEqual(code, 0)
                self.assertEqual(requests[0].url.path, expected)

    def test_local_openapi_contract(self):
        api = json.loads(Path(__file__).with_name("spec3.json").read_text(encoding="utf-8"))
        for endpoint in cli.ENDPOINTS.values():
            operation = api["paths"]["/" + endpoint]["post"]
            self.assertIn("application/json", operation["requestBody"]["content"])
            response = operation["responses"]["200"]["content"]["application/json"]["schema"]
            self.assertIn("data", response["properties"])
        self.assertEqual(api["components"]["securitySchemes"]["BearerAuth"]["scheme"], "bearer")
        update = api["paths"]["/documents.update"]["post"]["requestBody"]["content"]["application/json"]["schema"]
        self.assertIn("id", update["required"])
        self.assertEqual(update["properties"]["editMode"]["$ref"], "#/components/schemas/TextEditMode")
        self.assertIn("append", api["components"]["schemas"]["TextEditMode"]["enum"])
        filters = api["components"]["schemas"]["DocumentFilter"]["oneOf"][0]["properties"]
        self.assertIn("collectionId", filters["field"]["enum"])
        self.assertIn("eq", filters["operator"]["enum"])
        search = api["paths"]["/documents.search"]["post"]["requestBody"]["content"]["application/json"]["schema"]["allOf"][1]["properties"]
        self.assertEqual(search["filters"]["items"]["$ref"], "#/components/schemas/DocumentFilter")
        self.assertTrue(search["collectionId"]["deprecated"])

    def test_create_under_collection_or_parent(self):
        for flag, field in (("--collection", "collectionId"), ("--parent", "parentDocumentId")):
            code, _, requests = self.invoke(["create", "环境", flag, "location-id", "--text", "长期事实"])
            self.assertEqual(code, 0)
            self.assertEqual(requests[0].url.path, "/api/documents.create")
            self.assertEqual(json.loads(requests[0].content), {"title": "环境", field: "location-id", "text": "长期事实", "publish": True})

    def test_append_is_only_new_text(self):
        code, _, requests = self.invoke(["append", "document-id", "--text", "新修订"])
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].url.path, "/api/documents.update")
        self.assertEqual(json.loads(requests[0].content), {"id": "document-id", "text": "\n\n新修订", "editMode": "append"})

    def test_append_normalizes_leading_newlines_without_changing_markdown(self):
        markdown = '### 修订\n\n```sh\n  value="$HOME"\n  `command`\n```\n'
        for leading in ("", "\n", "\n\n", "\r\n\r\n", "\n\r\n\n"):
            with self.subTest(leading=repr(leading)):
                code, _, requests = self.invoke(["append", "doc", "--text", leading + markdown])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(requests[0].content)["text"], "\n\n" + markdown)
        code, _, requests = self.invoke(["append", "doc", "--text", "\r\n    indented code\n"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(requests[0].content)["text"], "\n\n    indented code\n")

    def test_initialize_collection(self):
        code, _, requests = self.invoke(["create-collection", "个人知识", "--description", "长期上下文"])
        self.assertEqual(code, 0)
        self.assertEqual(requests[0].url.path, "/api/collections.create")
        self.assertEqual(json.loads(requests[0].content), {"name": "个人知识", "description": "长期上下文"})

    def test_utf8_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "revision.txt"
            path.write_text("### 修订\n- 中文事实\n", encoding="utf-8")
            code, _, requests = self.invoke(["append", "doc", "--file", str(path)])
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(requests[0].content)["text"], "\n\n" + path.read_text(encoding="utf-8"))

    def test_file_content_preserves_shell_metacharacters_for_create_and_append(self):
        text = '\n\n### 修订\n\n```sh\nprintf "%s" "$HOME"\n`command`\npath="C:\\data"\n```\n'
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "中文 $body.md"
            path.write_text(text, encoding="utf-8")
            for argv, expected in ((["create", "标题", "--collection", "collection-id"], text),
                                   (["append", "doc"], "\n\n" + text.lstrip("\r\n"))):
                with self.subTest(command=argv[0]):
                    code, _, requests = self.invoke([*argv, "--file", str(path)])
                    self.assertEqual(code, 0)
                    self.assertEqual(json.loads(requests[0].content)["text"], expected)

    def test_http_error_includes_message_or_error_without_retry(self):
        for status, payload, expected in (
            (400, {"message": "collectionId must be a valid UUID", "error": "validation_error"}, "collectionId must be a valid UUID"),
            (422, {"error": "validation_error"}, "validation_error"),
            (429, {"message": "Rate limit exceeded; try again in 60 seconds", "error": "rate_limit_exceeded"}, "Rate limit exceeded; try again in 60 seconds"),
            (400, {"message": {"field": "id"}, "error": "validation_error"}, "validation_error"),
            (400, {"message": "  ", "error": "validation_error"}, "validation_error"),
        ):
            with self.subTest(status=status, payload=payload):
                code, output, requests = self.invoke(["append", "doc", "--text", "新修订"], response=httpx.Response(status, json=payload))
                self.assertEqual(code, 1)
                self.assertIn(f"HTTP {status}", output["error"])
                self.assertIn(f"({expected})", output["error"])
                self.assertIn("warning", output)
                self.assertEqual(len(requests), 1)

    def test_http_error_ignores_non_json_and_unusable_error_fields(self):
        responses = [httpx.Response(400, text="<html>test-secret-key</html>"), httpx.Response(400, content=b"\xff")]
        responses.extend(httpx.Response(400, json=value) for value in ([], "test-secret-key", None, {"message": [], "error": 400}, {}))
        for response in responses:
            with self.subTest(response=response):
                code, output, requests = self.invoke(["read", "doc"], response=response)
                self.assertEqual(code, 1)
                self.assertEqual(output["error"], "Outline API 返回 HTTP 400，请检查地址、权限和请求参数。")
                self.assertEqual(len(requests), 1)

    def test_http_error_detail_redacts_configured_key(self):
        for field in ("message", "error"):
            with self.subTest(field=field):
                response = httpx.Response(400, json={field: "invalid token test-secret-key"})
                code, output, requests = self.invoke(["append", "doc", "--text", "新修订"], response=response)
                self.assertEqual(code, 1)
                self.assertIn("invalid token [REDACTED]", output["error"])
                self.assertNotIn(ENV["OUTLINE_API_KEY"], json.dumps(output))
                self.assertEqual(len(requests), 1)

    def test_missing_environment(self):
        for env in ({}, {"OUTLINE_BASE_URL": ENV["OUTLINE_BASE_URL"]}, {"OUTLINE_API_KEY": ENV["OUTLINE_API_KEY"]},
                    {"OUTLINE_URL": ENV["OUTLINE_BASE_URL"], "OUTLINE_API_KEY": ENV["OUTLINE_API_KEY"]}):
            code, output, requests = self.invoke(["collections"], env=env)
            self.assertEqual(code, 1)
            self.assertIn("OUTLINE_BASE_URL", output["error"])
            self.assertIn("OUTLINE_API_KEY", output["error"])
            self.assertEqual(requests, [])

    def test_invalid_url_or_key(self):
        for value in ("http://remote.test", "https://user:pass@remote.test", "https://remote.test/?secret=key", "https://remote.test/#key", "https://remote.test:bad"):
            code, _, requests = self.invoke(["collections"], env={**ENV, "OUTLINE_BASE_URL": value})
            self.assertEqual(code, 1)
            self.assertEqual(requests, [])
        for key in ("key\nheader", "key header", "中文key"):
            code, _, requests = self.invoke(["collections"], env={**ENV, "OUTLINE_API_KEY": key})
            self.assertEqual(code, 1)
            self.assertEqual(requests, [])

    def test_interrupted_append_warns_without_traceback(self):
        code, output, requests = self.invoke(["append", "doc", "--text", "新修订"], response=KeyboardInterrupt())
        self.assertEqual(code, 130)
        self.assertIn("中断", output["error"])
        self.assertIn("warning", output)
        self.assertEqual(len(requests), 1)

    def test_content_errors_and_secret_refusal(self):
        for flag, value in (("--text", " \n"), ("--file", "/nonexistent/revision.txt"), ("--text", "test-secret-key")):
            code, output, requests = self.invoke(["append", "doc", flag, value])
            self.assertEqual(code, 1)
            self.assertEqual(requests, [])
            self.assertNotIn("test-secret-key", json.dumps(output))

    def test_errors_do_not_retry_or_expose_secrets(self):
        responses = [httpx.Response(status, text="test-secret-key") for status in (401, 403, 409, 500)]
        responses.extend([httpx.ReadTimeout("test-secret-key"), httpx.ConnectError("test-secret-key"),
                          httpx.Response(200, text="not json"), httpx.Response(200, json={"ok": False, "error": "test-secret-key"})])
        for response in responses:
            with self.subTest(response=response):
                code, output, requests = self.invoke(["append", "doc", "--text", "新修订"], response=response)
                self.assertEqual(code, 1)
                self.assertIn("warning", output)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("test-secret-key", json.dumps(output))

    def test_redirect_is_not_followed(self):
        code, _, requests = self.invoke(["collections"], response=httpx.Response(307, headers={"Location": "http://localhost:9090/other"}))
        self.assertEqual(code, 1)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "localhost")
        self.assertEqual(requests[0].url.port, 8080)

    def test_success_preserves_data_and_pagination_and_redacts_key(self):
        response = httpx.Response(200, json={"data": {"text": "test-secret-key 中文"}, "pagination": {"offset": 10, "limit": 10}})
        code, output, _ = self.invoke(["read", "doc"], response=response)
        self.assertEqual(code, 0)
        self.assertEqual(output["data"]["text"], "[REDACTED] 中文")
        self.assertEqual(output["pagination"], {"offset": 10, "limit": 10})

    def test_no_ambiguous_write_or_dangerous_command(self):
        cases = [
            ["create", "标题", "--text", "内容"],
            ["create", "标题", "--collection", "a", "--parent", "b", "--text", "内容"],
            ["append", "doc", "--text", "a", "--file", "b"],
            ["collections", "--limit", "101"], ["search", "对象", "--offset", "-1"],
            ["delete", "doc"], ["tools"],
        ]
        for argv in cases:
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    cli.parser().parse_args(argv)
                self.assertEqual(error.exception.code, 2)


class LiveCleanupTests(unittest.TestCase):
    def cleanup(self, unfamiliar=False, permanent_forbidden=False, lost_response=False, trashed=False):
        requests = []
        state = {"collection": True, "document": "trash" if trashed else "active"}
        collection = {"id": "collection-id", "name": "unique-test primary"}
        document = {"id": "document-id", "title": "unfamiliar" if unfamiliar else "unique-test 对象",
                    "collectionId": collection["id"], "parentDocumentId": None}

        def handler(request):
            endpoint = request.url.path.removeprefix("/api/")
            body = json.loads(request.content)
            requests.append((endpoint, body))
            if endpoint == "collections.list":
                payload = {"data": [collection] if state["collection"] else []}
            elif endpoint == "collections.info":
                if not state["collection"]:
                    return httpx.Response(404, json={"ok": False})
                payload = {"data": collection}
            elif endpoint == "documents.list":
                self.assertEqual(body["collectionId"], collection["id"])
                payload = {"data": [document] if state["document"] == "active" else []}
            elif endpoint == "documents.deleted":
                payload = {"data": [{**document, "deletedAt": "2026-10-15T00:00:00Z"}] if state["document"] == "trash" else []}
            elif endpoint == "documents.delete":
                self.assertEqual(body["id"], document["id"])
                if body.get("permanent"):
                    self.assertEqual(state["document"], "trash", "permanent deletion must follow soft deletion")
                    if permanent_forbidden:
                        return httpx.Response(403, json={"ok": False})
                    state["document"] = "gone"
                else:
                    self.assertEqual(state["document"], "active")
                    state["document"] = "trash"
                payload = {"success": True}
            elif endpoint == "collections.delete":
                self.assertEqual(body["id"], collection["id"])
                self.assertEqual(state["document"], "gone", "delete collection only after permanently deleting documents")
                state["collection"] = False
                payload = {"success": True}
            elif endpoint == "documents.info":
                if state["document"] != "active":
                    return httpx.Response(404, json={"ok": False})
                payload = {"data": document}
            else:
                self.fail(f"unexpected endpoint: {endpoint}")
            return httpx.Response(200, json={"ok": True, **payload})

        def client(**kwargs):
            return HTTP_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, ENV, clear=True), patch.object(live.httpx, "Client", side_effect=client), \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            tests = live.LiveTests()
            tests.marker = "unique-test"
            tests.collection_names = {collection["name"]}
            tests.document_titles = {"unique-test 对象"}
            if not lost_response:
                tests.collections = {collection["id"]: collection["name"]}
                tests.documents = {document["id"]: "unique-test 对象"}
            with patch.object(tests, "invoke", return_value={"ok": True, "data": []}) as invoke:
                result = tests.cleanup()
            if result:
                invoke.assert_called_once_with("search", "unique-test", "--limit", "100")
        self.assertNotIn(ENV["OUTLINE_API_KEY"], stdout.getvalue() + stderr.getvalue())
        return result, requests, stderr.getvalue()

    def test_cleanup_soft_deletes_then_permanently_deletes_then_removes_collection(self):
        result, requests, errors = self.cleanup()
        self.assertTrue(result, errors)
        writes = [(endpoint, body) for endpoint, body in requests if endpoint.endswith(".delete")]
        self.assertEqual(writes, [("documents.delete", {"id": "document-id"}),
                                  ("documents.delete", {"id": "document-id", "permanent": True}),
                                  ("collections.delete", {"id": "collection-id"})])

    def test_cleanup_reconciles_lost_creation_response_by_exact_owned_names(self):
        result, requests, errors = self.cleanup(lost_response=True)
        self.assertTrue(result, errors)
        self.assertIn(("documents.delete", {"id": "document-id", "permanent": True}), requests)

    def test_cleanup_can_finish_already_trashed_owned_document(self):
        result, requests, errors = self.cleanup(trashed=True)
        self.assertTrue(result, errors)
        self.assertNotIn(("documents.delete", {"id": "document-id"}), requests)
        self.assertIn(("documents.delete", {"id": "document-id", "permanent": True}), requests)

    def test_cleanup_refuses_unfamiliar_active_or_trashed_document(self):
        for trashed in (False, True):
            with self.subTest(trashed=trashed):
                result, requests, errors = self.cleanup(unfamiliar=True, trashed=trashed)
                self.assertFalse(result)
                self.assertIn("unfamiliar documents", errors)
                self.assertFalse(any(endpoint.endswith(".delete") for endpoint, _ in requests))

    def test_cleanup_reports_permission_failure_and_never_claims_success(self):
        result, requests, errors = self.cleanup(permanent_forbidden=True)
        self.assertFalse(result)
        self.assertIn("HTTP 403", errors)
        self.assertNotIn(("collections.delete", {"id": "collection-id"}), requests)

    def test_live_runner_requires_explicit_run_flag(self):
        with patch.object(live.sys, "argv", ["live_outline.py"]), \
             patch.object(live, "LiveTests") as constructor, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                live.main()
            self.assertEqual(error.exception.code, 2)
            constructor.assert_not_called()


if __name__ == "__main__":
    unittest.main()
