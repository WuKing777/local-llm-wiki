# Open Source Release

This guide defines the public publication boundary for this repository. It prepares a later human-approved handoff only; it does not publish anything.

Real user vault data is not uploaded. No cloud or LLM provider is configured or called by default. Cloud or LLM use is off by default. Do not point release checks at real user vaults. Do not call real providers while preparing the public artifact.

## Clean Public Export

The private development Git history is not a public release artifact. Do not push the private repository history to a public remote, and do not treat a private full-history scan as release approval. Public publication must use a clean snapshot, a new public repository, a squash import, or an equivalent history-safe boundary that does not expose private development history.

Create the candidate artifact from the current working tree into a new directory outside the source repository:

```powershell
.\tools\create-public-export.ps1 `
  -OutputPath "<public-export-root>" `
  -RepositoryUrl "https://github.com/<owner>/local-llm-wiki.git"
```

Repository URL placeholders are allowed only in the private source tree; the final public artifact must contain none. `-RepositoryUrl` accepts only a credential-free HTTPS GitHub repository URL with one owner and repository path. It materializes clone instructions and package project URLs in the exported copy without modifying the private source tree.

The export command is non-destructive. It fails when the output directory already exists, when the output path is inside the source repository, or when the repository URL contains credentials, a query, a fragment, traversal, an unsupported host, or extra path segments. It excludes private runtime roots, generated caches, build outputs, private Git metadata, and internal implementation evidence under `docs/superpowers/`.

## Local/Offline Checks

Run the public export and scan checks before publication. Run public export and documentation checks before any release decision:

```powershell
python -B -m unittest tests.test_public_export -v
python -B -m unittest tests.test_open_source_distribution tests.test_open_source_release tests.test_docs_encoding -v
```

The public export scan must cover every exported text file, not only changed files. A passing scan means the current clean snapshot candidate does not contain detected static provider-key shapes, bearer-token shapes, concrete drive paths, root runtime data directories, or internal `docs/superpowers/` evidence.

AI/LLM output is never a fact source. Stable wiki content must pass validate and publish gates with local quote evidence. Do not persist prompts, full provider responses, API keys, bearer tokens, private source text, raw chunks, or source chunks outside approved local evidence.

## Account and Credential Gates

Rotate any provider credential that was disclosed before publication, then verify the retired credential no longer works. Do not record the old or replacement value in the repository, report, issue, commit, or shell transcript.

Authenticate the intended GitHub account outside the repository and verify it with:

```powershell
gh auth status
```

Authentication is necessary for publication but is not publication approval. The repository owner, repository name, visibility, and exact exported commit still require explicit human approval before external publication.

## Publication Handoff

After audit and release owners accept the candidate, publish only the clean snapshot boundary:

- Initialize a new public repository from the exported snapshot with exactly one initial commit, or
- import the exported snapshot as a squash commit, or
- use an equivalent clean-public-history process approved for the release.

Before any remote creation or push, require `git status --short` to be empty, `git rev-list --count HEAD` to report `1` for the new-repository path, and the focused, full, export, install, demo, and hygiene checks to pass against the exported candidate.

Do not publish `.git` from the private development repository. Do not publish private root data directories such as `raw/`, `sources/`, `wiki/`, `meta/`, `.obsidian/`, `db/`, `backups/`, `reports/`, or `inbox/`. Do not publish prompts, full provider responses, API keys, bearer tokens, private source text, raw chunks, concrete private paths, real vault artifacts, or real user profile data.

Use [Release Checklist](release-checklist.md) for the human approval gate. This guide does not create a public repository, push to GitHub, publish to PyPI, approve real-provider use, approve operation on a real user vault, or grant final product acceptance.
