# Security Policy

Report vulnerabilities privately. Use GitHub private vulnerability reporting when it is available for this repository; if it is not available, open a minimal public issue asking for a private reporting channel and do not include exploit details.

Do not include secrets, API keys, bearer tokens, credentials, private paths, private source content, prompts, full provider responses, or raw source chunks in a report. Do not include private source content. Do not include prompts, full provider responses, or raw source chunks. Use Synthetic demo data or a minimal redacted reproduction that can run locally without provider calls.

This project is local-first software. Cloud or LLM use is off by default, and vulnerability reports should not require real providers, real user vaults, or private source material. If a security issue appears to involve provider configuration, describe the configuration shape with placeholders only.

Supported security expectations for public release:

- No secret-shaped values in docs, examples, source cards, manifests, logs, or CI configuration.
- No private source text or raw chunks in public examples.
- No default cloud calls in CI, demo smoke commands, or product-console output.
- No persistence of prompts, full model responses, API keys, bearer tokens, private source content, or raw source chunks outside approved local evidence.
