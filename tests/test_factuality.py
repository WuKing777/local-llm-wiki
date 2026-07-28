import json
import tempfile
import unittest
from pathlib import Path

from kb.commands import ingest_file, init_repository
from kb.factuality import (
    ParsedDraftResponse,
    factual_statements,
    non_heading_paragraphs,
    parse_llm_draft_response,
    validate_claims,
)


def issue_types(issues: list[dict[str, str]]) -> set[str]:
    return {issue["type"] for issue in issues}


def create_root_with_source(temp: Path, text: str) -> tuple[Path, str]:
    root = temp / "kb"
    source = temp / "source.md"
    source.write_text(text, encoding="utf-8")
    init_repository(root)
    metadata = ingest_file(root, source)
    return root, metadata["source_id"]


def metadata_for(source_id: str, **overrides: object) -> dict[str, object]:
    metadata: dict[str, object] = {
        "context_sources": [source_id],
        "context_chunks": [f"{source_id}#0"],
    }
    metadata.update(overrides)
    return metadata


def valid_claim(source_id: str, text: str) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [
            {
                "chunk": f"{source_id}#0",
                "quote": text,
            }
        ],
    }


class ParseLLMDraftResponseTests(unittest.TestCase):
    def assert_invalid_response(self, content: str) -> None:
        with self.assertRaises(RuntimeError) as raised:
            parse_llm_draft_response(content)
        self.assertNotIn(content, str(raised.exception))

    def test_parse_success(self):
        claim = {
            "claim_id": "claim-1",
            "paragraph": 1,
            "text": "Grounded draft",
            "evidence": [
                {
                    "chunk": "src-abcdef123456#0",
                    "quote": "Grounded draft",
                }
            ],
        }
        content = json.dumps(
            {
                "body": "Grounded draft.",
                "claims": [claim],
            }
        )

        parsed = parse_llm_draft_response(content)

        self.assertEqual(
            ParsedDraftResponse(
                body="Grounded draft.",
                claims=[claim],
            ),
            parsed,
        )

    def test_invalid_json(self):
        self.assert_invalid_response("{not json")

    def test_raw_markdown(self):
        self.assert_invalid_response("# Raw markdown\n\nNot JSON.")

    def test_missing_body(self):
        self.assert_invalid_response(
            json.dumps(
                {
                    "claims": [
                        {
                            "claim_id": "claim-1",
                            "paragraph": 1,
                            "text": "Grounded draft",
                            "evidence": [
                                {
                                    "chunk": "src-abcdef123456#0",
                                    "quote": "Grounded draft",
                                }
                            ],
                        }
                    ]
                }
            )
        )

    def test_missing_claims(self):
        self.assert_invalid_response(json.dumps({"body": "Grounded draft."}))

    def test_empty_claims(self):
        self.assert_invalid_response(
            json.dumps({"body": "Grounded draft.", "claims": []})
        )

    def test_malformed_claim_record(self):
        invalid_claims = [
            ["not a dict"],
            [{"claim_id": "claim-1"}],
            [
                {
                    "claim_id": "claim-1",
                    "paragraph": "1",
                    "text": "Grounded draft",
                    "evidence": [
                        {
                            "chunk": "src-abcdef123456#0",
                            "quote": "Grounded draft",
                        }
                    ],
                }
            ],
            [
                {
                    "claim_id": "claim-1",
                    "paragraph": 1,
                    "text": "",
                    "evidence": [
                        {
                            "chunk": "src-abcdef123456#0",
                            "quote": "Grounded draft",
                        }
                    ],
                }
            ],
            [
                {
                    "claim_id": "claim-1",
                    "paragraph": 1,
                    "text": "Grounded draft",
                    "evidence": [],
                }
            ],
            [
                {
                    "claim_id": "claim-1",
                    "paragraph": 1,
                    "text": "Grounded draft",
                    "evidence": [{"chunk": "src-abcdef123456#0"}],
                }
            ],
        ]

        for claims in invalid_claims:
            with self.subTest(claims=claims):
                self.assert_invalid_response(
                    json.dumps({"body": "Grounded draft.", "claims": claims})
                )


