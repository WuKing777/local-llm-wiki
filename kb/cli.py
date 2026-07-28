import argparse
import json
import os
import sys

from .ocr_fixture import create_ocr_fixture
from .locks import WriteLockError
from .commands import (
    answer,
    backup_repository,
    benchmark_add,
    capture_candidate,
    compile_page,
    create_self_statement,
    daily_workflow,
    embedding_check,
    eval_search_repository,
    exobrain_check_repository,
    gateway_check_repository,
    govern,
    hybrid_search,
    ingest_derived,
    ingest_file,
    ingest_inbox,
    ingest_ocr,
    ingest_pdf,
    init_personal_exobrain,
    init_obsidian_vault,
    init_repository,
    lock_check_repository,
    llm_check,
    llm_contract_check,
    llm_draft,
    llm_preflight_repository,
    ocr_check,
    product_console_repository,
    lint_repository,
    publish_memory,
    publish_draft,
    profile_add,
    profile_list,
    repair_draft_file,
    rebuild_index,
    refresh_source,
    recover_lock_repository,
    review_candidate,
    review_source,
    migrate_check_repository,
    search,
    semantic_search,
    schema_check_repository,
    status_repository,
    suggest_topics,
    restore_repository,
    trust_report_repository,
    validate_draft_file,
    vector_rebuild,
)
from .personal_compile import personal_compile_request
from .doctor import doctor, format_doctor_summary
from .redaction import redact_text
from .web_console import DEFAULT_HOST, DEFAULT_PORT, is_loopback_host, serve_web_console


class RedactingArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        self.exit(2, f"{self.prog}: error: {_sanitize_error_message(message)}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = RedactingArgumentParser(prog="kb")
    subparsers = parser.add_subparsers(
        dest="command", required=True, parser_class=RedactingArgumentParser
    )

    init_parser = subparsers.add_parser("init", help="Initialize a knowledge base")
    init_parser.add_argument("--root", required=True, help="Knowledge base root directory")

    obsidian_parser = subparsers.add_parser(
        "obsidian-init", help="Initialize Obsidian vault files"
    )
    obsidian_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    exobrain_parser = subparsers.add_parser(
        "exobrain-init", help="Initialize a first-stage personal exobrain vault"
    )
    exobrain_parser.add_argument("--root", required=True, help="Personal exobrain root")

    self_statement_parser = subparsers.add_parser(
        "self-statement", help="Create an auditable self_statement source"
    )
    self_statement_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    self_statement_parser.add_argument("--text", required=True, help="Statement text")
    self_statement_parser.add_argument("--event-date", required=True, help="YYYY-MM-DD event date")
    self_statement_parser.add_argument(
        "--privacy",
        required=True,
        choices=("public", "personal", "sensitive", "restricted"),
    )
    self_statement_parser.add_argument(
        "--confidence",
        required=True,
        choices=("confirmed", "likely", "uncertain", "unknown"),
    )
    self_statement_parser.add_argument(
        "--input-method",
        required=True,
        choices=("chat", "obsidian", "manual_file", "voice_transcript", "imported_note"),
    )

    capture_candidate_parser = subparsers.add_parser(
        "capture-candidate", help="Capture a non-stable memory candidate"
    )
    capture_candidate_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    capture_candidate_parser.add_argument("--type", required=True, help="Candidate type")
    capture_candidate_parser.add_argument("--text", required=True, help="Candidate text")
    capture_candidate_parser.add_argument(
        "--event-date", required=True, help="YYYY-MM-DD event date"
    )
    capture_candidate_parser.add_argument(
        "--privacy",
        required=True,
        choices=("public", "personal", "sensitive", "restricted"),
    )
    capture_candidate_parser.add_argument(
        "--confidence",
        required=True,
        choices=("confirmed", "likely", "uncertain", "unknown"),
    )
    capture_candidate_parser.add_argument(
        "--value-reason", required=True, help="Why this memory is useful"
    )
    capture_candidate_parser.add_argument(
        "--suggested-source-type",
        required=True,
        choices=("self_statement",),
    )

    review_candidate_parser = subparsers.add_parser(
        "review-candidate", help="Approve or reject a memory candidate"
    )
    review_candidate_parser.add_argument("candidate_id", help="Candidate id")
    review_candidate_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    review_candidate_parser.add_argument(
        "--status", required=True, choices=("approved", "rejected")
    )

    publish_memory_parser = subparsers.add_parser(
        "publish-memory", help="Publish an approved memory candidate as a source"
    )
    publish_memory_parser.add_argument("candidate_id", help="Candidate id")
    publish_memory_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    publish_memory_parser.add_argument(
        "--confirm", action="store_true", help="Confirm source creation"
    )

    suggest_topics_parser = subparsers.add_parser(
        "suggest-topics", help="Create local topic suggestion metadata"
    )
    suggest_topics_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    suggest_topics_parser.add_argument(
        "--source-id",
        action="append",
        default=[],
        help="Limit suggestions to a source id; may be repeated",
    )
    suggest_topics_parser.add_argument(
        "--json", action="store_true", help="Print redacted JSON"
    )

    daily_workflow_parser = subparsers.add_parser(
        "daily-workflow", help="Create a redacted local daily workflow plan"
    )
    daily_workflow_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    daily_workflow_parser.add_argument("--date", help="Workflow date YYYY-MM-DD")
    daily_workflow_parser.add_argument(
        "--json", action="store_true", help="Print redacted JSON"
    )

    benchmark_add_parser = subparsers.add_parser(
        "benchmark-add", help="Append a safe retrieval benchmark case"
    )
    benchmark_add_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    benchmark_add_parser.add_argument("--query", required=True, help="Benchmark query")
    benchmark_add_parser.add_argument(
        "--expected-source-id",
        action="append",
        required=True,
        help="Expected source id; may be repeated",
    )
    benchmark_add_parser.add_argument(
        "--expected-wiki-path",
        action="append",
        default=[],
        help="Expected wiki path; may be repeated",
    )
    benchmark_add_parser.add_argument(
        "--expected-quote",
        action="append",
        default=[],
        help="Expected local quote for metric-only retrieval quality; may be repeated",
    )
    benchmark_add_parser.add_argument(
        "--privacy",
        default="public",
        choices=("public", "personal", "sensitive", "restricted"),
    )
    benchmark_add_parser.add_argument(
        "--confirm-private",
        action="store_true",
        help="Confirm sensitive or restricted benchmark sample",
    )
    benchmark_add_parser.add_argument(
        "--json", action="store_true", help="Print redacted JSON"
    )

    exobrain_check_parser = subparsers.add_parser(
        "exobrain-check", help="Report deterministic read-only exobrain status"
    )
    exobrain_check_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    exobrain_check_parser.add_argument(
        "--json", action="store_true", help="Print redacted JSON"
    )

    ingest_parser = subparsers.add_parser("ingest", help="Ingest a source file")
    ingest_parser.add_argument(
        "path", help="Path to a Markdown, text, HTML, BibTeX, or RIS file"
    )
    ingest_parser.add_argument("--root", required=True, help="Knowledge base root directory")

    ingest_singlefile_parser = subparsers.add_parser(
        "ingest-singlefile", help="Ingest a SingleFile HTML web capture"
    )
    ingest_singlefile_parser.add_argument("path", help="Path to a saved HTML file")
    ingest_singlefile_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    ingest_zotero_parser = subparsers.add_parser(
        "ingest-zotero", help="Ingest a Zotero BibTeX or RIS export"
    )
    ingest_zotero_parser.add_argument("path", help="Path to a .bib or .ris export")
    ingest_zotero_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    ingest_derived_parser = subparsers.add_parser(
        "ingest-derived", help="Ingest an original file with derived searchable text"
    )
    ingest_derived_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    ingest_derived_parser.add_argument(
        "--original", required=True, help="Original PDF/image/document file"
    )
    ingest_derived_parser.add_argument(
        "--text", required=True, help="Derived UTF-8 text, Markdown, or HTML file"
    )
    ingest_derived_parser.add_argument(
        "--workflow",
        required=True,
        choices=("ocr", "pdf-text", "pandoc"),
        help="Derived text workflow",
    )

    ingest_pdf_parser = subparsers.add_parser(
        "ingest-pdf", help="Extract and ingest a text-layer PDF"
    )
    ingest_pdf_parser.add_argument("path", help="Path to a text-layer PDF")
    ingest_pdf_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    ingest_ocr_parser = subparsers.add_parser(
        "ingest-ocr", help="Run local Tesseract OCR and ingest the derived text"
    )
    ingest_ocr_parser.add_argument("path", help="Path to an image or scanned PDF")
    ingest_ocr_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    ingest_ocr_parser.add_argument(
        "--lang", default="eng", help="Tesseract language code"
    )

    ingest_inbox_parser = subparsers.add_parser(
        "ingest-inbox", help="Ingest supported files from inbox"
    )
    ingest_inbox_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    index_parser = subparsers.add_parser("index", help="Rebuild the search index")
    index_parser.add_argument("--root", required=True, help="Knowledge base root directory")

    rebuild_parser = subparsers.add_parser(
        "rebuild-index", help="Rebuild the search index"
    )
    rebuild_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    search_parser = subparsers.add_parser("search", help="Search the knowledge base")
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    search_parser.add_argument("--limit", type=int, default=10, help="Maximum results")

    vector_parser = subparsers.add_parser(
        "vector-rebuild", help="Rebuild the semantic vector index"
    )
    vector_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    semantic_parser = subparsers.add_parser(
        "semantic-search", help="Search source chunks with embeddings"
    )
    semantic_parser.add_argument("query", help="Search query")
    semantic_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    semantic_parser.add_argument("--limit", type=int, default=10, help="Maximum results")

    hybrid_parser = subparsers.add_parser(
        "hybrid-search", help="Combine FTS and semantic search"
    )
    hybrid_parser.add_argument("query", help="Search query")
    hybrid_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    hybrid_parser.add_argument("--limit", type=int, default=10, help="Maximum results")

    eval_search_parser = subparsers.add_parser(
        "eval-search", help="Evaluate a retrieval benchmark"
    )
    eval_search_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    eval_search_parser.add_argument(
        "--benchmark",
        default="meta/evals/retrieval-benchmark.jsonl",
        help="Benchmark JSONL path, relative paths resolve under --root",
    )
    eval_search_parser.add_argument("--limit", type=int, default=10, help="Maximum results")
    eval_search_parser.add_argument(
        "--json", action="store_true", help="Print deterministic JSON"
    )

    answer_parser = subparsers.add_parser(
        "answer", help="Answer a question with local sources"
    )
    answer_parser.add_argument("question", help="Question to answer")
    answer_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    answer_parser.add_argument("--limit", type=int, default=5, help="Maximum evidence items")

    lint_parser = subparsers.add_parser("lint", help="Check wiki and source references")
    lint_parser.add_argument("--root", required=True, help="Knowledge base root directory")

    status_parser = subparsers.add_parser("status", help="Print repository counts")
    status_parser.add_argument("--root", required=True, help="Knowledge base root directory")

    govern_parser = subparsers.add_parser("govern", help="Write governance quality report")
    govern_parser.add_argument("--root", required=True, help="Knowledge base root directory")

    backup_parser = subparsers.add_parser(
        "backup", help="Create an allowlisted durable backup archive"
    )
    backup_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    backup_parser.add_argument("--output", required=True, help="Output backup zip path")
    backup_parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Allow dirty durable git entries and record them in the manifest",
    )

    restore_parser = subparsers.add_parser(
        "restore", help="Restore an allowlisted durable backup archive"
    )
    restore_parser.add_argument("--backup", required=True, help="Backup zip path")
    restore_parser.add_argument("--root", required=True, help="Restore target root")
    restore_parser.add_argument(
        "--replace",
        action="store_true",
        help="Replace a non-empty target root using a same-parent rollback",
    )

    migrate_check_parser = subparsers.add_parser(
        "migrate-check", help="Verify a restored root against a source root"
    )
    migrate_check_parser.add_argument("--source", required=True, help="Source root")
    migrate_check_parser.add_argument("--restored", required=True, help="Restored root")
    migrate_check_parser.add_argument(
        "--json", action="store_true", help="Print ProductResult JSON"
    )

    gateway_check_parser = subparsers.add_parser(
        "gateway-check", help="Check local policy gateway readiness"
    )
    gateway_check_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    gateway_check_parser.add_argument(
        "--json", action="store_true", help="Print ProductResult JSON"
    )

    product_console_parser = subparsers.add_parser(
        "product-console", help="Show the local product console state"
    )
    product_console_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    product_console_parser.add_argument(
        "--json", action="store_true", help="Print deterministic JSON"
    )

    trust_report_parser = subparsers.add_parser(
        "trust-report", help="Show deterministic local trust evidence"
    )
    trust_report_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    trust_report_parser.add_argument(
        "--json", action="store_true", help="Print deterministic JSON"
    )

    web_console_parser = subparsers.add_parser(
        "web-console", help="Start the local read-only web console"
    )
    web_console_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    web_console_parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help="Loopback host to bind; non-loopback hosts are rejected",
    )
    web_console_parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Local port, or 0 for any free port"
    )
    web_console_parser.add_argument(
        "--no-open", action="store_true", help="Do not open a browser automatically"
    )

    doctor_parser = subparsers.add_parser("doctor", help="Run read-only health checks")
    doctor_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    doctor_parser.add_argument(
        "--json", action="store_true", help="Print doctor report JSON"
    )
    doctor_parser.add_argument(
        "--online",
        action="store_true",
        help="Run explicit online provider probes when configured",
    )

    schema_check_parser = subparsers.add_parser(
        "schema-check", help="Validate schema contracts"
    )
    schema_check_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    schema_check_parser.add_argument(
        "--write-manifest",
        action="store_true",
        help="Write only meta/kb-manifest.json when it is missing",
    )
    schema_check_parser.add_argument(
        "--json", action="store_true", help="Print ProductResult JSON"
    )

    lock_check_parser = subparsers.add_parser(
        "lock-check", help="Check the root write lock"
    )
    lock_check_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    lock_check_parser.add_argument(
        "--json", action="store_true", help="Print ProductResult JSON"
    )

    recover_lock_parser = subparsers.add_parser(
        "recover-lock", help="Manually recover a stale or uncertain root write lock"
    )
    recover_lock_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    recover_lock_parser.add_argument(
        "--manual-confirm",
        action="store_true",
        help="Confirm manual recovery of stale or uncertain lock",
    )
    recover_lock_parser.add_argument(
        "--json", action="store_true", help="Print ProductResult JSON"
    )

    review_source_parser = subparsers.add_parser(
        "review-source", help="Record an auditable source review"
    )
    review_source_parser.add_argument("source_id", help="Source id to review")
    review_source_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    review_source_parser.add_argument(
        "--status",
        required=True,
        choices=("reviewed", "verified", "pass", "needs_reingest", "rejected"),
        help="Review status",
    )
    review_source_parser.add_argument("--reviewer", default="", help="Reviewer name")
    review_source_parser.add_argument("--note", default="", help="Short review note")

    refresh_source_parser = subparsers.add_parser(
        "refresh-source", help="Refresh a source card after explicit raw repair"
    )
    refresh_source_parser.add_argument("source_id", help="Source id to refresh")
    refresh_source_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )

    subparsers.add_parser("llm-check", help="Check local LLM configuration")
    contract_parser = subparsers.add_parser(
        "llm-contract-check", help="Classify a local LLM draft response envelope"
    )
    contract_parser.add_argument(
        "--response", help="Local file containing the LLM response"
    )
    contract_parser.add_argument("--root", help="Knowledge base root for live no-write check")
    contract_parser.add_argument("--query", help="Query for live no-write check")
    contract_parser.add_argument("--title", required=True, help="Expected draft title")
    contract_parser.add_argument("--target", help="Optional publish target title")
    contract_parser.add_argument(
        "--context-limit",
        type=int,
        default=5,
        help="Maximum source chunks for live no-write check",
    )
    subparsers.add_parser("embedding-check", help="Check embedding configuration")
    subparsers.add_parser("ocr-check", help="Check local OCR configuration")

    ocr_fixture_parser = subparsers.add_parser(
        "ocr-fixture", help="Create a local OCR smoke-test image"
    )
    ocr_fixture_parser.add_argument(
        "--output", required=True, help="Output image path"
    )
    ocr_fixture_parser.add_argument("--text", required=True, help="Text to render")

    draft_parser = subparsers.add_parser("llm-draft", help="Create an LLM draft")
    draft_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    draft_parser.add_argument("--query", required=True, help="Query used to retrieve context")
    draft_parser.add_argument("--title", required=True, help="Draft page title")

    preflight_parser = subparsers.add_parser(
        "llm-preflight", help="Run provider-agnostic LLM preflight"
    )
    preflight_parser.add_argument("--root", required=True, help="Knowledge base root directory")
    preflight_parser.add_argument("--query", required=True, help="Query used to retrieve context")
    preflight_parser.add_argument("--title", required=True, help="Draft page title")
    preflight_parser.add_argument("--target", help="Optional publish target title")
    preflight_parser.add_argument(
        "--context-limit",
        type=int,
        default=5,
        help="Maximum source chunks for preflight",
    )
    preflight_parser.add_argument(
        "--json", action="store_true", help="Print ProductResult JSON"
    )
    preflight_parser.add_argument(
        "--write-audit",
        action="store_true",
        help="Persist minimal allowlisted LLM audit metadata",
    )
    preflight_parser.add_argument(
        "--confirm-privacy",
        help="JSON privacy confirmation payload for sensitive/restricted sources",
    )
    preflight_parser.add_argument(
        "--offline",
        action="store_true",
        help="Validate local preflight plumbing without provider call",
    )

    validate_draft_parser = subparsers.add_parser(
        "validate-draft", help="Validate an LLM draft"
    )
    validate_draft_parser.add_argument("draft", help="Draft Markdown file")
    validate_draft_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    validate_draft_parser.add_argument("--target", help="Target wiki page title")

    publish_draft_parser = subparsers.add_parser(
        "publish-draft", help="Publish a validated draft"
    )
    publish_draft_parser.add_argument("draft", help="Draft Markdown file")
    publish_draft_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    publish_draft_parser.add_argument(
        "--target", required=True, help="Target wiki page title"
    )

    repair_draft_parser = subparsers.add_parser(
        "repair-draft", help="Repair an LLM draft with exact local evidence"
    )
    repair_draft_parser.add_argument("draft", help="Draft Markdown file")
    repair_draft_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    repair_draft_parser.add_argument(
        "--target", required=True, help="Target wiki page title"
    )

    compile_page_parser = subparsers.add_parser(
        "compile-page", help="Draft, validate, repair if needed, and publish a page"
    )
    compile_page_parser.add_argument(
        "--root", required=True, help="Knowledge base root directory"
    )
    compile_page_parser.add_argument(
        "--query", help="Query used to retrieve context"
    )
    compile_page_parser.add_argument("--title", help="Page title")
    compile_page_parser.add_argument(
        "--kind",
        choices=(
            "daily",
            "weekly-review",
            "monthly-review",
            "yearly-review",
            "goal",
            "person",
            "project",
            "decision",
            "preference-summary",
            "agent-context",
        ),
        help="First-stage personal exobrain compile kind",
    )
    compile_page_parser.add_argument("--date", default="", help="Date for daily kind")
    compile_page_parser.add_argument("--period", default="", help="Period for review kinds")
    compile_page_parser.add_argument(
        "--archive-existing",
        action="store_true",
        help="Archive an existing same-title draft before generating a replacement",
    )

    profile_add_parser = subparsers.add_parser(
        "profile-add", help="Register or update a local profile root"
    )
    profile_add_parser.add_argument(
        "--config-dir", help="Configuration base directory"
    )
    profile_add_parser.add_argument("--name", required=True, help="Profile display name")
    profile_add_parser.add_argument("--root", required=True, help="Absolute profile root")
    profile_add_parser.add_argument("--kind", required=True, help="Profile kind")

    profile_list_parser = subparsers.add_parser(
        "profile-list", help="List registered local profiles"
    )
    profile_list_parser.add_argument(
        "--config-dir", help="Configuration base directory"
    )

    return parser


