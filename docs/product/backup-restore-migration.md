# Backup, Restore, and Migration

Backup and restore protect durable knowledge assets, not runtime caches. They must preserve source evidence, wiki content, metadata, and auditability without copying generated state that can be rebuilt.

For public clone-and-run checks, use `"examples/demo-root"` only as synthetic demo data. Backup and restore examples below use generic quoted placeholders because real roots require explicit approval.

Real user vault data is not uploaded, and cloud/LLM providers are not called by default.

## Allowlist Backup

Use an output path outside the root:

```powershell
python -B -m kb backup --root "<root>" --output "<backup.zip>"
```

The backup allowlist is limited to durable assets such as raw source material, source cards, stable wiki files, review metadata, manifests, and approved benchmark files. Runtime databases, vector indexes, caches, temporary files, lock files, local workspaces, provider responses, prompts, API keys, bearer tokens, and private examples outside approved source evidence are excluded or rejected.

If the worktree has durable dirty changes, backup should be treated cautiously. Prefer a clean worktree. If an approved operation allows dirty backup, the manifest must record the dirty state instead of hiding it.

## Safe Restore

Restore to a new or empty target unless a later exact-path operation explicitly approves replacement:

```powershell
python -B -m kb restore --backup "<backup.zip>" --root "<restored-root>"
```

Restore validates the archive and manifest, stages content under the target parent, and uses rollback paths for replacement cases. If post-restore checks fail, the product must either leave the original target untouched or restore the previous content according to the command's rollback contract.

## Migration Check

Compare source and restored durable assets after restore:

```powershell
python -B -m kb migrate-check --source "<root>" --restored "<restored-root>" --json
```

`migrate-check` verifies durable file hashes and rebuildable vector state. It is a migration integrity check, not a claim that every future workflow has been exercised.

## Locks and Recovery

Write workflows use a root write lock. Inspect and recover locks explicitly:

```powershell
python -B -m kb lock-check --root "<root>" --json
python -B -m kb recover-lock --root "<root>" --manual-confirm --json
```

`recover-lock` must be conservative. A stale or uncertain lock is not automatically safe to delete; recovery requires process, nonce, root, and manual-confirmation checks.

## No Real-User Defaults

Real user-vault draft validate/publish operations require a later exact-path PM operation task with explicit root and item paths. Real retrieval benchmark operations require a later exact-path PM operation task with explicit root and item paths. Backup, restore, migration, and recovery examples in this document are command-shape examples only and do not authorize operations on a real user vault.

For public publication, use the clean snapshot boundary in [Open Source Release](open-source-release.md); backup archives and private runtime roots are not public release artifacts.