class ParagraphStatementTests(unittest.TestCase):
    def test_non_heading_paragraphs_ignores_heading_only_blocks(self):
        body = "# Title\n\nBody one.\n\n## Heading\nBody two."

        self.assertEqual(["Body one.", "Body two."], non_heading_paragraphs(body))

    def test_factual_statements_cleans_markup_and_splits_on_newlines(self):
        source_id = "src-abcdef123456"
        paragraph = (
            f"**Alpha** [[Target|beta]] {source_id}\n"
            "_Gamma_ statement with [[Page]]."
        )

        statements = factual_statements(paragraph)

        self.assertEqual(
            ["Alpha beta", "Gamma statement with Page"],
            statements,
        )

    def test_factual_statements_ignore_wiki_link_targets(self):
        source_id = "src-abcdef123456"
        paragraph = f"Alpha beta {source_id} and links [[Self Page]]."

        statements = factual_statements(paragraph, {"Self Page"})

        self.assertEqual(["Alpha beta"], statements)

    def test_factual_statements_keep_non_ignored_wiki_link_labels(self):
        source_id = "src-abcdef123456"
        paragraph = f"Alpha beta {source_id} and links [[Other Page]]."

        statements = factual_statements(paragraph, {"Self Page"})

        self.assertEqual(["Alpha beta and links Other Page"], statements)


