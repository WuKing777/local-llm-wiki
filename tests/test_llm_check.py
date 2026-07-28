import os
import subprocess
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SENTINEL_API_KEY = "fake-sentinel-key-for-llm-check-tests"


def subprocess_env(env: dict[str, str] | None = None) -> dict[str, str]:
    merged = os.environ.copy()
    for key in (
        "KB_LLM_BASE_URL",
        "KB_LLM_MODEL",
        "KB_LLM_API_KEY",
        "KB_LLM_TIMEOUT_SECONDS",
        "KB_LLM_RESPONSE_FORMAT",
        "KB_LLM_MAX_TOKENS",
        "KB_LLM_THINKING",
        "KB_LLM_REASONING_EFFORT",
    ):
        merged.pop(key, None)
    if env:
        merged.update(env)
    return merged


def run_llm_check(env: dict[str, str] | None = None):
    return subprocess.run(
        [sys.executable, "-B", "-m", "kb", "llm-check"],
        cwd=PROJECT_ROOT,
        env=subprocess_env(env),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class LLMCheckTests(unittest.TestCase):
    def test_missing_config_outputs_one_error_line(self):
        result = run_llm_check()

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual(["error: KB_LLM_BASE_URL is required"], result.stderr.splitlines())

    def test_configured_check_succeeds_without_printing_secret(self):
        result = run_llm_check(
            {
                "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                "KB_LLM_MODEL": "fake-local-model",
                "KB_LLM_API_KEY": SENTINEL_API_KEY,
                "KB_LLM_TIMEOUT_SECONDS": "2",
                "KB_LLM_RESPONSE_FORMAT": "json_object",
                "KB_LLM_MAX_TOKENS": "8192",
                "KB_LLM_THINKING": "enabled",
                "KB_LLM_REASONING_EFFORT": "high",
            }
        )

        self.assertEqual(0, result.returncode)
        self.assertEqual("", result.stderr)
        self.assertIn("LLM config ok", result.stdout)
        self.assertIn("KB_LLM_BASE_URL=set", result.stdout)
        self.assertIn("KB_LLM_MODEL=set", result.stdout)
        self.assertIn("KB_LLM_API_KEY=set", result.stdout)
        self.assertIn("KB_LLM_TIMEOUT_SECONDS=2.0", result.stdout)
        self.assertIn("KB_LLM_RESPONSE_FORMAT=json_object", result.stdout)
        self.assertIn("KB_LLM_MAX_TOKENS=8192", result.stdout)
        self.assertIn("KB_LLM_THINKING=enabled", result.stdout)
        self.assertIn("KB_LLM_REASONING_EFFORT=high", result.stdout)
        self.assertNotIn(SENTINEL_API_KEY, result.stdout)
        self.assertNotIn(SENTINEL_API_KEY, result.stderr)

    def test_invalid_timeout_does_not_print_secret(self):
        result = run_llm_check(
            {
                "KB_LLM_BASE_URL": "http://127.0.0.1:9/v1",
                "KB_LLM_MODEL": "fake-local-model",
                "KB_LLM_API_KEY": SENTINEL_API_KEY,
                "KB_LLM_TIMEOUT_SECONDS": "0",
            }
        )

        self.assertEqual(1, result.returncode)
        self.assertEqual("", result.stdout)
        self.assertEqual(
            ["error: KB_LLM_TIMEOUT_SECONDS must be a positive number"],
            result.stderr.splitlines(),
        )
        self.assertNotIn(SENTINEL_API_KEY, result.stderr)


if __name__ == "__main__":
    unittest.main()
