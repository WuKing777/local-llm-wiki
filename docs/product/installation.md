# Installation

This product is local-first software for a knowledge-base root that you choose. Personal use is local-first: install the package locally, create or open one root at a time, and pass that root explicitly in every command. Real user vault data is not uploaded. No cloud or LLM provider is configured or called by default. Cloud or LLM use is off by default.

## Prerequisites

- Python 3.11 or newer.
- Git for source control and dirty-worktree visibility.
- PowerShell on Windows.
- Optional local tools such as Tesseract, an embedding server, or an OpenAI-compatible chat server only when you need those features and explicitly choose them.

## Public Clone and Editable Install

Clone the repository with a placeholder URL, install it in editable mode, and verify both CLI entry points:

```powershell
git clone "https://github.com/WuKing777/local-llm-wiki.git" "local-llm-wiki"
cd "local-llm-wiki"
python -B -m pip install -e .
python -B -m kb --help
kb --help
```

The repository URL placeholder is allowed only in the private source template. The release export must replace it with the approved GitHub clone URL, and the final public artifact must contain no repository URL placeholder.

Run the synthetic demo from the repository root. The demo is public placeholder content and is safe for local smoke commands:

```powershell
python -B -m kb doctor --root "examples/demo-root"
python -B -m kb product-console --root "examples/demo-root" --json
python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open
.\tools\run-demo.ps1
```

Do not point demo commands at a real user vault, private source collection, real provider, or production root.

## First Run

Create a new root with a generic quoted path placeholder:

```powershell
python -B -m kb init --root "<root>"
```

Open `"<root>"` in Obsidian or another editor after initialization. Do not point the product at a real user vault by default. Real user-vault draft validate/publish operations require a later exact-path PM operation task with explicit root and item paths. Real retrieval benchmark operations require a later exact-path PM operation task with explicit root and item paths.

Run read-only and schema checks before using write workflows:

```powershell
python -B -m kb doctor --root "<root>"
python -B -m kb schema-check --root "<root>" --json
python -B -m kb product-console --root "<root>" --json
python -B -m kb web-console --root "<root>" --no-open
```

`doctor` reports local health without online probes by default. `schema-check` validates manifests, source cards, wiki front matter, profile registry, backup manifest, and benchmark files. `product-console` summarizes local state and available actions without calling providers.

## Completed Command Surfaces

The completed product command surfaces are shown below with safe placeholder paths:

```powershell
python -B -m kb doctor --root "<root>"
python -B -m kb schema-check --root "<root>" --json
python -B -m kb lock-check --root "<root>" --json
python -B -m kb recover-lock --root "<root>" --manual-confirm --json
python -B -m kb ingest "<source-file>" --root "<root>"
python -B -m kb search "<query>" --root "<root>"
python -B -m kb answer "<question>" --root "<root>"
python -B -m kb backup --root "<root>" --output "<backup.zip>"
python -B -m kb restore --backup "<backup.zip>" --root "<restored-root>"
python -B -m kb migrate-check --source "<root>" --restored "<restored-root>" --json
python -B -m kb llm-preflight --root "<root>" --query "<query>" --title "<title>" --offline --json
python -B -m kb eval-search --root "<root>" --benchmark "meta/evals/retrieval-benchmark.jsonl" --json
python -B -m kb gateway-check --root "<root>" --json
python -B -m kb product-console --root "<root>" --json
python -B -m kb web-console --root "<root>" --no-open
python -B -m kb validate-draft --root "<root>" "<draft-path>" --target "<title>"
python -B -m kb publish-draft --root "<root>" "<draft-path>" --target "<title>"
python -B -m kb capture-candidate --root "<root>" --type self_statement --text "<candidate text>" --event-date "2026-07-07" --privacy personal --confidence confirmed --value-reason "<why useful>" --suggested-source-type self_statement
python -B -m kb review-candidate "<candidate-id>" --root "<root>" --status approved
python -B -m kb publish-memory "<candidate-id>" --root "<root>" --confirm
python -B -m kb suggest-topics --root "<root>" --json
python -B -m kb daily-workflow --root "<root>" --date "2026-07-07" --json
python -B -m kb benchmark-add --root "<root>" --query "<query>" --expected-source-id "src-xxxxxxxxxxxx" --privacy public --json
python -B -m kb exobrain-check --root "<root>" --json
```

The commands above document command shape only. They are not permission to operate on a real user vault, call real providers, or publish stable personal content.

## Evidence and Publication Boundaries

AI/LLM output is never a fact source. DeepSeek and other LLMs may organize, summarize, reason, and draft from approved local context. Stable wiki content must pass validate and publish gates with local quote evidence. Local bge-m3 embeddings are retrieval accelerators only, not reasoning authority or citation authority.

Do not persist prompts, full provider responses, API keys, bearer tokens, private source text, or source chunks outside approved local evidence. Real-provider readiness is limited to configuration and preflight checks.

Another Windows computer is supported by reinstalling prerequisites, cloning the repository, installing with `python -B -m pip install -e .`, restoring or opening an approved root, and rerunning `doctor`, `schema-check`, and `product-console`.

Future commercialization requires a separate product, legal, security, privacy, support, and release process. This repository documentation describes current local product operation; it does not create a hosted service boundary or customer support commitment.

For public publication, use the clean snapshot boundary in [Open Source Release](open-source-release.md) and [Release Checklist](release-checklist.md) instead of pushing private development history. Do not publish private development Git history.