def _format_issue(issue: dict[str, str]) -> str:
    parts = [issue["type"]]
    parts.extend(f"{key}={issue[key]}" for key in sorted(issue) if key != "type")
    return " ".join(parts)


def _sanitize_error_message(message: str) -> str:
    sanitized = str(message)
    for name in ("KB_LLM_API_KEY", "KB_EMBEDDING_API_KEY"):
        api_key = os.environ.get(name)
        if api_key:
            sanitized = sanitized.replace(api_key, "[redacted]")
    return redact_text(sanitized, env=dict(os.environ))


def _format_product_console_menu(state: dict[str, object]) -> str:
    root = state.get("root", {})
    health = state.get("health", {})
    actions = state.get("actions", [])
    root_path = root.get("path", "") if isinstance(root, dict) else ""
    health_status = health.get("status", "unknown") if isinstance(health, dict) else "unknown"
    lines = [
        f"product-console root={_sanitize_error_message(str(root_path))} status={health_status}",
        "actions:",
    ]
    if isinstance(actions, list):
        for index, action in enumerate(actions, start=1):
            if not isinstance(action, dict):
                continue
            marker = " confirm" if action.get("requires_confirmation") else ""
            label = _sanitize_error_message(str(action.get("label", action.get("id", ""))))
            action_id = _sanitize_error_message(str(action.get("id", "")))
            lines.append(f"{index}. {label} [{action_id}]{marker}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            result = init_repository(args.root)
            print(f"Initialized knowledge base at {result['root']}")
            return 0

        if args.command == "obsidian-init":
            result = init_obsidian_vault(args.root)
            print(f"Initialized Obsidian vault at {result['root']}")
            return 0

        if args.command == "exobrain-init":
            result = init_personal_exobrain(args.root)
            print(f"Initialized personal exobrain at {result['root']}")
            return 0

        if args.command == "self-statement":
            result = create_self_statement(
                args.root,
                text=args.text,
                event_date=args.event_date,
                privacy=args.privacy,
                confidence=args.confidence,
                input_method=args.input_method,
            )
            print(result["source_id"])
            return 0

        if args.command == "capture-candidate":
            result = capture_candidate(
                args.root,
                type=args.type,
                text=args.text,
                event_date=args.event_date,
                privacy=args.privacy,
                confidence=args.confidence,
                value_reason=args.value_reason,
                suggested_source_type=args.suggested_source_type,
            )
            print(_sanitize_error_message(str(result["id"])))
            return 0

        if args.command == "review-candidate":
            result = review_candidate(
                args.root,
                args.candidate_id,
                status=args.status,
            )
            print(
                f"{_sanitize_error_message(str(result['id']))} "
                f"status={_sanitize_error_message(str(result['status']))}"
            )
            return 0

        if args.command == "publish-memory":
            result = publish_memory(
                args.root,
                args.candidate_id,
                confirm=args.confirm,
            )
            print(_sanitize_error_message(str(result["source_id"])))
            return 0

        if args.command == "suggest-topics":
            result = suggest_topics(
                args.root,
                source_ids=list(args.source_id) if args.source_id else None,
            )
            if args.json:
                from .topic_suggestions import redacted_cli_payload

                print(
                    json.dumps(
                        redacted_cli_payload(result),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            else:
                print(_sanitize_error_message(str(result["path"])))
            return 0

        if args.command == "daily-workflow":
            result = daily_workflow(args.root, workflow_date=args.date)
            if args.json:
                from .daily_workflows import redacted_cli_payload

                print(
                    json.dumps(
                        redacted_cli_payload(result),
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                )
            else:
                print(_sanitize_error_message(str(result["path"])))
            return 0

        if args.command == "benchmark-add":
            result = benchmark_add(
                args.root,
                query=args.query,
                expected_source_ids=list(args.expected_source_id),
                expected_wiki_paths=list(args.expected_wiki_path),
                expected_quotes=list(args.expected_quote),
                privacy=args.privacy,
                confirmed=args.confirm_private,
                env=dict(os.environ),
            )
            from .retrieval_benchmark import redacted_cli_payload

            payload = redacted_cli_payload(result)
            if args.json:
                print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
            else:
                print(_sanitize_error_message(str(payload["benchmark"])))
            return 0

        if args.command == "exobrain-check":
            result = exobrain_check_repository(args.root)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"{_sanitize_error_message(str(result['status']))} "
                    f"{_sanitize_error_message(str(result['classification']))}"
                )
                counts = result.get("counts", {})
                if isinstance(counts, dict):
                    for key in sorted(counts):
                        print(
                            f"{_sanitize_error_message(str(key))}: "
                            f"{_sanitize_error_message(str(counts[key]))}"
                        )
            return 0 if result["status"] == "pass" else 1

        if args.command == "ingest":
            result = ingest_file(args.root, args.path)
            print(f"Ingested {result['source_id']} {result['raw_path']}")
            return 0

        if args.command == "ingest-singlefile":
            suffix = os.path.splitext(args.path)[1].lower()
            if suffix not in {".html", ".htm"}:
                raise RuntimeError("SingleFile ingest expects .html or .htm")
            result = ingest_file(args.root, args.path, workflow="singlefile")
            print(f"Ingested {result['source_id']} {result['raw_path']}")
            return 0

        if args.command == "ingest-zotero":
            suffix = os.path.splitext(args.path)[1].lower()
            if suffix not in {".bib", ".ris"}:
                raise RuntimeError("Zotero ingest expects .bib or .ris")
            result = ingest_file(args.root, args.path, workflow="zotero")
            print(f"Ingested {result['source_id']} {result['raw_path']}")
            return 0

        if args.command == "ingest-derived":
            result = ingest_derived(
                args.root,
                original=args.original,
                text=args.text,
                workflow=args.workflow,
            )
            print(
                f"Ingested {result['source_id']} {result['raw_path']} "
                f"original={result['original_path']}"
            )
            return 0

        if args.command == "ingest-pdf":
            result = ingest_pdf(args.root, args.path)
            print(
                f"Ingested {result['source_id']} {result['raw_path']} "
                f"original={result['original_path']}"
            )
            return 0

        if args.command == "ingest-ocr":
            result = ingest_ocr(args.root, args.path, lang=args.lang)
            print(
                f"Ingested {result['source_id']} {result['raw_path']} "
                f"original={result['original_path']}"
            )
            return 0

        if args.command == "ingest-inbox":
            result = ingest_inbox(args.root)
            print(f"Ingested {result['count']} inbox files")
            for metadata in result["ingested"]:
                print(
                    f"{metadata['source_id']}\t{metadata['inbox_path']}\t"
                    f"{metadata['raw_path']}"
                )
            return 0

        if args.command in {"index", "rebuild-index"}:
            result = rebuild_index(args.root)
            print(f"Rebuilt index at {result['root']} from {result['sources']} sources")
            return 0

        if args.command == "search":
            for result in search(args.root, args.query, args.limit):
                print(
                    f"{result['source_id']}\t{result['raw_path']}\t"
                    f"{result['title']}\t{result['snippet']}"
                )
            return 0

        if args.command == "vector-rebuild":
            result = vector_rebuild(args.root)
            print(
                f"Rebuilt vector index at {result['root']} "
                f"from {result['chunks']} chunks"
            )
            return 0

        if args.command == "semantic-search":
            for result in semantic_search(args.root, args.query, args.limit):
                print(
                    f"{result['score']:.6f}\t{result['source_id']}\t"
                    f"{result['raw_path']}\t{result['title']}\t{result['snippet']}"
                )
            return 0

        if args.command == "hybrid-search":
            for result in hybrid_search(args.root, args.query, args.limit):
                print(
                    f"{result['score']:.6f}\t{result['source_id']}\t"
                    f"{result['raw_path']}\t{result['title']}\t{result['snippet']}"
                )
            return 0

        if args.command == "eval-search":
            result = eval_search_repository(
                args.root,
                args.benchmark,
                limit=args.limit,
                env=dict(os.environ),
            )
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"{result['status']} {result['classification']}: "
                    f"fts_top_k_hit_rate={result['fts_top_k_hit_rate']}"
                )
            return 0 if result["status"] == "pass" else 1

        if args.command == "answer":
            result = answer(args.root, args.question, args.limit)
            print(f"status: {result['status']}")
            print(f"uncertainty: {result['uncertainty']}")
            print(f"answer: {result['answer']}")
            if result["source_ids"]:
                print(f"source_ids: {', '.join(result['source_ids'])}")
            if result["evidence"]:
                print("evidence:")
                for index, evidence in enumerate(result["evidence"], start=1):
                    location = evidence.get("path") or evidence.get("raw_path", "")
                    chunk = evidence.get("chunk_index")
                    suffix = f"#{chunk}" if chunk is not None else ""
                    print(
                        f"- [{index}] {evidence['kind']} "
                        f"{evidence['source_id']} {location}{suffix}: "
                        f"{evidence['quote']}"
                    )
            return 0

        if args.command == "lint":
            issues = lint_repository(args.root)
            if not issues:
                print("No lint issues")
                return 0
            for issue in issues:
                print(_format_issue(issue))
            return 1

        if args.command == "status":
            for key, value in status_repository(args.root).items():
                print(f"{key}: {value}")
            return 0

        if args.command == "govern":
            result = govern(args.root)
            print(f"Wrote quality report: {_sanitize_error_message(str(result['path']))}")
            print(f"blocking: {result['blocking_count']}")
            print(f"advisory: {result['advisory_count']}")
            return 1 if result["blocking_count"] else 0

        if args.command == "backup":
            result = backup_repository(
                args.root, args.output, allow_dirty=args.allow_dirty
            )
            print(result.to_json())
            return 0 if result.status == "pass" else 1

        if args.command == "restore":
            result = restore_repository(
                args.backup, args.root, replace=args.replace
            )
            print(result.to_json())
            return 0 if result.status == "pass" else 1

        if args.command == "migrate-check":
            result = migrate_check_repository(args.source, args.restored)
            if args.json:
                print(result.to_json())
            else:
                data = result.to_dict()
                print(
                    f"{data['status']} {data['classification']}: "
                    f"{data['summary']}"
                )
            return 0 if result.status == "pass" else 1

        if args.command == "gateway-check":
            result = gateway_check_repository(args.root)
            if args.json:
                print(result.to_json())
            else:
                data = result.to_dict()
                print(
                    f"{data['status']} {data['classification']}: "
                    f"{data['summary']}"
                )
            return 0 if result.status == "pass" else 1

        if args.command == "product-console":
            state = product_console_repository(args.root)
            if args.json:
                print(json.dumps(state, ensure_ascii=False, sort_keys=True))
            else:
                print(_format_product_console_menu(state))
            return 0

        if args.command == "trust-report":
            result = trust_report_repository(args.root)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(
                    f"{result['status']} {result['classification']}: "
                    f"stable_pages={result['summary']['stable_pages']} "
                    f"drafts={result['summary']['drafts']}"
                )
            return 1 if result["status"] == "failed" else 0

        if args.command == "web-console":
            if not is_loopback_host(args.host):
                raise RuntimeError("web-console host must be loopback, such as 127.0.0.1")
            serve_web_console(
                root=args.root,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
            )
            return 0

        if args.command == "doctor":
            result = doctor(args.root, online=args.online)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            else:
                print(format_doctor_summary(result))
            return 0 if result["status"] == "pass" else 1

        if args.command == "schema-check":
            result = schema_check_repository(
                args.root, write_manifest=args.write_manifest
            )
            if args.json:
                print(result.to_json())
            else:
                data = result.to_dict()
                print(
                    f"{data['status']} {data['classification']}: "
                    f"{data['summary']}"
                )
            return 0 if result.status == "pass" else 1

        if args.command == "lock-check":
            result = lock_check_repository(args.root)
            if args.json:
                print(result.to_json())
            else:
                data = result.to_dict()
                print(
                    f"{data['status']} {data['classification']}: "
                    f"{data['summary']}"
                )
            return 0 if result.status == "pass" else 1

        if args.command == "recover-lock":
            result = recover_lock_repository(
                args.root, manual_confirm=args.manual_confirm
            )
            if args.json:
                print(result.to_json())
            else:
                data = result.to_dict()
                print(
                    f"{data['status']} {data['classification']}: "
                    f"{data['summary']}"
                )
            return 0 if result.status == "pass" else 1

        if args.command == "review-source":
            result = review_source(
                args.root,
                args.source_id,
                status=args.status,
                reviewer=args.reviewer,
                note=args.note,
            )
            print(f"Reviewed {result['source_id']} status={result['review_status']}")
            return 0

        if args.command == "refresh-source":
            result = refresh_source(args.root, args.source_id)
            print(
                f"Refreshed {result['old_source_id']} -> {result['source_id']} "
                f"chunks={result['chunks']}"
            )
            return 0

        if args.command == "llm-check":
            result = llm_check()
            print(
                "LLM config ok: "
                f"KB_LLM_BASE_URL={result['base_url']} "
                f"KB_LLM_MODEL={result['model']} "
                f"KB_LLM_API_KEY={result['api_key']} "
                f"KB_LLM_TIMEOUT_SECONDS={result['timeout_seconds']} "
                f"KB_LLM_RESPONSE_FORMAT={result['response_format']} "
                f"KB_LLM_MAX_TOKENS={result['max_tokens']} "
                f"KB_LLM_THINKING={result['thinking']} "
                f"KB_LLM_REASONING_EFFORT={result['reasoning_effort']}"
            )
            return 0

        if args.command == "llm-contract-check":
            result = llm_contract_check(
                args.response,
                title=args.title,
                target=args.target,
                root=args.root,
                query=args.query,
                context_limit=args.context_limit,
            )
            print(f"status: {result['status']}")
            return 0 if result["status"] == "pass" else 1

        if args.command == "embedding-check":
            result = embedding_check()
            print(
                "Embedding config ok: "
                f"KB_EMBEDDING_BASE_URL={result['base_url']} "
                f"KB_EMBEDDING_MODEL={result['model']} "
                f"KB_EMBEDDING_API_KEY={result['api_key']} "
                f"KB_EMBEDDING_TIMEOUT_SECONDS={result['timeout_seconds']}"
            )
            return 0

        if args.command == "ocr-check":
            result = ocr_check()
            print(f"OCR config ok: KB_TESSERACT_CMD={result['command']}")
            return 0

        if args.command == "ocr-fixture":
            result = create_ocr_fixture(args.output, text=args.text)
            print(_sanitize_error_message(result["path"]))
            return 0

        if args.command == "llm-draft":
            result = llm_draft(args.root, args.query, args.title)
            print(result["path"])
            return 0

        if args.command == "llm-preflight":
            confirmation = None
            if args.confirm_privacy:
                try:
                    confirmation = json.loads(args.confirm_privacy)
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Invalid privacy confirmation JSON") from exc
            result = llm_preflight_repository(
                args.root,
                args.query,
                args.title,
                target=args.target,
                context_limit=args.context_limit,
                write_audit=args.write_audit,
                privacy_confirmation=confirmation,
                offline=args.offline,
            )
            if args.json:
                print(result.to_json())
            else:
                data = result.to_dict()
                print(
                    f"{data['status']} {data['classification']}: "
                    f"{data['summary']}"
                )
            if result.status == "pass":
                return 0
            if args.offline and result.classification == "configured_but_unverified":
                return 0
            return 1

        if args.command == "validate-draft":
            issues = validate_draft_file(args.root, args.draft, target=args.target)
            if not issues:
                print("Draft valid")
                return 0
            for issue in issues:
                print(_sanitize_error_message(_format_issue(issue)))
            return 1

        if args.command == "publish-draft":
            result = publish_draft(args.root, args.draft, args.target)
            issues = result["issues"]
            if issues:
                for issue in issues:
                    print(_sanitize_error_message(_format_issue(issue)))
                return 1
            print(_sanitize_error_message(str(result["target"])))
            return 0

        if args.command == "repair-draft":
            result = repair_draft_file(args.root, args.draft, target=args.target)
            issues = result["issues"]
            if issues:
                for issue in issues:
                    print(_sanitize_error_message(_format_issue(issue)))
                return 1
            print(_sanitize_error_message(str(result["path"])))
            return 0

        if args.command == "compile-page":
            if args.kind:
                request = personal_compile_request(
                    kind=args.kind,
                    title=args.title or "",
                    date=args.date,
                    period=args.period,
                )
                result = compile_page(
                    args.root,
                    request.query,
                    request.title,
                    target=request.target,
                    context_limit=request.context_limit,
                    archive_existing=args.archive_existing,
                )
            else:
                if not args.query or not args.title:
                    raise RuntimeError("--query and --title are required unless --kind is used")
                result = compile_page(
                    args.root,
                    args.query,
                    args.title,
                    archive_existing=args.archive_existing,
                )
            issues = result["issues"]
            if issues:
                for issue in issues:
                    print(_sanitize_error_message(_format_issue(issue)))
                return 1
            print(_sanitize_error_message(str(result["target"])))
            return 0

        if args.command == "profile-add":
            result = profile_add(
                args.config_dir,
                name=args.name,
                root=args.root,
                kind=args.kind,
            )
            print(f"{result['name']}\t{result['root']}\t{result['kind']}")
            return 0

        if args.command == "profile-list":
            for profile in profile_list(args.config_dir):
                print(f"{profile['name']}\t{profile['root']}\t{profile['kind']}")
            return 0
    except WriteLockError as exc:
        message = _sanitize_error_message(f"{exc.classification}: {exc.summary}")
        print(f"error: {message}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print(f"error: {_sanitize_error_message(str(exc))}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError) as exc:
        print(f"error: {_sanitize_error_message(str(exc))}", file=sys.stderr)
        return 1

    parser.error(f"Unknown command: {args.command}")
    return 2
