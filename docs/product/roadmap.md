# Roadmap

This roadmap describes public local/offline capability and conservative future work. Real user vault data is not uploaded. No cloud or LLM provider is configured or called by default. Cloud or LLM use is off by default.

## Available now: local/offline

- Public clone-and-run setup with Python 3.11+, editable install, `python -B -m kb --help`, and `kb --help`.
- Synthetic first-run demo through `examples/demo-root` and `tools/run-demo.ps1`.
- Local doctor, schema-check, product-console, and web-console commands for an explicit local root.
- File-first source cards, wiki pages, review metadata, and rebuildable generated state.
- Evidence-gated draft validation and publish gates: AI/LLM output is never a fact source, and stable wiki content must pass validate and publish gates with local quote evidence.
- Local privacy, provider-preflight, backup, restore, migration, and command-guide documentation for offline operation.

## Planned work

- Clearer public examples that stay synthetic and do not include private source text, prompts, raw chunks, provider responses, concrete private paths, or real vault artifacts.
- More local-only diagnostics for first-run setup and common Windows environment issues.
- Additional documentation for clean public snapshots, community triage, and offline support boundaries.
- More focused tests for public documentation links, release wording, and issue-template safety.

## Not ready or not certified

- No hosted service is provided.
- No installer is provided.
- No PyPI package is provided.
- No GitHub release is provided.
- Real-provider readiness is not certified by this roadmap; provider configuration remains opt-in and bounded by preflight checks.
- Real-vault readiness is not certified by this roadmap; demo and public validation use synthetic data only.
- Obsidian integration is not certified as a public product surface. Opening a local root in an editor remains a local user choice after initialization.
- Public publication is not approved by this roadmap. Publication requires a clean snapshot, human release approval, and the release checklist.

Do not publish private development Git history.
