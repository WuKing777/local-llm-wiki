import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import kb.commands as commands
from kb.commands import ingest_file, init_repository


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SECRET = "draft-secret-token"
SHORT_SECRET = "sek"


def issue_types(issues: list[dict[str, str]]) -> set[str]:
    return {issue["type"] for issue in issues}


def claim_for(
    source_id: str,
    text: str,
    *,
    paragraph: int = 1,
    quote: str | None = None,
    chunk: str | None = None,
    claim_id: str = "claim-1",
) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "paragraph": paragraph,
        "text": text,
        "evidence": [
            {
                "chunk": chunk or f"{source_id}#0",
                "quote": quote or text,
            }
        ],
    }


def metadata_for(source_id: str, **overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "draft_id": "draft-test",
        "title": "Draft Title",
        "query": "draft validation",
        "created_at": "2026-06-24T00:00:00Z",
        "model": "test-model",
        "prompt_hash": "a" * 64,
        "context_sources": [source_id],
        "context_chunks": [f"{source_id}#0"],
        "claims": [claim_for(source_id, "Grounded paragraph cites")],
    }
    metadata.update(overrides)
    return metadata


def write_draft(root: Path, metadata: dict[str, object], body: str) -> Path:
    draft_path = root / "wiki" / "_drafts" / "draft-title.md"
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in metadata.items()
    )
    lines.extend(["---", "", body.rstrip(), ""])
    draft_path.write_text("\n".join(lines), encoding="utf-8")
    return draft_path


def write_raw_draft(root: Path, content: str, name: str = "draft-title.md") -> Path:
    draft_path = root / "wiki" / "_drafts" / name
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(content, encoding="utf-8")
    return draft_path


def create_root_with_sources(temp: Path) -> tuple[Path, str, str]:
    root = temp / "kb"
    init_repository(root)
    first = temp / "first.md"
    second = temp / "second.md"
    first.write_text(
        (
            "# First\n\n"
            "Grounded paragraph cites. Alpha beta evidence. "
            "Gamma delta evidence. Unrelated quote exists. "
            "First grounded paragraph cites. Second grounded paragraph cites."
        ),
        encoding="utf-8",
    )
    second.write_text("# Second\n\nSecond source evidence.", encoding="utf-8")
    first_id = ingest_file(root, first)["source_id"]
    second_id = ingest_file(root, second)["source_id"]
    return root, first_id, second_id


def valid_body(source_id: str) -> str:
    return f"# Draft Title\n\nGrounded paragraph cites {source_id}."


