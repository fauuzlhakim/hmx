# Contributing to hmx

Thanks for helping improve hmx.

## Goals
- Keep Hermes state in one real home by default.
- Keep account management explicit and reversible.
- Prefer small, readable patches over clever but fragile hacks.
- Preserve the current Codex overlay behavior unless a change is intentional and tested.
- Keep the repo portable enough for a clean clone, editable install, and CI run.

## Local development workflow
1. Create a feature branch.
2. Set up a virtual environment.
3. Install the project in editable mode with dev dependencies.
4. Run tests and compile checks.
5. If you changed Hermes patch logic, run the patch/smoke flow against a real Hermes install before merging.

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
python3 -m pytest -q
python3 -m py_compile hmx.py hmxlib/*.py
```

## Hermes overlay verification
If you touched the Hermes patching or runtime overlay behavior, also verify:

```bash
hmx patch-hermes
hmx smoke
hmx explain
```

If you updated Hermes itself, prefer:

```bash
hmx update
```

Use `hmx update --skip-smoke` only when you intentionally want to skip the local Codex 429 simulation.

## Style
- Keep command output compact and readable.
- Prefer honest status labels over optimistic defaults.
- Keep provider-specific behavior explicit in the registry.
- Avoid committing cache files, credentials, or generated artifacts.
- Do not hardcode local repo paths in tests.

## Pull request checklist
- Explain what changed.
- Explain why it changed.
- Include exact test/verification commands you ran.
- Mention any migration or operator-facing behavior change.
- Update docs when install or usage behavior changes.
