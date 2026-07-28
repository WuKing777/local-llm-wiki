import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from kb.commands import init_repository
from kb.sources import source_id_and_sha256


class SchemaCheckTests(unittest.TestCase):
    def _replace_directory_with_symlink(self, link: Path, target: Path) -> None:
        if not hasattr(os, "symlink"):
            self.skipTest("directory symlink support is required for this test")
        shutil.rmtree(link)
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            self.skipTest(f"directory symlink unavailable: {exc}")

    def test_kb_manifest_schema_contract_matches_default_manifest_fields(self):
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "kb"
            / "schemas"
            / "kb-manifest.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertEqual(
            [
                "schema_version",
                "created_at",
                "engine_name",
                "engine_version",
                "profile_kind",
                "required_source_fields",
                "review_status_values",
                "contracts",
                "write_lock_integration",
            ],
            schema["required"],
        )
        self.assertNotIn("product", schema["properties"])

    def test_retrieval_benchmark_schema_records_runtime_evidence_and_privacy_gates(self):
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "kb"
            / "schemas"
            / "retrieval-benchmark.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("anyOf", schema)
        self.assertIn(
            {"required": ["expected_source_ids"]},
            schema["anyOf"],
        )
        self.assertIn(
            {"required": ["expected_wiki_paths"]},
            schema["anyOf"],
        )
        privacy_gate = schema["allOf"][0]
        self.assertEqual(
            ["sensitive", "restricted"],
            privacy_gate["if"]["properties"]["privacy"]["enum"],
        )
        self.assertEqual(True, privacy_gate["then"]["anyOf"][0]["properties"]["confirmed"]["const"])
        self.assertEqual(
            True,
            privacy_gate["then"]["anyOf"][1]["properties"]["user_confirmed"]["const"],
        )
        wiki_path_pattern = re.compile(
            schema["properties"]["expected_wiki_paths"]["items"]["pattern"]
        )
        self.assertIsNotNone(wiki_path_pattern.fullmatch("wiki/projects/alpha.md"))
        self.assertIsNone(wiki_path_pattern.fullmatch("wiki/../private.md"))
        self.assertIsNone(wiki_path_pattern.fullmatch("wiki/CON/file.md"))
        self.assertIsNone(wiki_path_pattern.fullmatch("wiki/con/file.md"))
        self.assertIsNone(wiki_path_pattern.fullmatch("wiki/file:stream.md"))

    def test_source_card_schema_records_secret_like_field_rejection(self):
        schema_path = (
            Path(__file__).resolve().parents[1]
            / "kb"
            / "schemas"
            / "source-card.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))

        self.assertIn("propertyNames", schema)
        pattern = schema["propertyNames"]["not"]["pattern"]
        self.assertIn("api[_-]?key", pattern)
        self.assertIn("token", pattern)
        self.assertIn("password", pattern)

    def test_cli_init_creates_root_manifest_with_schema_version_one(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [sys.executable, "-m", "kb", "init", "--root", str(root)],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            manifest_path = root / "meta" / "kb-manifest.json"
            self.assertTrue(manifest_path.is_file())
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(1, manifest["schema_version"])
            self.assertEqual("local-llm-wiki", manifest["engine_name"])
            self.assertEqual("0.1.0", manifest["engine_version"])
            self.assertEqual("personal_exobrain", manifest["profile_kind"])
            self.assertEqual(
                ["source_id", "title", "raw_path", "sha256", "imported_at", "kind"],
                manifest["required_source_fields"],
            )
            self.assertEqual(
                ["reviewed", "verified", "pass", "needs_reingest", "rejected"],
                manifest["review_status_values"],
            )

    def test_schema_check_passes_on_fresh_root(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            result = schema_check(root)

            data = result.to_dict()
            self.assertEqual("pass", data["status"])
            self.assertEqual("schema_ok", data["classification"])

    def test_retrieval_benchmark_expected_quotes_runtime_contract(self):
        from kb.commands import schema_check_repository

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            benchmark.parent.mkdir(parents=True)
            base_record = {
                "query": "aurora quote",
                "expected_source_ids": ["src-123456789abc"],
                "privacy": "public",
            }

            benchmark.write_text(
                json.dumps(base_record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            omitted = schema_check_repository(root).to_dict()
            self.assertEqual("pass", omitted["status"])
            self.assertEqual("schema_ok", omitted["classification"])

            valid_record = dict(base_record)
            valid_record["expected_quotes"] = ["local quote evidence"]
            benchmark.write_text(
                json.dumps(valid_record, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            valid = schema_check_repository(root).to_dict()
            self.assertEqual("pass", valid["status"])
            self.assertEqual("schema_ok", valid["classification"])

            invalid_values = [
                "local quote evidence",
                "",
                [],
                ["valid", ""],
                ["valid", 123],
            ]
            for value in invalid_values:
                with self.subTest(expected_quotes=value):
                    record = dict(base_record)
                    record["expected_quotes"] = value
                    benchmark.write_text(
                        json.dumps(record, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    result = schema_check_repository(root).to_dict()
                    self.assertEqual("failed", result["status"])
                    self.assertEqual(
                        "retrieval_benchmark_invalid",
                        result["classification"],
                    )
                    self.assertEqual(
                        "expected_quotes",
                        result["details"]["field"],
                    )

    def test_missing_manifest_is_no_write_by_default(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            manifest = root / "meta" / "kb-manifest.json"
            manifest.unlink()

            result = schema_check(root)

            data = result.to_dict()
            self.assertEqual("failed", data["status"])
            self.assertEqual("manifest_missing", data["classification"])
            self.assertFalse(manifest.exists())

    def test_write_manifest_writes_only_manifest_and_is_idempotent(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            manifest = root / "meta" / "kb-manifest.json"
            manifest.unlink()
            before = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )

            result = schema_check(root, write_manifest=True)
            after = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )
            result_again = schema_check(root, write_manifest=True)

            self.assertEqual("pass", result.to_dict()["status"])
            self.assertEqual(["meta/kb-manifest.json"], sorted(set(after) - set(before)))
            self.assertEqual("pass", result_again.to_dict()["status"])
            self.assertEqual(after, sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            ))

    def test_write_manifest_rejects_symlinked_meta_without_escape_write(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            outside = Path(tmpdir) / "outside-meta"
            outside.mkdir()
            self._replace_directory_with_symlink(root / "meta", outside)

            result = schema_check(root, write_manifest=True).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("path_escape", result["classification"])
            self.assertFalse((outside / "kb-manifest.json").exists())

    def test_schema_check_rejects_symlinked_sources_before_reading_cards(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            outside = Path(tmpdir) / "outside-sources"
            outside.mkdir()
            self._replace_directory_with_symlink(root / "sources", outside)

            result = schema_check(root).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("path_escape", result["classification"])
            self.assertEqual("sources", result["details"]["artifact"])

    def test_cli_write_manifest_writes_only_manifest(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            manifest = root / "meta" / "kb-manifest.json"
            manifest.unlink()
            before = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )
            project_root = Path(__file__).resolve().parents[1]

            completed = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "schema-check",
                    "--root",
                    str(root),
                    "--write-manifest",
                    "--json",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            after = sorted(
                path.relative_to(root).as_posix()
                for path in root.rglob("*")
                if path.is_file()
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            result = json.loads(completed.stdout)
            self.assertEqual("pass", result["status"])
            self.assertTrue(result["details"]["manifest_written"])
            self.assertEqual(["meta/kb-manifest.json"], sorted(set(after) - set(before)))

    def test_manifest_missing_created_at_is_invalid(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            manifest = root / "meta" / "kb-manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["created_at"] = ""
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            result = schema_check(root).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("manifest_invalid", result["classification"])
            self.assertEqual("created_at", result["details"]["field"])

    def test_manifest_missing_contracts_is_invalid(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            manifest = root / "meta" / "kb-manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            del data["contracts"]
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            result = schema_check(root).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("manifest_invalid", result["classification"])
            self.assertEqual("contracts", result["details"]["field"])

    def test_manifest_corrupt_write_lock_integration_is_invalid(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            manifest = root / "meta" / "kb-manifest.json"
            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["write_lock_integration"]["enforced"] = True
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

            result = schema_check(root).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("manifest_invalid", result["classification"])
            self.assertEqual("write_lock_integration", result["details"]["field"])

    def test_source_card_secret_field_is_invalid(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            raw = root / "raw" / "note.md"
            raw.write_text("# Note\nbody\n", encoding="utf-8")
            source_id, sha256 = source_id_and_sha256(raw.read_bytes())
            card = root / "sources" / f"{source_id}.md"
            card.write_text(
                "\n".join(
                    [
                        "---",
                        f"source_id: {source_id}",
                        "title: Note",
                        "raw_path: raw/note.md",
                        f"sha256: {sha256}",
                        "imported_at: 2026-07-03T00:00:00+08:00",
                        "kind: markdown",
                        "api_key: placeholder",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = schema_check(root).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("secret_in_source_card", result["classification"])
            self.assertEqual("$.api_key", result["details"]["field"])

    def test_schema_check_validates_root_local_profile_registry(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            registry = root / "meta" / "profile-registry.json"
            registry.write_text(
                json.dumps(
                    {
                        "profiles": [],
                        "api_key": "placeholder",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = schema_check(root).to_dict()

            self.assertEqual("failed", result["status"])
            self.assertEqual("secret_in_profile_registry", result["classification"])

    def test_manifest_version_classifications(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            manifest = root / "meta" / "kb-manifest.json"

            data = json.loads(manifest.read_text(encoding="utf-8"))
            data["schema_version"] = 999
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(
                "schema_upgrade_required",
                schema_check(root).to_dict()["classification"],
            )

            data["schema_version"] = 0
            manifest.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            self.assertEqual(
                "schema_unsupported",
                schema_check(root).to_dict()["classification"],
            )

    def test_source_card_missing_sha256_is_invalid(self):
        from kb.schema_check import schema_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            raw = root / "raw" / "note.md"
            raw.write_text("# Note\nbody\n", encoding="utf-8")
            source_id, _sha256 = source_id_and_sha256(raw.read_bytes())
            card = root / "sources" / f"{source_id}.md"
            card.write_text(
                "\n".join(
                    [
                        "---",
                        f"source_id: {source_id}",
                        "title: Note",
                        "raw_path: raw/note.md",
                        "imported_at: 2026-07-03T00:00:00+08:00",
                        "kind: markdown",
                        "---",
                        "",
                    ]
                ),
                encoding="utf-8",
            )

            result = schema_check(root)

            data = result.to_dict()
            self.assertEqual("failed", data["status"])
            self.assertEqual("source_card_invalid", data["classification"])
            self.assertEqual("sha256", data["details"]["missing_field"])

    def test_cli_schema_check_json_exit_code_matches_pass_status(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            project_root = Path(__file__).resolve().parents[1]

            passing = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "schema-check",
                    "--root",
                    str(root),
                    "--json",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(0, passing.returncode, passing.stderr)
            self.assertEqual("pass", json.loads(passing.stdout)["status"])

            (root / "meta" / "kb-manifest.json").unlink()
            failing = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "kb",
                    "schema-check",
                    "--root",
                    str(root),
                    "--json",
                ],
                cwd=project_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(1, failing.returncode)
            self.assertEqual("failed", json.loads(failing.stdout)["status"])
            self.assertEqual(
                "manifest_missing", json.loads(failing.stdout)["classification"]
            )


if __name__ == "__main__":
    unittest.main()
