import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kb.commands import init_obsidian_vault, lint_repository, status_repository


class ObsidianInitTests(unittest.TestCase):
    def test_init_obsidian_vault_creates_frontend_files_without_wiki_pages(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"

            result = init_obsidian_vault(root)

            self.assertEqual(Path(result["root"]), root.resolve())
            self.assertTrue((root / ".obsidian" / "app.json").is_file())
            self.assertTrue((root / ".obsidian" / "core-plugins.json").is_file())
            self.assertTrue((root / ".obsidian" / "templates.json").is_file())
            self.assertTrue((root / "meta" / "obsidian-home.md").is_file())
            self.assertTrue((root / "meta" / "templates" / "source-review.md").is_file())
            self.assertTrue((root / "meta" / "templates" / "wiki-page.md").is_file())
            self.assertTrue((root / "meta" / "assets").is_dir())
            self.assertEqual([], list((root / "wiki").glob("*.md")))
            self.assertEqual([], lint_repository(root))
            self.assertEqual(0, status_repository(root)["lint_issues"])

            app_config = json.loads((root / ".obsidian" / "app.json").read_text())
            self.assertEqual("inbox", app_config["newFileFolderPath"])
            self.assertEqual("meta/assets", app_config["attachmentFolderPath"])
            templates_config = json.loads(
                (root / ".obsidian" / "templates.json").read_text()
            )
            self.assertEqual("meta/templates", templates_config["folder"])

    def test_init_obsidian_vault_is_idempotent_and_preserves_existing_config(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            obsidian = root / ".obsidian"
            meta = root / "meta"
            obsidian.mkdir(parents=True)
            meta.mkdir(parents=True)
            app = obsidian / "app.json"
            home = meta / "obsidian-home.md"
            app.write_text('{"user":"setting"}\n', encoding="utf-8")
            home.write_text("# My Home\n", encoding="utf-8")

            first = init_obsidian_vault(root)
            second = init_obsidian_vault(root)

            self.assertEqual([], second["created_dirs"])
            self.assertEqual([], second["created_files"])
            self.assertEqual('{"user":"setting"}\n', app.read_text(encoding="utf-8"))
            self.assertEqual("# My Home\n", home.read_text(encoding="utf-8"))
            self.assertIn(str((root / "wiki").resolve()), first["created_dirs"])

    def test_cli_obsidian_init_creates_vault(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [sys.executable, "-B", "-m", "kb", "obsidian-init", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertIn("Initialized Obsidian vault", completed.stdout)
            self.assertTrue((root / ".obsidian" / "app.json").is_file())
            self.assertTrue((root / "meta" / "obsidian-home.md").is_file())


if __name__ == "__main__":
    unittest.main()
