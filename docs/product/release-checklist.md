# Release Checklist

This checklist prepares a later human-approved public handoff. It does not publish anything. Real user vault data is not uploaded. No cloud or LLM provider is configured or called by default. Cloud or LLM use is off by default.

## History Boundary

- Use a clean snapshot, new public repository, squash import, or equivalent history-safe boundary.
- Do not publish private development Git history.
- Do not copy the private `.git` directory into a public artifact.
- For the new-repository path, confirm `git rev-list --count HEAD` reports exactly one initial commit.
- Do not include management evidence, private worklogs, private review packets, or private source material.

## Export and Scan

- Build the candidate artifact from the approved source state into a separate output directory with `.\tools\create-public-export.ps1 -OutputPath "<public-export-root>" -RepositoryUrl "https://github.com/<owner>/local-llm-wiki.git"`.
- Repository URL placeholders are allowed only in the private source tree; the final public artifact must contain none.
- Run the full export scan over every exported text file, not only changed files.
- Confirm the artifact excludes private roots, generated caches, local databases, build outputs, and private Git metadata.
- Check for secret, path, privacy, and source exposure before external publication.
- Confirm the artifact contains no API keys, bearer tokens, prompts, full provider responses, private source text, raw chunks, concrete private paths, real vault artifacts, or private profile data.

## Account and Credential Gates

- Rotate any provider credential that was disclosed before publication and verify the retired credential is disabled.
- Keep replacement credentials outside files, reports, issues, commits, and command transcripts.
- Run `gh auth status` and verify the intended GitHub account.
- Confirm the approved owner, repository name, visibility, and exact exported commit.

## Local/Offline Evidence

- Capture local/offline test evidence for README clone-and-run commands, module help, console script help, synthetic doctor, synthetic product-console, and synthetic web-console smoke checks.
- Run focused public-distribution tests:

```powershell
python -B -m unittest tests.test_open_source_distribution tests.test_open_source_release tests.test_docs_encoding -v
```

- Run public export tests:

```powershell
python -B -m unittest tests.test_public_export -v
```

- Run broader tests when the release owner requires them:

```powershell
python -B -m unittest discover -s tests -v
```

## Human Approval

- Obtain explicit human approval before external publication.
- Confirm the approval names the artifact boundary and does not authorize private-history publication.
- No publication command is authorized by this checklist.
- This checklist does not create a repository, push to GitHub, publish to PyPI, certify real-provider operation, certify real-vault operation, or grant productization approval.