def run_validate_cli(root: Path, draft_path: Path, *extra: str):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "kb",
            "validate-draft",
            "--root",
            str(root),
            str(draft_path),
            *extra,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class DraftValidationTests(unittest.TestCase):
    def test_rejects_draft_path_outside_drafts(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            outside = root / "wiki" / "outside.md"
            outside.write_text(valid_body(source_id), encoding="utf-8")

            issues = commands.validate_draft_file(root, outside)

            self.assertIn("draft-path-outside-drafts", issue_types(issues))

    def test_accepts_root_relative_draft_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            write_draft(root, metadata_for(source_id), valid_body(source_id))

            issues = commands.validate_draft_file(
                root, Path("wiki") / "_drafts" / "draft-title.md"
            )

            self.assertEqual([], issues)

    def test_rejects_root_relative_traversal_outside_drafts(self):
        traversal_paths = [
            Path("..") / "outside.md",
            Path("wiki") / ".." / "raw" / "x.md",
            Path("wiki") / "_drafts" / ".." / ".." / "meta" / "log.md",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            for draft_path in traversal_paths:
                with self.subTest(draft_path=draft_path):
                    issues = commands.validate_draft_file(root, draft_path)

                    self.assertIn("draft-path-outside-drafts", issue_types(issues))

    def test_rejects_symlinked_drafts_directory_outside_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root, source_id, _other_id = create_root_with_sources(base)
            external_drafts = base / "external-drafts"
            external_drafts.mkdir()
            try:
                os.symlink(
                    external_drafts,
                    root / "wiki" / "_drafts",
                    target_is_directory=True,
                )
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("draft-path-outside-drafts", issue_types(issues))

    def test_rejects_symlinked_wiki_directory_outside_root(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root, source_id, _other_id = create_root_with_sources(base)
            external_wiki = base / "external-wiki"
            external_wiki.mkdir()
            shutil.rmtree(root / "wiki")
            try:
                os.symlink(external_wiki, root / "wiki", target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest(f"directory symlink unavailable: {exc}")
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("draft-path-outside-drafts", issue_types(issues))

    def test_rejects_missing_front_matter_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            metadata = metadata_for(source_id)
            metadata.pop("model")
            draft_path = write_draft(root, metadata, valid_body(source_id))

            issues = commands.validate_draft_file(root, draft_path)

            self.assertTrue(
                any(
                    issue["type"] == "missing-draft-field"
                    and issue["field"] == "model"
                    for issue in issues
                )
            )

    def test_rejects_missing_claims_front_matter_field(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            metadata = metadata_for(source_id)
            metadata.pop("claims")
            draft_path = write_draft(root, metadata, valid_body(source_id))

            issues = commands.validate_draft_file(root, draft_path)

            self.assertTrue(
                any(
                    issue["type"] == "missing-draft-field"
                    and issue["field"] == "claims"
                    for issue in issues
                )
            )

    def test_rejects_missing_front_matter_as_missing_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_raw_draft(root, valid_body(source_id))

            issues = commands.validate_draft_file(root, draft_path)

            self.assertEqual(
                set(metadata_for(source_id)),
                {
                    issue["field"]
                    for issue in issues
                    if issue["type"] == "missing-draft-field"
                },
            )

    def test_cli_missing_front_matter_outputs_issues_not_runtime_error(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_raw_draft(root, valid_body(source_id))

            completed = run_validate_cli(root, draft_path)

            self.assertEqual(1, completed.returncode)
            self.assertIn("missing-draft-field", completed.stdout)
            self.assertEqual("", completed.stderr)

    def test_unclosed_front_matter_returns_validation_issues_and_cli_stdout(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_raw_draft(
                root,
                "\n".join(
                    [
                        "---",
                        "draft_id: draft-test",
                        "title: Draft Title",
                        "",
                        valid_body(source_id),
                    ]
                ),
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("invalid-draft-front-matter", issue_types(issues))
            self.assertIn("missing-draft-field", issue_types(issues))

            completed = run_validate_cli(root, draft_path)
            self.assertEqual(1, completed.returncode)
            self.assertIn("invalid-draft-front-matter", completed.stdout)
            self.assertEqual("", completed.stderr)
            self.assertNotIn("Traceback", completed.stderr)

    def test_invalid_front_matter_line_returns_validation_issues(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_raw_draft(
                root,
                "\n".join(
                    [
                        "---",
                        "draft_id draft-test",
                        "---",
                        "",
                        valid_body(source_id),
                    ]
                ),
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("invalid-draft-front-matter", issue_types(issues))
            self.assertIn("missing-draft-field", issue_types(issues))

    def test_rejects_fake_context_source(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            fake_source = "src-deadbeef0000"
            draft_path = write_draft(
                root,
                metadata_for(fake_source),
                valid_body(fake_source),
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("invalid-context-source", issue_types(issues))

    def test_rejects_context_chunk_source_not_in_context_sources(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id, context_chunks=[f"{other_id}#0"]),
                valid_body(source_id),
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("invalid-context-chunk", issue_types(issues))

    def test_accepts_comma_separated_context_source_and_chunk_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, other_id = create_root_with_sources(Path(tmpdir))
            content = "\n".join(
                [
                    "---",
                    "draft_id: draft-test",
                    "title: Draft Title",
                    "query: draft validation",
                    "created_at: 2026-06-24T00:00:00Z",
                    "model: test-model",
                    f"prompt_hash: {'a' * 64}",
                    f"context_sources: {source_id}, {other_id}",
                    f"context_chunks: {source_id}#0, {other_id}#2",
                    "claims: "
                    + json.dumps(
                        [
                            {
                                "claim_id": "claim-1",
                                "paragraph": 1,
                                "text": "Grounded paragraph cites",
                                "evidence": [
                                    {
                                        "chunk": f"{source_id}#0",
                                        "quote": "Grounded paragraph cites",
                                    }
                                ],
                            }
                        ],
                        ensure_ascii=False,
                    ),
                    "---",
                    "",
                    f"# Draft Title\n\nGrounded paragraph cites {source_id}.",
                ]
            )
            draft_path = write_raw_draft(root, content)

            issues = commands.validate_draft_file(root, draft_path)

            self.assertEqual([], issues)

    def test_rejects_malformed_context_source_and_chunk_scalars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    context_sources={"source_id": source_id},
                    context_chunks={"chunk": f"{source_id}#0"},
                ),
                valid_body(source_id),
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("invalid-context-source", issue_types(issues))
            self.assertIn("invalid-context-chunk", issue_types(issues))

    def test_rejects_empty_or_malformed_claims(self):
        bad_claims = [
            [],
            [{"claim_id": "claim-1"}],
            [{"claim_id": "claim-1", "paragraph": 1, "text": "", "evidence": []}],
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))

            for claims in bad_claims:
                with self.subTest(claims=claims):
                    draft_path = write_draft(
                        root,
                        metadata_for(source_id, claims=claims),
                        valid_body(source_id),
                    )

                    issues = commands.validate_draft_file(root, draft_path)

                    self.assertIn("invalid-claims", issue_types(issues))
                    draft_path.unlink()

    def test_rejects_cited_paragraph_without_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            body = (
                f"Alpha beta evidence {source_id}.\n\n"
                f"Gamma delta evidence {source_id}."
            )
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[claim_for(source_id, "Alpha beta evidence")],
                ),
                body,
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("paragraph-without-claim", issue_types(issues))

    def test_rejects_unclaimed_factual_statement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[claim_for(source_id, "Alpha beta evidence")],
                ),
                f"Alpha beta evidence {source_id}. Gamma delta evidence {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("unclaimed-statement", issue_types(issues))

    def test_rejects_claim_quote_absent_from_chunk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "Alpha beta evidence",
                            quote="Missing quote text",
                        )
                    ],
                ),
                f"Alpha beta evidence {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("claim-quote-not-in-chunk", issue_types(issues))

    def test_rejects_whitespace_mutated_claim_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "Alpha beta evidence",
                            quote="Alpha\nbeta evidence",
                        )
                    ],
                ),
                f"Alpha beta evidence {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("claim-quote-not-in-chunk", issue_types(issues))

    def test_rejects_unmatched_heading_statement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"# Unclaimed heading fact\n\nGrounded paragraph cites {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("unsupported-draft-heading", issue_types(issues))

    def test_rejects_claim_quote_unrelated_to_claim_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "Alpha beta evidence",
                            quote="Unrelated quote exists",
                        )
                    ],
                ),
                f"Alpha beta evidence {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("claim-text-not-supported-by-quote", issue_types(issues))

    def test_rejects_claim_evidence_outside_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "Alpha beta evidence",
                            chunk="src-deadbeef0000#0",
                        )
                    ],
                ),
                f"Alpha beta evidence {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("claim-evidence-outside-context", issue_types(issues))

    def test_rejects_claim_source_not_cited_by_paragraph(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[claim_for(source_id, "Alpha beta evidence")],
                ),
                "Alpha beta evidence.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("claim-source-not-cited", issue_types(issues))

    def test_rejects_claim_text_not_present_in_paragraph(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[claim_for(source_id, "Alpha beta evidence")],
                ),
                f"Gamma delta evidence {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("claim-text-not-in-paragraph", issue_types(issues))

    def test_rejects_citation_to_existing_source_outside_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                valid_body(other_id),
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertTrue(
                any(
                    issue["type"] == "citation-outside-context"
                    and issue["source_id"] == other_id
                    for issue in issues
                )
            )

    def test_rejects_non_heading_paragraph_without_citation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"# Draft Title\n\nThis paragraph has no citation.\n\nThis cites {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("missing-paragraph-citation", issue_types(issues))

    def test_rejects_broken_wiki_link(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[Missing Page]].",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertTrue(
                any(
                    issue["type"] == "broken-wiki-link"
                    and issue["target"] == "Missing Page"
                    for issue in issues
                )
            )

    def test_rejects_traversal_wiki_link_even_if_outside_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            (root.parent / "escape.md").write_text(
                "Outside file must not satisfy wiki link.",
                encoding="utf-8",
            )
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[../../escape]].",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertTrue(
                any(
                    issue["type"] == "broken-wiki-link"
                    and issue["target"] == "../../escape"
                    for issue in issues
                )
            )

    def test_rejects_drafts_link_even_if_draft_file_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[_drafts/draft-title]].",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertTrue(
                any(
                    issue["type"] == "broken-wiki-link"
                    and issue["target"] == "_drafts/draft-title"
                    for issue in issues
                )
            )

    def test_rejects_current_target_standalone_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[Current Target]].",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertTrue(
                any(
                    issue["type"] == "broken-wiki-link"
                    and issue["target"] == "Current Target"
                    for issue in issues
                )
            )

    def test_accepts_current_target_only_with_explicit_safe_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                (
                    f"# Current Target\n\n"
                    f"Grounded paragraph cites {source_id} "
                    "and links [[Current Target]]."
                ),
            )

            issues = commands.validate_draft_file(
                root, draft_path, target="Current Target"
            )

            self.assertEqual([], issues)

    def test_rejects_unsafe_target_before_current_target_exception(self):
        unsafe_targets = [
            "../x",
            "C" + ":/absolute",
            "C" + ":drive-qualified",
            "_drafts/x",
            "raw/x",
            "sources/x",
            "meta/x",
            "db/x",
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            absolute_target = str(Path(tmpdir).resolve() / "absolute")
            unsafe_targets.append(absolute_target)

            for target in unsafe_targets:
                with self.subTest(target=target):
                    draft_path = write_draft(
                        root,
                        metadata_for(source_id),
                        f"Grounded paragraph cites {source_id} and links [[{target}]].",
                    )

                    issues = commands.validate_draft_file(root, draft_path, target=target)

                    self.assertIn("unsafe-target", issue_types(issues))

    def test_rejects_case_variant_reserved_roots_before_target_exception(self):
        unsafe_targets = ["_Drafts/x", "Raw/x", "Sources/x", "Meta/x", "DB/x"]
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))

            for target in unsafe_targets:
                with self.subTest(target=target):
                    draft_path = write_draft(
                        root,
                        metadata_for(source_id),
                        f"Grounded paragraph cites {source_id} and links [[{target}]].",
                    )

                    issues = commands.validate_draft_file(root, draft_path, target=target)

                    self.assertIn("unsafe-target", issue_types(issues))
                    self.assertIn("broken-wiki-link", issue_types(issues))

    def test_rejects_other_missing_wiki_links_even_with_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                (
                    f"Grounded paragraph cites {source_id} and links "
                    "[[Current Target]] plus [[Other Missing]]."
                ),
            )

            issues = commands.validate_draft_file(
                root, draft_path, target="Current Target"
            )

            self.assertTrue(
                any(
                    issue["type"] == "broken-wiki-link"
                    and issue["target"] == "Other Missing"
                    for issue in issues
                )
            )

    def test_rejects_secret_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and leaks {SECRET}.",
            )

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SECRET}):
                issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("secret-leak", issue_types(issues))

    def test_rejects_short_secret_leak(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and leaks {SHORT_SECRET}.",
            )

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SHORT_SECRET}):
                issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("secret-leak", issue_types(issues))

    def test_malformed_source_id_suffix_does_not_count_as_citation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Paragraph cites only malformed token {source_id}-fake.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertIn("missing-paragraph-citation", issue_types(issues))
            self.assertFalse(
                any(
                    issue["type"] == "citation-outside-context"
                    and issue.get("source_id") == source_id
                    for issue in issues
                )
            )

    def test_passes_valid_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            (root / "wiki" / "Existing Page.md").write_text(
                f"Existing page cites {source_id}.", encoding="utf-8"
            )
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    claims=[
                        claim_for(
                            source_id,
                            "First grounded paragraph cites",
                            claim_id="claim-1",
                            paragraph=1,
                        ),
                        claim_for(
                            source_id,
                            "Second grounded paragraph cites",
                            claim_id="claim-2",
                            paragraph=2,
                        ),
                    ],
                ),
                (
                    f"# Draft Title\n\n"
                    f"First grounded paragraph cites {source_id}.\n\n"
                    f"Second grounded paragraph cites {source_id}."
                ),
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertEqual([], issues)

    def test_valid_claim_manifest_passes_with_wiki_link_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            (root / "wiki" / "Existing Page.md").write_text(
                f"Existing page cites {source_id}.", encoding="utf-8"
            )
            draft_path = write_draft(
                root,
                metadata_for(
                    source_id,
                    title="Existing Page",
                    claims=[claim_for(source_id, "Alpha beta evidence")],
                ),
                f"# [[Existing Page]]\n\nAlpha beta evidence {source_id}.",
            )

            issues = commands.validate_draft_file(root, draft_path)

            self.assertEqual([], issues)

    def test_cli_outputs_valid_message_and_zero_for_valid_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(root, metadata_for(source_id), valid_body(source_id))

            completed = run_validate_cli(root, draft_path)

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("Draft valid\n", completed.stdout)
            self.assertEqual("", completed.stderr)

    def test_cli_accepts_root_relative_draft_path_from_outside_root_cwd(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            write_draft(root, metadata_for(source_id), valid_body(source_id))

            completed = run_validate_cli(
                root, Path("wiki") / "_drafts" / "draft-title.md"
            )

            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("Draft valid\n", completed.stdout)
            self.assertEqual("", completed.stderr)

    def test_cli_outputs_issues_and_one_for_invalid_draft(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                "# Draft Title\n\nThis paragraph has no citation.",
            )

            completed = run_validate_cli(root, draft_path)

            self.assertEqual(1, completed.returncode)
            self.assertIn("missing-paragraph-citation", completed.stdout)
            self.assertEqual("", completed.stderr)

    def test_cli_runtime_error_outputs_one_stderr_line(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            missing_draft = root / "wiki" / "_drafts" / "missing.md"

            completed = run_validate_cli(root, missing_draft)

            self.assertEqual(1, completed.returncode)
            self.assertEqual("", completed.stdout)
            self.assertRegex(completed.stderr, r"^error: .+\n$")
            self.assertEqual(1, len(completed.stderr.splitlines()))
            self.assertNotIn("Traceback", completed.stderr)

    def test_cli_issue_output_redacts_secret_in_wiki_link_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[{SECRET}]].",
            )

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SECRET}):
                completed = run_validate_cli(root, draft_path)

            self.assertEqual(1, completed.returncode)
            self.assertIn("broken-wiki-link", completed.stdout)
            self.assertIn("secret-leak", completed.stdout)
            self.assertNotIn(SECRET, completed.stdout)
            self.assertNotIn(SECRET, completed.stderr)

    def test_cli_issue_output_redacts_short_secret_in_wiki_link_target(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id, _other_id = create_root_with_sources(Path(tmpdir))
            draft_path = write_draft(
                root,
                metadata_for(source_id),
                f"Grounded paragraph cites {source_id} and links [[{SHORT_SECRET}]].",
            )

            with mock.patch.dict(os.environ, {"KB_LLM_API_KEY": SHORT_SECRET}):
                completed = run_validate_cli(root, draft_path)

            self.assertEqual(1, completed.returncode)
            self.assertIn("broken-wiki-link", completed.stdout)
            self.assertIn("secret-leak", completed.stdout)
            self.assertNotIn(SHORT_SECRET, completed.stdout)
            self.assertNotIn(SHORT_SECRET, completed.stderr)


if __name__ == "__main__":
    unittest.main()
