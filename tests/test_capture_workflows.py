import hashlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kb.commands import (
    _run_tesseract,
    ingest_derived,
    ingest_file,
    ingest_ocr,
    ingest_pdf,
    init_repository,
    ocr_check,
    search,
)
from kb.sources import read_source_card


def source_id_for(path: Path) -> str:
    return "src-" + hashlib.sha256(path.read_bytes()).hexdigest()[:12]


def write_minimal_pdf(path: Path, text: str) -> None:
    objects: list[bytes] = []

    def add(value: str) -> None:
        objects.append(value.encode("latin1"))

    add("1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n")
    add("2 0 obj << /Type /Pages /Kids [3 0 R] /Count 1 >> endobj\n")
    add(
        "3 0 obj << /Type /Page /Parent 2 0 R /Resources "
        "<< /Font << /F1 4 0 R >> >> /MediaBox [0 0 612 792] "
        "/Contents 5 0 R >> endobj\n"
    )
    add("4 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n")
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    add(f"5 0 obj << /Length {len(stream)} >> stream\n{stream}\nendstream endobj\n")
    content = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for obj in objects:
        offsets.append(len(content))
        content.extend(obj)
    xref = len(content)
    content.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin1"))
    for offset in offsets:
        content.extend(f"{offset:010d} 00000 n \n".encode("latin1"))
    content.extend(
        f"trailer << /Root 1 0 R /Size {len(objects) + 1} >>\n"
        f"startxref\n{xref}\n%%EOF\n".encode("latin1")
    )
    path.write_bytes(content)


class CaptureWorkflowTests(unittest.TestCase):
    def test_run_tesseract_decodes_utf8_output_on_windows(self):
        calls: dict[str, object] = {}

        def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.update(kwargs)
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout="知识库 OCR Test 123",
                stderr="",
            )

        with patch("kb.commands.subprocess.run", fake_run):
            text = _run_tesseract(["fake-tesseract", "scan.png", "stdout"])

        self.assertEqual("知识库 OCR Test 123", text)
        self.assertEqual("utf-8", calls["encoding"])
        self.assertEqual("replace", calls["errors"])

    def test_ingest_ocr_uses_local_runner_preserves_original_and_indexes_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            image = temp / "scan.png"
            image.write_bytes(b"fake image bytes")
            calls: list[list[str]] = []

            def fake_runner(args: list[str]) -> str:
                calls.append(args)
                return "OCR runner evidence"

            result = ingest_ocr(
                root,
                image,
                lang="eng",
                runner=fake_runner,
                env={"KB_TESSERACT_CMD": "fake-tesseract"},
            )

            self.assertEqual("ocr", result["workflow"])
            self.assertEqual(["fake-tesseract", str(image), "stdout", "-l", "eng"], calls[0])
            self.assertTrue((root / result["original_path"]).is_file())
            self.assertEqual(image.read_bytes(), (root / result["original_path"]).read_bytes())
            self.assertEqual(result["source_id"], search(root, "runner evidence")[0]["source_id"])

    def test_ingest_ocr_missing_tesseract_does_not_mutate_existing_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            image = temp / "scan.png"
            image.write_bytes(b"fake image bytes")
            tracked = [
                root / "meta" / "log.md",
                root / "meta" / "review-queue.md",
                root / "meta" / "source-map.jsonl",
                root / "db" / "kb.sqlite3",
            ]
            before = {path: path.read_bytes() for path in tracked}

            with self.assertRaisesRegex(RuntimeError, "KB_TESSERACT_CMD or tesseract"):
                ingest_ocr(root, image, env={"PATH": ""})

            self.assertEqual(before, {path: path.read_bytes() for path in tracked})
            self.assertEqual([], list((root / "sources").glob("src-*.md")))

    def test_ocr_check_reports_config_without_writing(self):
        result = ocr_check(env={"KB_TESSERACT_CMD": "fake-tesseract"})

        self.assertEqual("set", result["command"])

    def test_ingest_pdf_extracts_text_preserves_original_and_indexes_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            pdf = temp / "paper.pdf"
            write_minimal_pdf(pdf, "PDF searchable evidence")

            result = ingest_pdf(root, pdf)

            self.assertEqual("pdf-text", result["workflow"])
            self.assertTrue((root / result["original_path"]).is_file())
            self.assertTrue((root / result["raw_path"]).is_file())
            self.assertEqual(pdf.read_bytes(), (root / result["original_path"]).read_bytes())
            card = read_source_card(root / "sources" / f"{result['source_id']}.md")
            self.assertEqual("pdf-text", card["workflow"])
            self.assertEqual(result["source_id"], search(root, "PDF searchable")[0]["source_id"])

    def test_cli_ingest_pdf_extracts_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            pdf = temp / "cli-paper.pdf"
            write_minimal_pdf(pdf, "CLI PDF evidence")
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "ingest-pdf",
                    str(pdf),
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertTrue(search(root, "CLI PDF evidence"))

    def test_ingest_derived_preserves_pdf_original_and_indexes_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            original = temp / "paper.pdf"
            original.write_bytes(b"%PDF-1.4 scanned source bytes")
            extracted = temp / "paper.ocr.txt"
            extracted.write_text(
                "# OCR Paper\n\nOCR searchable evidence sentence.",
                encoding="utf-8",
            )

            result = ingest_derived(
                root,
                original=original,
                text=extracted,
                workflow="ocr",
            )

            self.assertEqual(source_id_for(extracted), result["source_id"])
            self.assertEqual("ocr", result["workflow"])
            self.assertTrue((root / result["original_path"]).is_file())
            self.assertEqual(original.read_bytes(), (root / result["original_path"]).read_bytes())
            self.assertEqual(extracted.read_bytes(), (root / result["raw_path"]).read_bytes())

            card = read_source_card(root / "sources" / f"{result['source_id']}.md")
            self.assertEqual("ocr", card["workflow"])
            self.assertEqual(result["original_path"], card["original_path"])
            self.assertEqual("text", card["kind"])
            self.assertEqual(result["source_id"], search(root, "OCR searchable")[0]["source_id"])

    def test_ingest_zotero_bibtex_export_as_reviewable_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            export = temp / "zotero-export.bib"
            export.write_text(
                "@article{karpathy2026wiki,\n"
                "  title={LLM Knowledge Bases},\n"
                "  author={Karpathy, Andrej}\n"
                "}\n",
                encoding="utf-8",
            )

            result = ingest_file(root, export, workflow="zotero")

            card = read_source_card(root / "sources" / f"{result['source_id']}.md")
            self.assertEqual("bibtex", card["kind"])
            self.assertEqual("zotero", card["workflow"])
            self.assertEqual(result["source_id"], search(root, "Knowledge Bases")[0]["source_id"])

    def test_cli_ingest_singlefile_marks_html_capture_workflow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            temp = Path(tmpdir)
            root = temp / "kb"
            init_repository(root)
            capture = temp / "singlefile.html"
            capture.write_text(
                "<html><head><script>secretNoise()</script></head>"
                "<body><h1>Saved Article</h1><p>SingleFile article evidence.</p></body></html>",
                encoding="utf-8",
            )
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "ingest-singlefile",
                    str(capture),
                    "--root",
                    str(root),
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            source_id = source_id_for(capture)
            card = read_source_card(root / "sources" / f"{source_id}.md")
            self.assertEqual("singlefile", card["workflow"])
            self.assertEqual(source_id, search(root, "article evidence")[0]["source_id"])
            self.assertFalse(search(root, "secretNoise"))


if __name__ == "__main__":
    unittest.main()
