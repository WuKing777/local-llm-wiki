# Examples

`demo-root` is a synthetic knowledge-base root for local smoke commands. It contains only public demo text and placeholder metadata; it is not a real user vault and it does not contain provider output.

From the repository root after an editable install:

```powershell
python -B -m kb doctor --root "examples/demo-root"
python -B -m kb product-console --root "examples/demo-root" --json
```

The doctor command may report warnings or failures for optional local dependencies or missing generated indexes. The product-console command is the primary deterministic demo smoke because it is read-only, local, and does not call providers.

`demo-story` contains public source materials for the expanded first-run story. `tools/run-demo.ps1` imports those files into a temp synthetic root, runs candidate capture/review/publish-memory, deterministic draft validate/publish, trust-report, governance, backup, restore, and migrate-check, and writes a redacted `synthetic-demo-story-v1` report. The default story boundary is `tracked_fixtures_mutated=false`.
