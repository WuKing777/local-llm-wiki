import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from kb.cli import main
from kb.commands import init_repository
from kb.memory_candidates import capture, review


SECRET = "sk" + "-SENTINELCHECK1234567890abcdef"


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            code = main(argv)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def _snapshot(root: Path) -> list[tuple[str, str, bytes | None]]:
    if not root.exists():
        return []
    snapshot: list[tuple[str, str, bytes | None]] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_dir():
            snapshot.append((relative, "dir", None))
        elif path.is_symlink():
            snapshot.append((relative, "symlink", os.readlink(path).encode()))
        else:
            snapshot.append((relative, "file", path.read_bytes()))
    return snapshot


def _write_json(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")


def _symlink_or_skip(testcase: unittest.TestCase, target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:
        testcase.skipTest(f"symlink unavailable: {exc}")


class ExobrainCheckTests(unittest.TestCase):
    def test_initialized_empty_root_returns_read_only_report(self):
        from kb.exobrain_check import exobrain_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            before = _snapshot(root)

            report = exobrain_check(root)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(before, _snapshot(root))
            self.assertEqual(1, report["schema_version"])
            self.assertEqual("<root>", report["root"])
            self.assertNotIn(str(root.resolve()), payload)
            self.assertEqual("pass", report["status"])
            self.assertEqual("ready", report["classification"])
            self.assertEqual(0, report["counts"]["source_cards"])
            self.assertEqual(0, report["counts"]["raw_files"])
            self.assertEqual(0, report["counts"]["stable_wiki_pages"])
            self.assertEqual(0, report["counts"]["drafts"])
            self.assertEqual(0, report["counts"]["pending_memory_candidates"])
            self.assertTrue(
                any(action["id"] == "benchmark-add" for action in report["next_actions"])
            )
            notices = " ".join(report["notices"])
            self.assertIn("AI is not a fact source", notices)
            self.assertIn("Stable content requires source review", notices)
            self.assertIn("Memory candidates require confirmation", notices)

    def test_rich_root_counts_and_next_actions_are_redacted(self):
        from kb.exobrain_check import exobrain_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            pending = capture(
                root,
                type="preference",
                text="Never leak this pending body.",
                event_date="2026-07-01",
                privacy="personal",
                confidence="confirmed",
                value_reason="Never leak this review reason.",
                suggested_source_type="self_statement",
            )
            approved = capture(
                root,
                type="preference",
                text="Never leak this approved body.",
                event_date="2026-07-02",
                privacy="personal",
                confidence="confirmed",
                value_reason="Never leak this approved reason.",
                suggested_source_type="self_statement",
            )
            review(root, approved["id"], status="approved")
            drafts = root / "wiki" / "_drafts"
            drafts.mkdir(parents=True)
            (drafts / "secret-draft.md").write_text(
                f"# Secret Draft\n\nDo not leak {SECRET} or body text.\n",
                encoding="utf-8",
            )
            _write_json(
                root / "meta" / "topic-suggestions" / "topic-1111111111111111.json",
                {
                    "suggestion_id": "topic-1111111111111111",
                    "created_at": "2026-07-06T00:00:00Z",
                    "source_ids": [],
                    "suggested_pages": [{"title": f"Do not leak {SECRET}"}],
                    "next_actions": ["Do not leak topic text"],
                },
            )
            (root / "meta" / "review-queue.md").write_text(
                f"# Review Queue\n\n- [ ] Do not leak queue text {SECRET}\n",
                encoding="utf-8",
            )
            benchmark = root / "meta" / "evals" / "retrieval-benchmark.jsonl"
            benchmark.parent.mkdir(parents=True)
            benchmark.write_text(
                json.dumps(
                    {
                        "query": f"Do not leak query {SECRET}",
                        "expected_source_ids": ["src-000000000000"],
                        "privacy": "public",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            before = _snapshot(root)

            report = exobrain_check(root)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(before, _snapshot(root))
            self.assertEqual(1, report["counts"]["pending_memory_candidates"])
            self.assertEqual(1, report["counts"]["approved_memory_candidates"])
            self.assertEqual(1, report["counts"]["drafts"])
            self.assertEqual(1, report["counts"]["topic_suggestions"])
            self.assertEqual(1, report["counts"]["review_queue_open_items"])
            self.assertEqual(1, report["counts"]["retrieval_benchmark_records"])
            action_ids = [action["id"] for action in report["next_actions"]]
            self.assertIn("review-candidate", action_ids)
            self.assertIn("publish-memory", action_ids)
            self.assertIn("validate-draft", action_ids)
            self.assertIn("publish-draft", action_ids)
            self.assertIn("inspect-topic-suggestions", action_ids)
            self.assertTrue(
                next(
                    action
                    for action in report["next_actions"]
                    if action["id"] == "publish-memory"
                )["requires_confirmation"]
            )
            self.assertNotIn("Never leak", payload)
            self.assertNotIn("Do not leak", payload)
            self.assertNotIn(SECRET, payload)
            self.assertNotIn("query", payload.casefold())

    def test_uninitialized_root_cli_exits_one_and_does_not_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "missing"
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(["exobrain-check", "--root", str(root), "--json"])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("error:", stderr)
            self.assertNotIn("Traceback", stderr)
            self.assertEqual(before, _snapshot(root))

    def test_secret_shaped_root_path_is_redacted(self):
        from kb.exobrain_check import exobrain_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / SECRET / "kb"
            init_repository(root)

            report = exobrain_check(root)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertNotIn(SECRET, payload)
            self.assertEqual("<root>", report["root"])

    def test_damaged_candidate_json_is_counted_as_advisory_without_traceback(self):
        from kb.exobrain_check import exobrain_check

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            candidate_dir = root / "meta" / "memory-candidates"
            candidate_dir.mkdir()
            (candidate_dir / "mem-1111111111111111.json").write_text("{bad json", encoding="utf-8")
            before = _snapshot(root)

            report = exobrain_check(root)

            self.assertEqual(before, _snapshot(root))
            self.assertEqual(1, report["counts"]["damaged_memory_candidates"])
            self.assertGreaterEqual(report["counts"]["governance_advisory"], 1)
            self.assertIn(
                "repair-damaged-candidates",
                [action["id"] for action in report["next_actions"]],
            )

    def test_symlinked_candidate_and_topic_directories_are_not_followed(self):
        from kb.exobrain_check import exobrain_check

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            init_repository(root)
            outside_candidates = base / "outside-candidates"
            outside_topics = base / "outside-topics"
            outside_candidates.mkdir()
            outside_topics.mkdir()
            _write_json(
                outside_candidates / "mem-1111111111111111.json",
                {
                    "id": "mem-1111111111111111",
                    "type": "preference",
                    "text": "External candidate must not be read.",
                    "original_text": "External candidate must not be read.",
                    "event_date": "2026-07-01",
                    "privacy": "personal",
                    "confidence": "confirmed",
                    "needs_confirmation": True,
                    "value_reason": "Outside sentinel should stay outside.",
                    "suggested_source_type": "self_statement",
                    "created_at": "2026-07-01T00:00:00",
                    "status": "pending",
                },
            )
            _write_json(
                outside_topics / "topic-1111111111111111.json",
                {"suggestion_id": "topic-1111111111111111", "secret": SECRET},
            )
            _symlink_or_skip(
                self,
                outside_candidates,
                root / "meta" / "memory-candidates",
                directory=True,
            )
            _symlink_or_skip(
                self,
                outside_topics,
                root / "meta" / "topic-suggestions",
                directory=True,
            )

            report = exobrain_check(root)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(0, report["counts"]["pending_memory_candidates"])
            self.assertEqual(0, report["counts"]["topic_suggestions"])
            self.assertNotIn("External candidate", payload)
            self.assertNotIn(SECRET, payload)

    def test_symlinked_candidate_and_topic_files_are_not_followed(self):
        from kb.exobrain_check import exobrain_check

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            init_repository(root)
            external_candidate = base / "external-candidate.json"
            external_topic = base / "external-topic.json"
            _write_json(
                external_candidate,
                {
                    "id": "mem-2222222222222222",
                    "type": "preference",
                    "text": "External candidate file must not be read.",
                    "original_text": "External candidate file must not be read.",
                    "event_date": "2026-07-01",
                    "privacy": "personal",
                    "confidence": "confirmed",
                    "needs_confirmation": True,
                    "value_reason": "Outside file should stay outside.",
                    "suggested_source_type": "self_statement",
                    "created_at": "2026-07-01T00:00:00",
                    "status": "pending",
                },
            )
            _write_json(external_topic, {"suggestion_id": "topic-2222222222222222"})
            candidate_dir = root / "meta" / "memory-candidates"
            topic_dir = root / "meta" / "topic-suggestions"
            candidate_dir.mkdir()
            topic_dir.mkdir()
            _symlink_or_skip(self, external_candidate, candidate_dir / "mem-2222222222222222.json")
            _symlink_or_skip(self, external_topic, topic_dir / "topic-2222222222222222.json")

            report = exobrain_check(root)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(1, report["counts"]["damaged_memory_candidates"])
            self.assertEqual(0, report["counts"]["pending_memory_candidates"])
            self.assertEqual(0, report["counts"]["topic_suggestions"])
            self.assertNotIn("External candidate file", payload)

    def test_raw_and_benchmark_symlinks_are_not_followed_or_counted(self):
        from kb.exobrain_check import exobrain_check

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            init_repository(root)
            external_raw = base / "external-raw.md"
            external_benchmark = base / "external-benchmark.jsonl"
            external_evals = base / "external-evals"
            external_raw.write_text(f"external raw {SECRET}\n", encoding="utf-8")
            external_benchmark.write_text(
                json.dumps({"query": f"external query {SECRET}"}) + "\n",
                encoding="utf-8",
            )
            _symlink_or_skip(self, external_raw, root / "raw" / "external-raw.md")
            evals = root / "meta" / "evals"
            evals.mkdir()
            _symlink_or_skip(self, external_benchmark, evals / "retrieval-benchmark.jsonl")

            report = exobrain_check(root)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(0, report["counts"]["raw_files"])
            self.assertEqual(0, report["counts"]["retrieval_benchmark_records"])
            self.assertNotIn(SECRET, payload)

            (evals / "retrieval-benchmark.jsonl").unlink()
            evals.rmdir()
            external_evals.mkdir()
            (external_evals / "retrieval-benchmark.jsonl").write_text(
                json.dumps({"query": f"external dir query {SECRET}"}) + "\n",
                encoding="utf-8",
            )
            _symlink_or_skip(self, external_evals, evals, directory=True)

            report = exobrain_check(root)
            payload = json.dumps(report, ensure_ascii=False, sort_keys=True)

            self.assertEqual(0, report["counts"]["retrieval_benchmark_records"])
            self.assertNotIn(SECRET, payload)

    def test_source_card_symlink_is_rejected_without_leaking_external_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            root = base / "kb"
            init_repository(root)
            external_card = base / "external-source.md"
            external_card.write_text(
                "---\n"
                "source_id: src-111111111111\n"
                f"title: External source card {SECRET}\n"
                "raw_path: raw/missing.md\n"
                "sha256: 0\n"
                "imported_at: 2026-07-01T00:00:00\n"
                "kind: markdown\n"
                "---\n",
                encoding="utf-8",
            )
            _symlink_or_skip(
                self,
                external_card,
                root / "sources" / "src-111111111111.md",
            )

            code, stdout, stderr = _run_cli(["exobrain-check", "--root", str(root), "--json"])

            self.assertEqual(1, code)
            self.assertEqual("", stdout)
            self.assertIn("source card path is unsafe", stderr)
            self.assertNotIn("External source card", stderr)
            self.assertNotIn(SECRET, stderr)

    def test_cli_json_and_plain_are_parseable_redacted_and_read_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir) / "kb"
            init_repository(root)
            capture(
                root,
                type="preference",
                text="Plain output must not leak this candidate.",
                event_date="2026-07-01",
                privacy="personal",
                confidence="confirmed",
                value_reason="No leak.",
                suggested_source_type="self_statement",
            )
            before = _snapshot(root)

            code, stdout, stderr = _run_cli(["exobrain-check", "--root", str(root), "--json"])

            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            data = json.loads(stdout)
            self.assertEqual("pass", data["status"])
            self.assertEqual(before, _snapshot(root))
            self.assertNotIn("Plain output must not leak", stdout)

            code, stdout, stderr = _run_cli(["exobrain-check", "--root", str(root)])

            self.assertEqual(0, code, stderr)
            self.assertEqual("", stderr)
            self.assertIn("pending_memory_candidates: 1", stdout)
            self.assertNotIn("Plain output must not leak", stdout)
            self.assertEqual(before, _snapshot(root))


if __name__ == "__main__":
    unittest.main()
