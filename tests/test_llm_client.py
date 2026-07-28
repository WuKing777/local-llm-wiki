import http.client
import json
import threading
import time
import urllib.error
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from kb.llm import LLMConfig, OpenAICompatibleClient, load_llm_config


SENTINEL_API_KEY = "fake-sentinel-key-for-tests"


class FakeChatServer:
    def __init__(
        self,
        response_body: bytes | None = None,
        status: int = 200,
        delay_seconds: float = 0,
    ):
        self.response_body = response_body or json.dumps(
            {"choices": [{"message": {"content": "fake answer"}}]}
        ).encode("utf-8")
        self.status = status
        self.delay_seconds = delay_seconds
        self.requests: list[dict[str, object]] = []

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length)
                owner.requests.append(
                    {
                        "path": self.path,
                        "headers": dict(self.headers),
                        "body": json.loads(body.decode("utf-8")),
                    }
                )
                if owner.delay_seconds:
                    time.sleep(owner.delay_seconds)
                self.send_response(owner.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(owner.response_body)
                except OSError:
                    pass

            def log_message(self, _format, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever)
        self.thread.start()
        host, port = self.server.server_address
        self.base_url = f"http://{host}:{port}"
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


def client_for(
    base_url: str,
    api_key: str | None = None,
    timeout_seconds: float = 1,
    response_format: str | None = None,
    max_tokens: int | None = None,
    thinking: str | None = None,
    reasoning_effort: str | None = None,
):
    config = LLMConfig(
        base_url=base_url,
        model="fake-model",
        api_key=api_key,
        timeout_seconds=timeout_seconds,
        response_format=response_format,
        max_tokens=max_tokens,
        thinking=thinking,
        reasoning_effort=reasoning_effort,
    )
    return OpenAICompatibleClient(config)


class LLMConfigTests(unittest.TestCase):
    def test_missing_base_url_or_model_raises_runtime_error(self):
        with self.assertRaisesRegex(RuntimeError, "KB_LLM_BASE_URL"):
            load_llm_config({"KB_LLM_MODEL": "fake-model"})

        with self.assertRaisesRegex(RuntimeError, "KB_LLM_MODEL"):
            load_llm_config({"KB_LLM_BASE_URL": "http://127.0.0.1:1"})

    def test_deepseek_api_requires_api_key(self):
        with self.assertRaisesRegex(RuntimeError, "KB_LLM_API_KEY"):
            load_llm_config(
                {
                    "KB_LLM_BASE_URL": "https://api.deepseek.com",
                    "KB_LLM_MODEL": "deepseek-v4-pro",
                }
            )

        config = load_llm_config(
            {
                "KB_LLM_BASE_URL": "https://api.deepseek.com",
                "KB_LLM_MODEL": "deepseek-v4-pro",
                "KB_LLM_API_KEY": SENTINEL_API_KEY,
            }
        )
        self.assertEqual(SENTINEL_API_KEY, config.api_key)

    def test_invalid_timeout_raises_runtime_error(self):
        env = {
            "KB_LLM_BASE_URL": "http://127.0.0.1:1",
            "KB_LLM_MODEL": "fake-model",
            "KB_LLM_TIMEOUT_SECONDS": "not-a-number",
        }
        with self.assertRaisesRegex(RuntimeError, "KB_LLM_TIMEOUT_SECONDS"):
            load_llm_config(env)

        env["KB_LLM_TIMEOUT_SECONDS"] = "0"
        with self.assertRaisesRegex(RuntimeError, "KB_LLM_TIMEOUT_SECONDS"):
            load_llm_config(env)

        for timeout in ("NaN", "inf", "-inf"):
            env["KB_LLM_TIMEOUT_SECONDS"] = timeout
            with self.assertRaisesRegex(RuntimeError, "KB_LLM_TIMEOUT_SECONDS"):
                load_llm_config(env)

    def test_optional_structured_output_and_reasoning_config(self):
        config = load_llm_config(
            {
                "KB_LLM_BASE_URL": "https://api.deepseek.com",
                "KB_LLM_MODEL": "deepseek-v4-pro",
                "KB_LLM_API_KEY": SENTINEL_API_KEY,
                "KB_LLM_RESPONSE_FORMAT": "json_object",
                "KB_LLM_MAX_TOKENS": "8192",
                "KB_LLM_THINKING": "enabled",
                "KB_LLM_REASONING_EFFORT": "high",
            }
        )

        self.assertEqual("json_object", config.response_format)
        self.assertEqual(8192, config.max_tokens)
        self.assertEqual("enabled", config.thinking)
        self.assertEqual("high", config.reasoning_effort)

    def test_invalid_optional_llm_config_raises_runtime_error(self):
        base = {
            "KB_LLM_BASE_URL": "https://api.deepseek.com",
            "KB_LLM_MODEL": "deepseek-v4-pro",
            "KB_LLM_API_KEY": SENTINEL_API_KEY,
        }

        invalid_cases = [
            ("KB_LLM_RESPONSE_FORMAT", "xml", "KB_LLM_RESPONSE_FORMAT"),
            ("KB_LLM_MAX_TOKENS", "0", "KB_LLM_MAX_TOKENS"),
            ("KB_LLM_MAX_TOKENS", "many", "KB_LLM_MAX_TOKENS"),
            ("KB_LLM_THINKING", "maybe", "KB_LLM_THINKING"),
            ("KB_LLM_REASONING_EFFORT", "medium", "KB_LLM_REASONING_EFFORT"),
        ]
        for key, value, pattern in invalid_cases:
            env = dict(base)
            env[key] = value
            with self.subTest(key=key, value=value):
                with self.assertRaisesRegex(RuntimeError, pattern):
                    load_llm_config(env)


class OpenAICompatibleClientTests(unittest.TestCase):
    def test_request_uses_authorization_only_when_api_key_exists(self):
        messages = [{"role": "user", "content": "hello"}]

        with FakeChatServer() as server:
            result = client_for(server.base_url, api_key=SENTINEL_API_KEY).complete(
                messages
            )

        self.assertEqual("fake answer", result)
        self.assertEqual("/chat/completions", server.requests[0]["path"])
        self.assertEqual(
            f"Bearer {SENTINEL_API_KEY}",
            server.requests[0]["headers"].get("Authorization"),
        )
        self.assertEqual("fake-model", server.requests[0]["body"]["model"])
        self.assertEqual(messages, server.requests[0]["body"]["messages"])
        self.assertEqual(0.2, server.requests[0]["body"].get("temperature"))

        with FakeChatServer() as server:
            result = client_for(server.base_url).complete(messages)

        self.assertEqual("fake answer", result)
        self.assertNotIn("Authorization", server.requests[0]["headers"])

    def test_request_includes_optional_deepseek_compatible_parameters(self):
        messages = [{"role": "user", "content": "return json"}]

        with FakeChatServer() as server:
            result = client_for(
                server.base_url,
                response_format="json_object",
                max_tokens=8192,
                thinking="enabled",
                reasoning_effort="high",
            ).complete(messages)

        self.assertEqual("fake answer", result)
        self.assertEqual(
            {"type": "json_object"},
            server.requests[0]["body"]["response_format"],
        )
        self.assertEqual(8192, server.requests[0]["body"]["max_tokens"])
        self.assertEqual({"type": "enabled"}, server.requests[0]["body"]["thinking"])
        self.assertEqual("high", server.requests[0]["body"]["reasoning_effort"])

    def test_fake_sentinel_api_key_is_not_included_in_exception_text(self):
        with FakeChatServer(status=500, response_body=b'{"error":"boom"}') as server:
            client = client_for(server.base_url, api_key=SENTINEL_API_KEY)

            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "hello"}])

        self.assertNotIn(SENTINEL_API_KEY, str(context.exception))

    def test_valid_fake_openai_compatible_response_returns_message_content(self):
        body = json.dumps(
            {"choices": [{"message": {"content": "local completion"}}]}
        ).encode("utf-8")
        with FakeChatServer(response_body=body) as server:
            result = client_for(server.base_url.rstrip("/") + "/").complete(
                [{"role": "user", "content": "hello"}]
            )

        self.assertEqual("local completion", result)
        self.assertEqual("/chat/completions", server.requests[0]["path"])

    def test_connection_failure_raises_runtime_error(self):
        client = client_for("http://127.0.0.1")

        with patch(
            "kb.llm.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "hello"}])

    def test_read_protocol_failure_raises_sanitized_runtime_error(self):
        class BrokenResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return None

            def read(self):
                raise http.client.IncompleteRead(b"partial response bytes", 100)

        client = client_for("http://127.0.0.1")

        with patch("kb.llm.urllib.request.urlopen", return_value=BrokenResponse()):
            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "hello"}])

        self.assertIsNone(context.exception.__cause__)
        self.assertNotIn("partial response bytes", str(context.exception))

    def test_read_protocol_failure_is_retried_before_error(self):
        class BrokenResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return None

            def read(self):
                raise http.client.IncompleteRead(b"partial response bytes", 100)

        class GoodResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, _exc_type, _exc, _traceback):
                return None

            def read(self):
                return json.dumps(
                    {"choices": [{"message": {"content": "recovered"}}]}
                ).encode("utf-8")

        client = client_for("http://127.0.0.1")

        with patch(
            "kb.llm.urllib.request.urlopen",
            side_effect=[BrokenResponse(), GoodResponse()],
        ) as urlopen:
            result = client.complete([{"role": "user", "content": "hello"}])

        self.assertEqual("recovered", result)
        self.assertEqual(2, urlopen.call_count)

    def test_invalid_json_raises_sanitized_runtime_error(self):
        with FakeChatServer(response_body=b"not json") as server:
            client = client_for(server.base_url)

            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "hello"}])

        self.assertIsNone(context.exception.__cause__)
        self.assertNotIn("not json", str(context.exception))

    def test_invalid_utf8_raises_sanitized_runtime_error(self):
        with FakeChatServer(response_body=b"\xff\xfe") as server:
            client = client_for(server.base_url)

            with self.assertRaises(RuntimeError) as context:
                client.complete([{"role": "user", "content": "hello"}])

        self.assertIsNone(context.exception.__cause__)
        self.assertNotIn("\\xff", str(context.exception))

    def test_timeout_raises_runtime_error(self):
        with FakeChatServer(delay_seconds=0.2) as server:
            client = client_for(server.base_url, timeout_seconds=0.01)

            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "hello"}])

    def test_non_2xx_http_response_raises_runtime_error(self):
        with FakeChatServer(status=429, response_body=b'{"error":"rate limit"}') as server:
            client = client_for(server.base_url)

            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "hello"}])

    def test_invalid_json_raises_runtime_error(self):
        with FakeChatServer(response_body=b"not json") as server:
            client = client_for(server.base_url)

            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "hello"}])

    def test_missing_message_content_raises_runtime_error(self):
        body = json.dumps({"choices": [{"message": {}}]}).encode("utf-8")
        with FakeChatServer(response_body=body) as server:
            client = client_for(server.base_url)

            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "hello"}])

    def test_empty_message_content_raises_runtime_error(self):
        body = json.dumps({"choices": [{"message": {"content": ""}}]}).encode("utf-8")
        with FakeChatServer(response_body=body) as server:
            client = client_for(server.base_url)

            with self.assertRaises(RuntimeError):
                client.complete([{"role": "user", "content": "hello"}])


if __name__ == "__main__":
    unittest.main()
