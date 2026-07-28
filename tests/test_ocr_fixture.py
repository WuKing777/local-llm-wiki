import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kb.ocr_fixture import create_ocr_fixture


class OcrFixtureTests(unittest.TestCase):
    def test_create_ocr_fixture_writes_png(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "ocr-smoke-chi-eng.png"

            result = create_ocr_fixture(output, text="外脑 OCR smoke 123")

            self.assertEqual(output.resolve(), Path(result["path"]))
            data = output.read_bytes()
            self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"BM"))
            self.assertGreater(len(data), 100)

    def test_create_ocr_fixture_writes_only_requested_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            output = temp / "nested" / "ocr-smoke-chi-eng.png"

            create_ocr_fixture(output, text="外脑 OCR smoke 123")

            files = sorted(
                path.relative_to(temp).as_posix()
                for path in temp.rglob("*")
                if path.is_file()
            )
            self.assertEqual(["nested/ocr-smoke-chi-eng.png"], files)

    def test_fixture_rejects_directory_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir)

            with self.assertRaisesRegex(RuntimeError, "output path is a directory"):
                create_ocr_fixture(output, text="外脑 OCR smoke 123")

    def test_cli_ocr_fixture(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output = Path(tmpdir) / "ocr-smoke-chi-eng.png"
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "ocr-fixture",
                    "--output",
                    str(output),
                    "--text",
                    "外脑 OCR smoke 123",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(str(output.resolve()) + "\n", completed.stdout)
            self.assertEqual("", completed.stderr)
            self.assertTrue(output.is_file())


if __name__ == "__main__":
    unittest.main()