class ValidateClaimsTests(unittest.TestCase):
    def test_valid_claim_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            body = f"Alpha beta evidence {source_id}."

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                body,
            )

            self.assertEqual([], issues)

    def test_valid_claim_with_underscores_passes_unclaimed_statement_gate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            claim_text = "Protocol requires source_ids and self_statement records"
            root, source_id = create_root_with_source(
                Path(tmpdir), f"# Source\n\n{claim_text}."
            )
            body = f"{claim_text} {source_id}."

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, claim_text)]),
                body,
            )

            self.assertEqual([], issues)

    def test_matching_h1_title_passes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            body = f"# Draft Title\n\nAlpha beta evidence {source_id}."

            issues = validate_claims(
                root,
                metadata_for(
                    source_id,
                    title="Draft Title",
                    claims=[valid_claim(source_id, "Alpha beta evidence")],
                ),
                body,
            )

            self.assertEqual([], issues)

    def test_rejects_unmatched_heading_statement(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            body = f"# Unclaimed heading fact\n\nAlpha beta evidence {source_id}."

            issues = validate_claims(
                root,
                metadata_for(
                    source_id,
                    title="Draft Title",
                    claims=[valid_claim(source_id, "Alpha beta evidence")],
                ),
                body,
            )

            self.assertIn("unsupported-draft-heading", issue_types(issues))

    def test_rejects_second_heading_even_when_body_claims_are_valid(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            body = (
                f"# Draft Title\n\nAlpha beta evidence {source_id}.\n\n"
                "## Unclaimed section fact"
            )

            issues = validate_claims(
                root,
                metadata_for(
                    source_id,
                    title="Draft Title",
                    claims=[valid_claim(source_id, "Alpha beta evidence")],
                ),
                body,
            )

            self.assertIn("unsupported-draft-heading", issue_types(issues))

    def test_quote_not_in_chunk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["evidence"] = [
                {"chunk": f"{source_id}#0", "quote": "Missing quote text"}
            ]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-quote-not-in-chunk", issue_types(issues))

    def test_whitespace_mutated_quote_is_not_exact_chunk_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["evidence"] = [
                {"chunk": f"{source_id}#0", "quote": "Alpha\nbeta evidence"}
            ]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-quote-not-in-chunk", issue_types(issues))

    def test_claim_text_must_be_exact_substring_of_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha\nbeta evidence."
            )
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["evidence"] = [
                {"chunk": f"{source_id}#0", "quote": "Alpha\nbeta evidence"}
            ]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-text-not-supported-by-quote", issue_types(issues))

    def test_quote_with_markup_removed_is_not_exact_chunk_quote(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\n**Alpha** beta evidence."
            )

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-quote-not-in-chunk", issue_types(issues))

    def test_claim_text_must_match_quote_without_markup_stripping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\n**Alpha** beta evidence."
            )
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["evidence"] = [
                {"chunk": f"{source_id}#0", "quote": "**Alpha** beta evidence"}
            ]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-text-not-supported-by-quote", issue_types(issues))

    def test_claim_text_must_match_paragraph_without_markup_stripping(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            claim = valid_claim(source_id, "**Alpha** beta evidence")
            claim["evidence"] = [
                {"chunk": f"{source_id}#0", "quote": "Alpha beta evidence"}
            ]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-text-not-in-paragraph", issue_types(issues))
            self.assertIn("claim-text-not-supported-by-quote", issue_types(issues))

    def test_quote_exists_in_chunk_but_is_unrelated_to_claim_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir),
                "# Source\n\nAlpha beta evidence. Unrelated quote exists.",
            )
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["evidence"] = [
                {"chunk": f"{source_id}#0", "quote": "Unrelated quote exists"}
            ]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-text-not-supported-by-quote", issue_types(issues))

    def test_evidence_chunk_outside_context(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            outside = "src-deadbeef0000"
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["evidence"] = [
                {"chunk": f"{outside}#0", "quote": "Alpha beta evidence"}
            ]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-evidence-outside-context", issue_types(issues))

    def test_claim_source_not_cited(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                "Alpha beta evidence has no citation.",
            )

            self.assertIn("claim-source-not-cited", issue_types(issues))

    def test_paragraph_with_citation_but_no_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence. Gamma delta evidence."
            )
            body = (
                f"Alpha beta evidence {source_id}.\n\n"
                f"Gamma delta evidence {source_id}."
            )

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                body,
            )

            self.assertIn("paragraph-without-claim", issue_types(issues))

    def test_two_factual_statements_with_only_one_claim(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence. Gamma delta evidence."
            )
            body = (
                f"Alpha beta evidence {source_id}. "
                f"Gamma delta evidence {source_id}."
            )

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                body,
            )

            self.assertIn("unclaimed-statement", issue_types(issues))

    def test_claim_text_not_in_paragraph(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence. Gamma delta evidence."
            )

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                f"Gamma delta evidence {source_id}.",
            )

            self.assertIn("claim-text-not-in-paragraph", issue_types(issues))

    def test_duplicate_claim_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            first = valid_claim(source_id, "Alpha beta evidence")
            second = valid_claim(source_id, "Alpha beta evidence")

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[first, second]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("duplicate-claim-id", issue_types(issues))

    def test_claim_paragraph_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["paragraph"] = 2

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-paragraph-out-of-range", issue_types(issues))

    def test_invalid_claim_evidence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            claim = valid_claim(source_id, "Alpha beta evidence")
            claim["evidence"] = [{"chunk": f"{source_id}#0"}]

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("invalid-claim-evidence", issue_types(issues))

    def test_validation_issues_do_not_include_model_controlled_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            secret = "sek"
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            claim = {
                "claim_id": f"claim-{secret}",
                "paragraph": 1,
                "text": f"Gamma {secret}",
                "evidence": [{"chunk": f"{secret}#0", "quote": f"Delta {secret}"}],
            }

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[claim]),
                f"Alpha beta evidence {source_id}. Extra {secret}.",
            )
            issue_text = json.dumps(issues, sort_keys=True)

            self.assertNotIn(secret, issue_text)
            self.assertIn("claim-text-not-in-paragraph", issue_types(issues))
            self.assertIn("invalid-claim-evidence", issue_types(issues))

    def test_duplicate_claim_id_issue_does_not_leak_claim_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            secret = "s" + "k-" + "test-secret"
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            first = valid_claim(source_id, "Alpha beta evidence")
            second = valid_claim(source_id, "Alpha beta evidence")
            first["claim_id"] = secret
            second["claim_id"] = secret

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[first, second]),
                f"Alpha beta evidence {source_id}.",
            )
            issue_text = json.dumps(issues, sort_keys=True)

            self.assertFalse(secret in issue_text, "claim id leaked")
            self.assertIn("duplicate-claim-id", issue_types(issues))

    def test_missing_raw_source_or_chunk_is_missing_chunk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            for path in (root / "raw").rglob("*.md"):
                path.unlink()

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-evidence-missing-chunk", issue_types(issues))

    def test_tampered_raw_source_is_missing_chunk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )
            for path in (root / "raw").rglob("*.md"):
                path.write_text("# Source\n\nAlpha beta evidence. Tampered.", encoding="utf-8")

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[valid_claim(source_id, "Alpha beta evidence")]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("claim-evidence-missing-chunk", issue_types(issues))

    def test_invalid_claims(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "# Source\n\nAlpha beta evidence."
            )

            issues = validate_claims(
                root,
                metadata_for(source_id, claims=[]),
                f"Alpha beta evidence {source_id}.",
            )

            self.assertIn("invalid-claims", issue_types(issues))


if __name__ == "__main__":
    unittest.main()
