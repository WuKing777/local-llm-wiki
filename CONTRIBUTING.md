# Contributing

Thanks for helping improve Local LLM Wiki. Contributions should keep the project local-first, evidence-gated, and safe for a clean public snapshot.

## Local Editable Setup

Use a public clone placeholder and an editable install:

```powershell
git clone "https://github.com/WuKing777/local-llm-wiki.git" "local-llm-wiki"
cd "local-llm-wiki"
python -B -m pip install -e .
python -B -m kb --help
kb --help
```

Use synthetic demo data for local checks:

```powershell
python -B -m kb doctor --root "examples/demo-root"
python -B -m kb product-console --root "examples/demo-root" --json
python -B -m kb web-console --root "examples/demo-root" --port 0 --no-open
.\tools\run-demo.ps1
```

## Test Expectations

Run the focused public-distribution checks when changing README, product docs, issue templates, or release guidance:

```powershell
python -B -m unittest tests.test_open_source_distribution tests.test_open_source_release tests.test_docs_encoding -v
```

Run broader local checks when changing behavior or before preparing a release candidate:

```powershell
python -B -m unittest discover -s tests -v
```

Documentation changes need matching tests when they introduce new public promises, links, templates, safety boundaries, or release steps. Keep Markdown and YAML UTF-8 encoded, newline-terminated, and free of trailing whitespace.

## Safe Issue and Pull Request Data

Use synthetic demo data or minimal non-sensitive reproduction details. Do not include secrets. Do not include private source text. Do not include raw chunks. Do not include prompts. Do not include full provider responses. Do not include concrete private paths. Do not include real vault artifacts. Do not include private Git history.

Do not claim publication, PyPI availability, hosted service readiness, real-provider readiness, real-vault readiness, or productization approval from local documentation work. Do not publish private Git history.

## Documentation Boundaries

Public docs must state that the product is local-first, no provider is called by default, demo commands use synthetic data, LLM output is never a fact source, and stable wiki claims require local evidence plus validate/publish gates.

Issue templates and examples should ask for command shape, operating system, Python version, expected behavior, actual behavior, and sanitized logs only. Replace any sensitive value with a placeholder before sharing.
