# Contributing to hmx

Thanks for helping improve hmx.

## Goals
- Keep Hermes state in one real home: `/root/.hermes`
- Keep account management explicit and reversible
- Prefer small, readable patches over clever but fragile hacks
- Preserve the current Codex overlay behavior unless a change is intentional and tested

## Workflow
1. Make changes on a feature branch.
2. Run the test suite:
   - `python3 -m pytest tests/test_hmx_list.py -q`
3. Verify the CLI still compiles:
   - `python3 -m py_compile hmx.py`
4. If you touched the Hermes overlay, run:
   - `hmx patch-hermes`
   - `hmx smoke`
   - `hmx explain`
5. If you updated Hermes itself, use:
   - `hmx update`
   - use `hmx update --skip-smoke` only when you intentionally want to skip the local Codex 429 simulation

## Style
- Keep command output compact and readable.
- Prefer honest status labels over optimistic defaults.
- Keep provider-specific behavior explicit in the registry.
- Avoid committing cache files or generated artifacts.

## What to include in a PR
- What changed
- Why it changed
- How you tested it
- Any migration or update notes
