# Provider Preflight

Provider preflight checks whether a configured LLM path is usable for a bounded draft workflow. It is provider-agnostic and does not make the model a source of fact.

Public clone-and-run commands should use `python -B -m kb --help`, `kb --help`, and the synthetic `"examples/demo-root"` smoke path before any provider configuration is considered.

Real user vault data is not uploaded, and cloud/LLM providers are not called by default.

## Offline Default

Use offline preflight first:

```powershell
python -B -m kb llm-preflight --root "<root>" --query "<query>" --title "<title>" --offline --json
```

Offline preflight validates local root shape, retrieval context availability, privacy boundaries, and response-contract plumbing without a provider call. Cloud or LLM use is off by default.

## Optional Providers

DeepSeek and other OpenAI-compatible providers are optional. Configure them only outside the repository:

```powershell
$env:KB_LLM_BASE_URL="<openai-compatible-base-url>"
$env:KB_LLM_MODEL="<chat-model>"
$env:KB_LLM_API_KEY="<api-key>"
$env:KB_LLM_TIMEOUT_SECONDS="30"
```

DeepSeek and other LLMs may organize, summarize, reason, and draft. AI/LLM output is never a fact source. Stable wiki content must pass validate and publish gates with local quote evidence.

Real-provider readiness is limited to configuration and preflight checks. A successful preflight does not approve production use, real user-vault publication, real retrieval benchmarks, or broad cloud sending.

## Embeddings and bge-m3

Configure embeddings only for retrieval workflows:

```powershell
$env:KB_EMBEDDING_BASE_URL="<openai-compatible-base-url>"
$env:KB_EMBEDDING_MODEL="<embedding-model>"
$env:KB_EMBEDDING_API_KEY="<api-key>"
$env:KB_EMBEDDING_TIMEOUT_SECONDS="30"
```

Local bge-m3 embeddings are retrieval accelerators only, not reasoning authority or citation authority. Vector hits help find candidate local chunks, but they never replace exact quote evidence, source review, validation, or publish gates.

## Retrieval and Product Checks

Run deterministic local checks before considering provider-backed workflows:

```powershell
python -B -m kb doctor --root "<root>"
python -B -m kb gateway-check --root "<root>" --json
python -B -m kb eval-search --root "<root>" --benchmark "meta/evals/retrieval-benchmark.jsonl" --json
python -B -m kb benchmark-add --root "<root>" --query "<query>" --expected-source-id "src-xxxxxxxxxxxx" --expected-quote "<local quote>" --privacy public --json
python -B -m kb exobrain-check --root "<root>" --json
```

Real retrieval benchmark operations require a later exact-path PM operation task with explicit root and item paths. Synthetic or fixture benchmarks can test command shape; they do not authorize use of private source material.

The eval-search benchmark report is deterministic and redacted. Quote-support scoring checks whether optional `expected_quotes` appear in local retrieved evidence, but it is a metric-only signal. Semantic and hybrid retrieval remain candidate-finding accelerators and never become source, citation, validation, publish, or governance authority.

## Limits

Preflight is a readiness classifier, not a publishing gate. Real user-vault draft validate/publish operations require a later exact-path PM operation task with explicit root and item paths. Preflight must not persist prompts, full provider responses, API keys, bearer tokens, private source text, or source chunks outside approved local evidence.

For public publication, use the clean snapshot boundary in [Open Source Release](open-source-release.md). Provider preflight success does not authorize a public push of private repository history.
