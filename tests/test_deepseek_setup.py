import re
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class DeepSeekSetupScriptTests(unittest.TestCase):
    def test_api_key_setup_script_prompts_without_committed_key(self):
        script = PROJECT_ROOT / "tools" / "set-deepseek-api-key.ps1"

        text = script.read_text(encoding="utf-8")

        self.assertIn("Read-Host", text)
        self.assertIn("-AsSecureString", text)
        self.assertIn("KB_LLM_API_KEY", text)
        self.assertNotRegex(text, re.compile(r"sk-[A-Za-z0-9]{12,}"))
        self.assertNotIn("<your api key>", text.lower())


if __name__ == "__main__":
    unittest.main()
