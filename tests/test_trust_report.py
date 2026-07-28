import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import kb.commands as commands
from kb.cli import main
from kb.commands import ingest_file, init_repository, review_source
from kb.paths import KnowledgeBasePaths


def tree_snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


def claim_for(source_id: str, text: str, quote: str | None = None) -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "paragraph": 1,
        "text": text,
        "evidence": [{"chunk": f"{source_id}#0", "quote": quote or text}],
    }


def metadata_for(source_id: str, claim_text: str) -> dict[str, object]:
    return {
        "draft_id": "draft-trust",
        "title": "Trust Page",
        "query": "trust report query should not be serialized",
        "created_at": "2026-07-09T00:00:00Z",
        "model": "fake-local-model",
        "prompt_hash": "b" * 64,
        "context_sources": [source_id],
        "context_chunks": [f"{source_id}#0"],
        "claims": [claim_for(source_id, claim_text)],
    }


def write_draft(root: Path, metadata: dict[str, object], body: str) -> Path:
    draft = root / "wiki" / "_drafts" / "trust-page.md"
    draft.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    lines.extend(
        f"{key}: {json.dumps(value, ensure_ascii=False)}"
        for key, value in metadata.items()
    )
    lines.extend(["---", "", body.rstrip(), ""])
    draft.write_text("\n".join(lines), encoding="utf-8")
    return draft


def create_root_with_source(base: Path, text: str) -> tuple[Path, str]:
    root = base / "kb"
    init_repository(root)
    source = base / "source.md"
    source.write_text(f"# Source\n\n{text}", encoding="utf-8")
    source_id = ingest_file(root, source)["source_id"]
    return root, source_id


def create_root_with_named_source(
    base: Path, filename: str, text: str
) -> tuple[Path, str]:
    root = base / "kb"
    init_repository(root)
    source = base / filename
    source.write_text(f"# Source\n\n{text}", encoding="utf-8")
    source_id = ingest_file(root, source)["source_id"]
    return root, source_id


