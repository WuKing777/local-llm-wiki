# Local LLM Wiki

Local LLM Wiki is a file-first personal knowledge base for local source material, wiki notes, and rebuildable search metadata. Durable content stays in plain files; SQLite databases, vector indexes, caches, and reports are generated local state.

Read this page in [Chinese README](README.zh-CN.md).

## Public Clone-and-Run Quick Start

Use Python 3.11+ and Git from a clean public snapshot:

```powershell
git clone "https://github.com/WuKing777/local-llm-wiki.git" "local-llm-wiki"
cd "local-llm-wiki"
python -B -m pip install -e .
python -B -m kb --help
kb --help
python -B -m kb doctor --root "examples/demo-root"
python -B -m kb product-console --root "examples/demo-root" --json
python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open
```

The repository URL placeholder is a private-source template value. The release export replaces it with the approved GitHub clone URL; a final public artifact must not retain repository URL placeholders.

The one-command demo path `tools/run-demo.ps1` uses synthetic demo data only:

```powershell
.\tools\run-demo.ps1
```

Start with [中文快速开始](docs/product/quickstart-zh.md), then run the synthetic [首次运行演示](docs/product/first-run-demo.md). Use the [命令选择指南](docs/product/command-guide.md) when you know the goal but not the command name. The local browser entry is documented in [本地网页控制台](docs/product/local-web-console.md).

The `examples/demo-root` tree is synthetic public demo data. Do not point demo commands at a real user vault, private source collection, real provider, or production root. For a new local root, initialize a directory you control and pass it explicitly:

```powershell
python -B -m kb init --root "<root>"
python -B -m kb doctor --root "<root>"
python -B -m kb schema-check --root "<root>" --json
python -B -m kb product-console --root "<root>" --json
python -B -m kb web-console --root "<root>" --no-open
```

Open `"<root>"` in Obsidian or another editor only after initialization. Keep source material in `raw/`, source cards in `sources/`, authored pages in `wiki/`, review metadata in `meta/`, and generated indexes in `db/`.

## Safety Model

The product is local-first: commands operate on an explicit local root, and public smoke commands use only synthetic demo data. Real user vault data is not uploaded. No cloud or LLM provider is configured or called by default. Cloud or LLM use is off by default.

AI/LLM output is never a fact source. DeepSeek and other LLMs may organize, summarize, reason, and draft from approved local context, but stable claims require local evidence and stable wiki content must pass `validate-draft` and `publish-draft` gates with local quote evidence. Local bge-m3 embeddings are retrieval accelerators only, not reasoning authority or citation authority.

Do not store API keys, bearer tokens, prompts, full provider responses, private source text, raw chunks, or source chunks outside approved local evidence and audit paths. This repository is intended for a clean snapshot with no private Git history.

## Core Local Workflows

Import and search local evidence before drafting:

```powershell
python -B -m kb ingest "<source-file>" --root "<root>"
python -B -m kb ingest-inbox --root "<root>"
python -B -m kb rebuild-index --root "<root>"
python -B -m kb search "<query>" --root "<root>"
python -B -m kb answer "<question>" --root "<root>"
```

Use the draft-first LLM path only after local evidence exists and provider use is explicitly approved:

```powershell
python -B -m kb llm-draft --root "<root>" --query "<query>" --title "<title>"
python -B -m kb validate-draft --root "<root>" "<draft-path>" --target "<title>"
python -B -m kb publish-draft --root "<root>" "<draft-path>" --target "<title>"
```

Review and governance commands remain local evidence controls:

```powershell
python -B -m kb review-source "src-xxxxxxxxxxxx" --root "<root>" --status reviewed --reviewer "<reviewer>"
python -B -m kb lint --root "<root>"
python -B -m kb status --root "<root>"
python -B -m kb govern --root "<root>"
```

## Public Documentation

- [CONTRIBUTING.md](CONTRIBUTING.md)
- [Roadmap](docs/product/roadmap.md)
- [Release Checklist](docs/product/release-checklist.md)
- [Installation](docs/product/installation.md)
- [Configuration](docs/product/configuration.md)
- [Backup, Restore, and Migration](docs/product/backup-restore-migration.md)
- [Privacy and Secrets](docs/product/privacy-and-secrets.md)
- [Provider Preflight](docs/product/provider-preflight.md)
- [Open Source Release](docs/product/open-source-release.md)
- [Examples](examples/README.md)
- [LICENSE](LICENSE)
- [SECURITY.md](SECURITY.md)

These docs cover local clone-and-run use, another-machine setup, synthetic demo operation, contribution boundaries, offline defaults, provider preflight limits, the clean public export boundary, and the command surfaces intended for local product operation.

## Focused Tests

```powershell
python -B -m unittest tests.test_open_source_distribution tests.test_open_source_release tests.test_docs_encoding -v
python -B -m unittest tests.test_public_export -v
python -B -m unittest discover -s tests -v
```
