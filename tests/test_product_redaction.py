import importlib
import json
import unittest


def _product_modules():
    try:
        redaction = importlib.import_module("kb.redaction")
        product_result = importlib.import_module("kb.product_result")
    except ModuleNotFoundError as exc:
        raise AssertionError(f"required module missing: {exc.name}") from None
    return redaction, product_result


class ProductRedactionTests(unittest.TestCase):
    def test_redacts_key_shaped_values_and_bearer_headers(self):
        redaction, _ = _product_modules()
        api_token = "sk-" + "sentinel" + "123456"
        bearer = "sentinel" + ".example" + ".token"

        redacted = redaction.redact_text(
            f"key={api_token}\nAuthorization: Bearer {bearer}"
        )

        self.assertFalse(api_token in redacted, "key-shaped value should be redacted")
        self.assertFalse(bearer in redacted, "bearer value should be redacted")
        self.assertIn("[redacted-api-key]", redacted)
        self.assertIn("Authorization: Bearer [redacted-bearer-token]", redacted)

    def test_redacts_named_secret_fields(self):
        redaction, _ = _product_modules()

        redacted = redaction.redact_text("api_key=abc password=def token=ghi")

        self.assertIn("api_key=[redacted-secret]", redacted)
        self.assertIn("password=[redacted-secret]", redacted)
        self.assertIn("token=[redacted-secret]", redacted)
        self.assertNotIn("abc", redacted)
        self.assertNotIn("def", redacted)
        self.assertNotIn("ghi", redacted)

    def test_redacts_configured_environment_secret_values(self):
        redaction, _ = _product_modules()
        llm_key = "fake-" + "sentinel-key-for-redaction"
        embedding_key = "fake-" + "embedding-key-for-redaction"

        redacted = redaction.redact_text(
            f"llm={llm_key} embedding={embedding_key}",
            env={
                "KB_LLM_API_KEY": llm_key,
                "KB_EMBEDDING_API_KEY": embedding_key,
            },
        )

        self.assertFalse(llm_key in redacted, "llm env value should be redacted")
        self.assertFalse(
            embedding_key in redacted, "embedding env value should be redacted"
        )
        self.assertEqual(redacted.count("[redacted-env-secret]"), 2)

    def test_short_generic_environment_secret_does_not_redact_structural_text(self):
        redaction, _ = _product_modules()

        redacted = redaction.redact_text(
            "root-exists",
            env={"GITHUB_TOKEN": "root"},
        )

        self.assertEqual("root-exists", redacted)

    def test_long_generic_environment_secret_is_still_redacted(self):
        redaction, _ = _product_modules()
        secret = "generic-ci-secret-sentinel"

        redacted = redaction.redact_text(
            f"value={secret}",
            env={"GITHUB_TOKEN": secret},
        )

        self.assertEqual("value=[redacted-env-secret]", redacted)

    def test_redacts_private_key_like_blocks(self):
        redaction, _ = _product_modules()
        private_block = (
            "-----BEGIN "
            + "PRIVATE KEY-----\n"
            + "not-real-private-material\n"
            + "-----END "
            + "PRIVATE KEY-----"
        )

        redacted = redaction.redact_text(f"before {private_block} after")

        self.assertIn("[redacted-private-key]", redacted)
        self.assertFalse("not-real-private-material" in redacted)

    def test_summarize_text_truncates_before_persistence(self):
        redaction, _ = _product_modules()

        summary = redaction.summarize_text("A" * 1200, limit=200)

        self.assertLessEqual(len(summary), 220)
        self.assertTrue(summary.endswith("[truncated]"))

    def test_product_result_serializes_deterministic_json_without_object_reprs(self):
        _, product_result = _product_modules()
        result = product_result.ProductResult(
            status="failed",
            classification="auth_failure",
            summary="Provider rejected credentials",
            severity="error",
            details={"attempt": 1, "operation": "llm-preflight"},
        )

        serialized = result.to_json()

        self.assertEqual(serialized, json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True))
        self.assertNotIn("ProductResult(", serialized)
        self.assertNotIn(" object at 0x", serialized)
        self.assertEqual(json.loads(serialized)["classification"], "auth_failure")

    def test_product_result_redacts_secret_like_detail_keys_recursively(self):
        _, product_result = _product_modules()
        secret_values = {
            "api_key": "plain-alpha",
            "password": "plain-beta",
            "token": "plain-gamma",
            "nested": {"secret": "plain-delta"},
            "items": [{"access_token": "plain-epsilon"}],
            "safe": "visible-summary",
        }
        result = product_result.ProductResult(
            status="failed",
            classification="auth_failure",
            summary="Provider rejected credentials",
            severity="error",
            details=secret_values,
        )

        data = json.loads(result.to_json())

        self.assertEqual(data["details"]["api_key"], "[redacted-secret]")
        self.assertEqual(data["details"]["password"], "[redacted-secret]")
        self.assertEqual(data["details"]["token"], "[redacted-secret]")
        self.assertEqual(data["details"]["nested"]["secret"], "[redacted-secret]")
        self.assertEqual(data["details"]["items"][0]["access_token"], "[redacted-secret]")
        self.assertEqual(data["details"]["safe"], "visible-summary")
        serialized = result.to_json()
        for value in (
            "plain-alpha",
            "plain-beta",
            "plain-gamma",
            "plain-delta",
            "plain-epsilon",
        ):
            self.assertNotIn(value, serialized)


if __name__ == "__main__":
    unittest.main()
