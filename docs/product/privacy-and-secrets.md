# Privacy and Secrets

AI/LLM output is never a fact source. DeepSeek and other LLMs may organize, summarize, reason, and draft from approved local context, but Stable wiki content must pass validate and publish gates with local quote evidence.

For open-source use, start with the synthetic `"examples/demo-root"` and the repository [security policy](../../SECURITY.md). Public examples must not contain secrets, private source content, prompts, full provider responses, or raw source chunks.

Real user vault data is not uploaded, and cloud/LLM providers are not called by default.

## Evidence Gates

Stable pages are source-grounded, not model-grounded. A stable claim must be tied to local source chunks and exact quotes through validate and publish gates. Citations alone are not enough when the claim text is not supported by local quote evidence.

Draft workflows must keep retrieved context and model output auditable without treating model prose as truth. If no local evidence is retrieved, drafting should fail without calling a provider or writing draft state.

## Privacy Levels

Use the existing privacy levels consistently:

- `public`: safe to discuss and benchmark when source evidence is approved.
- `personal`: private user context that must stay local unless an operation explicitly says otherwise.
- `sensitive`: higher-risk material that requires explicit confirmation before any cloud-send or benchmark use.
- `restricted`: material that should not leave approved local evidence paths and should not be used in provider calls by default.

Real user-vault draft validate/publish operations require a later exact-path PM operation task with explicit root and item paths.

## Cloud-Send Boundaries

Cloud or LLM use is off by default. Real provider calls require explicit provider configuration, explicit operation approval, privacy confirmation where applicable, and a bounded task that says what may be sent.

Do not persist prompts, full provider responses, API keys, bearer tokens, private source text, or source chunks outside approved local evidence. Redacted audit metadata may record model labels, hashes, timing, classifications, and pass/fail status, but it must not reveal secrets or private content.

## Secret Hygiene

Keep secrets in the current shell or user environment only:

```powershell
$env:KB_LLM_API_KEY="<api-key>"
$env:KB_EMBEDDING_API_KEY="<api-key>"
```

Never paste concrete API keys or bearer tokens into docs, wiki pages, drafts, logs, review queues, benchmark files, scripts, commits, or chat transcripts. Redaction must happen before values are printed, written, or included in reports.

## Memory Candidate Workflow

Candidate memory commands create draftable local records, not stable facts by themselves:

```powershell
python -B -m kb capture-candidate --root "<root>" --type self_statement --text "<candidate text>" --event-date "2026-07-07" --privacy personal --confidence confirmed --value-reason "<why useful>" --suggested-source-type self_statement
python -B -m kb review-candidate "<candidate-id>" --root "<root>" --status approved
python -B -m kb publish-memory "<candidate-id>" --root "<root>" --confirm
python -B -m kb suggest-topics --root "<root>" --json
python -B -m kb daily-workflow --root "<root>" --date "2026-07-07" --json
```

Publication still depends on local evidence, source review, and validation gates. A memory candidate is not a stable wiki claim until it is reviewed and published through the allowed local workflow.

For public publication, use the clean snapshot boundary in [Open Source Release](open-source-release.md) and scan the exported files before any public repository import.