def set_source_card_fields(root: Path, source_id: str, fields: dict[str, str]) -> None:
    source_card = root / "sources" / f"{source_id}.md"
    lines = source_card.read_text(encoding="utf-8").splitlines()
    for field, value in fields.items():
        replacement = f"{field}: {value}"
        for index, line in enumerate(lines):
            if line.startswith(f"{field}:"):
                lines[index] = replacement
                break
        else:
            lines.insert(1, replacement)
    source_card.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TrustReportTests(unittest.TestCase):
    def test_report_surfaces_stable_draft_source_governance_and_audit_without_writes(self):
        from kb.trust_report import trust_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "Grounded paragraph cites."
            )
            set_source_card_fields(root, source_id, {"privacy": "public"})
            review_source(root, source_id, status="reviewed", reviewer="tester")
            body = f"# Trust Page\n\nGrounded paragraph cites {source_id}."
            draft = write_draft(root, metadata_for(source_id, "Grounded paragraph cites"), body)
            publish_result = commands.publish_draft(root, draft, "Trust Page")
            self.assertEqual([], publish_result["issues"])

            before = tree_snapshot(root)
            report = trust_report(root)
            after = tree_snapshot(root)

        self.assertEqual(before, after)
        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertEqual("trust-report-v1", report["schema_version"])
        self.assertIn(report["status"], {"pass", "warning"})
        self.assertEqual(str(root.resolve()), report["root"]["path"])
        self.assertIn("AI/LLM output is never a fact source", report["root"]["trust_principles"])

        sources = {source["source_id"]: source for source in report["sources"]}
        self.assertEqual("pass", sources[source_id]["review"]["status"])
        self.assertEqual("pass", sources[source_id]["raw_integrity"]["status"])

        stable_pages = {page["path"]: page for page in report["stable_wiki"]}
        stable = stable_pages["wiki/trust-page.md"]
        self.assertEqual("stable", stable["kind"])
        self.assertEqual("pass", stable["citation_status"])
        self.assertEqual([source_id], stable["source_ids"])
        self.assertEqual("Grounded paragraph cites", stable["quote_support"][0]["quote"])
        self.assertEqual(f"{source_id}#0", stable["quote_support"][0]["chunk"])

        drafts = {draft_report["path"]: draft_report for draft_report in report["drafts"]}
        draft_report = drafts["wiki/_drafts/trust-page.md"]
        self.assertEqual("draft", draft_report["kind"])
        self.assertEqual("pass", draft_report["validation_status"])
        self.assertEqual([source_id], draft_report["context_sources"])

        readiness = {item["path"]: item for item in report["publish_readiness"]}
        self.assertEqual("ready", readiness["wiki/_drafts/trust-page.md"]["classification"])

        self.assertEqual("meta/log.md", report["audit"]["log_path"])
        self.assertTrue(
            any("[publish-draft]" in entry for entry in report["audit"]["recent_entries"])
        )
        self.assertIn("governance", report)
        self.assertIn("residual_risks", report)
        self.assertNotIn("trust report query should not be serialized", payload)

    def test_missing_evidence_and_private_source_quotes_are_classified_and_redacted(self):
        from kb.trust_report import trust_report

        private_sentinel = "".join(["s", "k", "-", "trust", "-private", "-", "0" * 12])
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), f"Private quote {private_sentinel}."
            )
            source_card = root / "sources" / f"{source_id}.md"
            source_card.write_text(
                source_card.read_text(encoding="utf-8").replace(
                    "---\n", "---\nprivacy: sensitive\n", 1
                ),
                encoding="utf-8",
            )
            (root / "wiki" / "private.md").write_text(
                f"# Private\n\nPrivate quote {private_sentinel} {source_id}.",
                encoding="utf-8",
            )
            (root / "wiki" / "unsupported.md").write_text(
                "# Unsupported\n\nThis stable claim has no local citation.",
                encoding="utf-8",
            )

            report = trust_report(root)

        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertEqual("failed", report["status"])
        self.assertNotIn(private_sentinel, payload)
        pages = {page["path"]: page for page in report["stable_wiki"]}
        self.assertEqual("[redacted-private-quote]", pages["wiki/private.md"]["quote_support"][0]["quote"])
        unsupported_issues = {
            issue["type"] for issue in pages["wiki/unsupported.md"]["issues"]
        }
        self.assertIn("missing-citation", unsupported_issues)
        self.assertTrue(any(risk["classification"] == "blocking_evidence_gap" for risk in report["residual_risks"]))

    def test_personal_source_title_and_quotes_are_redacted(self):
        from kb.trust_report import trust_report

        personal_sentinel = "".join(["s", "k", "-", "trust", "-personal", "-", "1" * 12])
        private_title = "Synthetic personal source title"
        private_filename = "synthetic-private-source-file.md"
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_named_source(
                Path(tmpdir), private_filename, f"Personal quote {personal_sentinel}."
            )
            set_source_card_fields(
                root,
                source_id,
                {
                    "privacy": "personal",
                    "title": f"{private_title} {personal_sentinel}",
                    "review_status": "reviewed",
                    "reviewed_at": "2026-07-09T00:00:00Z",
                },
            )
            (root / "wiki" / "personal.md").write_text(
                f"# Personal\n\nPersonal quote {personal_sentinel} {source_id}.",
                encoding="utf-8",
            )

            report = trust_report(root)

        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(personal_sentinel, payload)
        self.assertNotIn(private_title, payload)
        self.assertNotIn(private_filename, payload)
        sources = {source["source_id"]: source for source in report["sources"]}
        self.assertEqual("[redacted-private-title]", sources[source_id]["title"])
        self.assertEqual("[redacted-private-path]", sources[source_id]["raw_integrity"]["raw_path"])
        pages = {page["path"]: page for page in report["stable_wiki"]}
        self.assertEqual("[redacted-private-quote]", pages["wiki/personal.md"]["quote_support"][0]["quote"])

    def test_missing_privacy_source_metadata_is_not_displayed(self):
        from kb.trust_report import trust_report

        missing_privacy_sentinel = "".join(
            ["s", "k", "-", "trust", "-missing-privacy", "-", "2" * 12]
        )
        private_title = "Synthetic missing privacy title"
        private_filename = "missing-privacy-private-filename.md"
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_named_source(
                Path(tmpdir),
                private_filename,
                f"Missing privacy quote {missing_privacy_sentinel}.",
            )
            set_source_card_fields(
                root,
                source_id,
                {
                    "title": f"{private_title} {missing_privacy_sentinel}",
                    "review_status": "reviewed",
                    "reviewed_at": "2026-07-09T00:00:00Z",
                },
            )
            (root / "wiki" / "missing-privacy.md").write_text(
                f"# Missing Privacy\n\nMissing privacy quote {missing_privacy_sentinel} {source_id}.",
                encoding="utf-8",
            )

            report = trust_report(root)

        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(missing_privacy_sentinel, payload)
        self.assertNotIn(private_title, payload)
        self.assertNotIn(private_filename, payload)
        sources = {source["source_id"]: source for source in report["sources"]}
        self.assertEqual("unspecified", sources[source_id]["privacy"])
        self.assertEqual("[redacted-private-title]", sources[source_id]["title"])
        self.assertEqual("[redacted-private-path]", sources[source_id]["raw_integrity"]["raw_path"])
        pages = {page["path"]: page for page in report["stable_wiki"]}
        self.assertEqual(
            "[redacted-private-quote]",
            pages["wiki/missing-privacy.md"]["quote_support"][0]["quote"],
        )

    def test_unknown_privacy_metadata_is_not_echoed(self):
        from kb.trust_report import trust_report

        privacy_sentinel = "".join(
            ["s", "k", "-", "privacy", "-field", "-", "3" * 12]
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), f"Unknown privacy quote {privacy_sentinel}."
            )
            set_source_card_fields(
                root,
                source_id,
                {
                    "privacy": privacy_sentinel,
                    "title": f"Synthetic unknown privacy title {privacy_sentinel}",
                    "review_status": "reviewed",
                    "reviewed_at": "2026-07-09T00:00:00Z",
                },
            )
            (root / "wiki" / "unknown-privacy.md").write_text(
                f"# Unknown Privacy\n\nUnknown privacy quote {privacy_sentinel} {source_id}.",
                encoding="utf-8",
            )

            report = trust_report(root)

        payload = json.dumps(report, ensure_ascii=False, sort_keys=True)
        self.assertNotIn(privacy_sentinel, payload)
        sources = {source["source_id"]: source for source in report["sources"]}
        self.assertEqual("unknown-non-public", sources[source_id]["privacy"])
        self.assertEqual("[redacted-private-title]", sources[source_id]["title"])
        pages = {page["path"]: page for page in report["stable_wiki"]}
        self.assertEqual(
            "[redacted-private-quote]",
            pages["wiki/unknown-privacy.md"]["quote_support"][0]["quote"],
        )

    def test_unconfirmed_stable_quote_support_is_blocking_residual_risk(self):
        from kb.trust_report import trust_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "The source supports only this sentence."
            )
            review_source(root, source_id, status="reviewed", reviewer="tester")
            (root / "wiki" / "unsupported-quote.md").write_text(
                f"# Unsupported Quote\n\nThis different stable claim cites {source_id}.",
                encoding="utf-8",
            )

            report = trust_report(root)

        self.assertEqual("failed", report["status"])
        self.assertEqual("trust_report_failed", report["classification"])
        pages = {page["path"]: page for page in report["stable_wiki"]}
        issue_types = {issue["type"] for issue in pages["wiki/unsupported-quote.md"]["issues"]}
        self.assertIn("quote-support-unconfirmed", issue_types)
        risks = {
            (risk["classification"], risk["issue_type"])
            for risk in report["residual_risks"]
        }
        self.assertIn(("blocking_evidence_gap", "quote-support-unconfirmed"), risks)

    def test_invalid_source_id_and_source_review_states_are_classified(self):
        from kb.trust_report import trust_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "Source review state is local."
            )
            (root / "wiki" / "invalid-source.md").write_text(
                "# Invalid\n\nThis stable claim cites src-deadbeef0000.",
                encoding="utf-8",
            )
            unreviewed_report = trust_report(root)
            review_source(root, source_id, status="needs_reingest", reviewer="tester")
            stale_report = trust_report(root)

        sources = {source["source_id"]: source for source in unreviewed_report["sources"]}
        self.assertEqual("warning", sources[source_id]["review"]["status"])
        invalid_page = {
            page["path"]: page for page in unreviewed_report["stable_wiki"]
        }["wiki/invalid-source.md"]
        self.assertIn(
            "invalid-source-reference",
            {issue["type"] for issue in invalid_page["issues"]},
        )
        self.assertEqual("failed", stale_report["status"])
        stale_sources = {source["source_id"]: source for source in stale_report["sources"]}
        self.assertEqual("failed", stale_sources[source_id]["review"]["status"])
        self.assertTrue(
            any(
                risk["classification"] == "source_review_blocker"
                for risk in stale_report["residual_risks"]
            )
        )

    def test_draft_empty_context_is_reported_as_publish_blocker(self):
        from kb.trust_report import trust_report

        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "Draft context should be explicit."
            )
            metadata = metadata_for(source_id, "Draft context should be explicit")
            metadata["context_sources"] = []
            metadata["context_chunks"] = []
            draft = write_draft(
                root,
                metadata,
                f"# Empty Context\n\nDraft context should be explicit {source_id}.",
            )

            report = trust_report(root)

        drafts = {draft_report["path"]: draft_report for draft_report in report["drafts"]}
        draft_label = str(draft.relative_to(root)).replace("\\", "/")
        draft_report = drafts[draft_label]
        self.assertEqual(str(draft.relative_to(root)).replace("\\", "/"), draft_report["path"])
        self.assertEqual("failed", draft_report["validation_status"])
        issue_types = {issue["type"] for issue in draft_report["issues"]}
        self.assertIn("missing-draft-field", issue_types)
        readiness = {item["path"]: item for item in report["publish_readiness"]}
        self.assertEqual("blocked", readiness[draft_report["path"]]["classification"])

    def test_cli_trust_report_json_does_not_require_or_call_provider_configuration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "CLI trust evidence is local."
            )
            review_source(root, source_id, status="reviewed", reviewer="tester")
            (root / "wiki" / "cli-trust.md").write_text(
                f"# CLI Trust\n\nCLI trust evidence is local {source_id}.",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            sentinel = "".join(["s", "k", "-", "trust", "-cli", "-", "0" * 12])

            with mock.patch.dict(
                os.environ,
                {
                    "KB_LLM_API_KEY": sentinel,
                    "KB_EMBEDDING_API_KEY": sentinel,
                },
                clear=False,
            ), mock.patch("kb.commands.OpenAICompatibleClient") as client_mock, redirect_stdout(
                stdout
            ), redirect_stderr(stderr):
                client_mock.side_effect = AssertionError("provider must not be called")
                code = main(["trust-report", "--root", str(root), "--json"])

        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual("", stderr.getvalue())
        data = json.loads(stdout.getvalue())
        self.assertEqual("trust-report-v1", data["schema_version"])
        self.assertNotIn(sentinel, stdout.getvalue())
        client_mock.assert_not_called()

    def test_cli_trust_report_summary_mode_is_redacted_and_does_not_traceback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root, source_id = create_root_with_source(
                Path(tmpdir), "Summary trust evidence is local."
            )
            review_source(root, source_id, status="reviewed", reviewer="tester")
            (root / "wiki" / "summary-trust.md").write_text(
                f"# Summary Trust\n\nSummary trust evidence is local {source_id}.",
                encoding="utf-8",
            )
            stdout = io.StringIO()
            stderr = io.StringIO()

            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["trust-report", "--root", str(root)])

        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual("", stderr.getvalue())
        self.assertIn("trust_report", stdout.getvalue())
        self.assertIn("stable_pages=", stdout.getvalue())
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())

    def test_product_and_web_console_expose_descriptor_without_browser_mutation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)

            from kb.product_console import product_console_state
            from kb.web_console import web_console_state

            product_state = product_console_state(root)
            actions = {action["id"]: action for action in product_state["actions"]}
            action = actions["inspect-trust-report"]
            self.assertEqual("kb_command", action["transport"])
            self.assertEqual("trust-report", action["command"])
            self.assertFalse(action["requires_confirmation"])
            self.assertFalse(action["executes"])

            browser_state = web_console_state(product_state, host="127.0.0.1", port=0)
            browser_actions = {
                action["id"]: action for action in browser_state["actions"]
            }
            self.assertIn("inspect-trust-report", browser_actions)
            self.assertFalse(browser_actions["inspect-trust-report"]["executes"])
            self.assertTrue(all(action.get("executes") is False for action in browser_actions.values()))


if __name__ == "__main__":
    unittest.main()
