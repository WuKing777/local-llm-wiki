# Configuration

Configuration is local, explicit, and non-secret by default. Keep persistent product configuration separate from API keys, bearer tokens, prompts, source chunks, private source text, and full provider responses.

After `python -B -m pip install -e .`, both `python -B -m kb --help` and `kb --help` should work from a public clone. Use `"examples/demo-root"` only for synthetic smoke commands; use `"<root>"` for a root that is explicitly approved for the current operation.

Real user vault data is not uploaded, and cloud/LLM providers are not called by default.

## Profile Registry

Profiles name approved roots without making those roots safe for every operation. Register only paths that are already approved for the current task:

```powershell
python -B -m kb profile-add --name "personal" --root "<root>" --kind personal
python -B -m kb profile-list
```

The profile registry is non-secret metadata. It can store profile names, root paths, and kinds, but it must not store credentials or private examples. A registered profile is a convenience handle, not authorization to run draft validation, publish, backup, restore, or benchmark operations on real user data.

## Non-Secret Config

Safe durable configuration includes feature choices, local executable paths, timeout numbers, profile names, and model labels. Secret values stay outside the repository and outside durable knowledge-base files.

Use shell environment variables for provider and optional dependency fallback:

```powershell
$env:KB_LLM_BASE_URL="<openai-compatible-base-url>"
$env:KB_LLM_MODEL="<chat-model>"
$env:KB_LLM_API_KEY="<api-key>"
$env:KB_LLM_TIMEOUT_SECONDS="30"
$env:KB_EMBEDDING_BASE_URL="<openai-compatible-base-url>"
$env:KB_EMBEDDING_MODEL="<embedding-model>"
$env:KB_EMBEDDING_API_KEY="<api-key>"
$env:KB_EMBEDDING_TIMEOUT_SECONDS="30"
$env:KB_TESSERACT_CMD="<path-to-tesseract.exe>"
```

Set API keys only in the current shell or user environment, never in README files, wiki pages, drafts, source cards, logs, review notes, benchmark files, scripts, or chat transcripts.

## Optional Dependencies

- OCR is optional and uses `KB_TESSERACT_CMD` or `tesseract` on `PATH`.
- LLM drafting is optional and uses OpenAI-compatible chat configuration only when explicitly requested.
- Semantic retrieval is optional and uses OpenAI-compatible embedding configuration only when vector workflows are requested.
- Local bge-m3 embeddings are retrieval accelerators only, not reasoning authority or citation authority.

Cloud or LLM use is off by default. DeepSeek and other LLMs may organize, summarize, reason, and draft only after configuration and operation approval, and their output still needs evidence gates before stable publication.

## Real Root Conditions

Use a real root only when all conditions below are true:

- The current PM operation task names the exact root path and exact item paths in scope.
- The root is not a real user vault unless that exact operation is approved.
- `python -B -m kb doctor --root "<root>"` has been reviewed for blocking issues.
- `python -B -m kb lock-check --root "<root>" --json` does not show an active or uncertain writer.
- The command is within the approved operation type and allowed files.
- The operation will not send private source content, prompts, source chunks, or full provider responses outside approved local evidence.

Personal use is local-first. Another Windows computer is supported by reinstalling prerequisites, restoring or opening an approved root, setting non-secret configuration again, and keeping secrets in that computer's shell or user environment. Future commercialization requires a separate product, legal, security, privacy, support, and release process.

For public publication, use the clean snapshot boundary in [Open Source Release](open-source-release.md) so local configuration examples do not become a private-history release.
