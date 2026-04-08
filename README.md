# hmx

![CI](https://github.com/fauuzlhakim/hmx/actions/workflows/ci.yml/badge.svg)
![License](https://img.shields.io/github/license/fauuzlhakim/hmx)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

hmx is a small CLI that lets one Hermes installation manage multiple auth slots without splitting state across multiple `~/.hermes-*` homes.

It keeps one real Hermes home, stores multiple auth files in a pooled account registry, and switches the live `auth.json` symlink between accounts. It also includes helpers for patching Hermes so a Codex `usage_limit_reached` condition can rotate to the next available account automatically.

## What this solves

Without hmx, a multi-account workflow often turns into multiple separate Hermes homes such as `~/.hermes`, `~/.hermes-b`, and `~/.hermes-c`. That makes updates, sessions, and maintenance messy.

hmx keeps the stateful Hermes data in one place and moves account switching into a small overlay:

- one real Hermes home: `~/.hermes` by default
- pooled auth files under `~/.hermes/accounts/auth/`
- registry metadata under `~/.hermes/accounts/registry.json`
- live auth selected via `~/.hermes/auth.json` symlink

## Scope and assumptions

This project is intentionally opinionated.

- target environment: Linux
- target workflow: local/self-managed Hermes installations
- default provider flow today: OpenAI Codex auth pooling
- this is an overlay for an existing Hermes install, not a replacement for Hermes itself

If your Hermes repo lives somewhere other than the default path, hmx can still work, but the patch-related commands assume a standard layout unless you override the relevant environment or edit the runtime configuration.

## Features

- list pooled accounts with provider, status, cooldown, and reset hints
- switch or hop between accounts
- import an existing `auth.json` into the pool
- capture the current live auth into a named slot
- annotate accounts with labels, notes, roles, and priority
- disable or re-enable accounts without deleting them
- patch Hermes so limit-triggered rotation can happen automatically
- run smoke checks after patching
- apply cost/quality presets for Hermes config

## Requirements

- Python 3.10+
- Linux
- an existing Hermes installation available on `PATH` if you want `hmx use`, `hmx login`, or update/patch workflows
- `PyYAML` for config editing features

## Install

### Option A: editable install for development

```bash
git clone https://github.com/YOUR-USER/hmx.git
cd hmx
python3 -m venv .venv
. .venv/bin/activate
pip install -e .[dev]
```

### Option B: local install without venv

```bash
git clone https://github.com/YOUR-USER/hmx.git
cd hmx
pip install .
```

After installation, the `hmx` command will be available from the console entry point defined in `pyproject.toml`.

## Quick start

Show the CLI help:

```bash
hmx --help
```

Initialize or migrate existing auths into the pooled layout:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx init
```

List known accounts:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx list
```

Import an auth file into a named slot and activate it:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx import /path/to/auth.json main --activate
```

Switch to an account:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx use main
```

Hop to the next enabled account:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx hop
```

Capture the currently active live auth into a new slot:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx capture backup-alt
```

Annotate an account for readability:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx annotate backup-alt \
  --label "Backup Alt" \
  --role backup \
  --note "Use when primary hits limit" \
  --priority 5
```

Turn auto-rotation on or off:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx auto on
HMX_ROOT_HOME="$HOME/.hermes" hmx auto off
```

## Cost/quality modes

hmx can update Hermes config presets:

- `balanced`: `gpt-5.4` with high reasoning and simple-prompt routing to `gpt-5.4-mini`
- `focus`: `gpt-5.4` with `xhigh` reasoning
- `saver`: `gpt-5.4-mini` with medium reasoning

Examples:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx mode balanced
HMX_ROOT_HOME="$HOME/.hermes" hmx mode focus
HMX_ROOT_HOME="$HOME/.hermes" hmx mode saver
```

## Patch workflow

Patch Hermes so a Codex account can rotate automatically after a limit-triggering error:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx patch-hermes
```

Run the local smoke simulation:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx smoke
```

Update Hermes, reapply the patch, and smoke test in one command:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx update
```

Skip the smoke phase only if you are intentionally doing so:

```bash
HMX_ROOT_HOME="$HOME/.hermes" hmx update --skip-smoke
```

## Common commands

```bash
hmx list
hmx list --probe
hmx current
hmx doctor
hmx use main
hmx use main -c
hmx hop -c
hmx add research
hmx login research
hmx rename research research-alt
hmx remove research-alt --purge
hmx disable research-alt
hmx enable research-alt
hmx explain
hmx unpatch-hermes
```

## Environment variables

- `HMX_ROOT_HOME`: Hermes home to manage. Defaults to `~/.hermes`.
- `HMX_HERMES_REPO_PATH`: override the upstream Hermes checkout path used by patch/update commands.
- `HMX_BIN_PATH`: override the wrapper install path for the generated `hmx` launcher.
- `HMX_SOURCE_PATH`: optional override for resolving the source file used by the entrypoint wrapper logic.
- `HMX_DEFAULT_SOURCE_PATH`: optional fallback source path for wrapper generation.
- `HMX_LEGACY_REGISTRY`: override legacy migration registry location.
- `HMX_LEGACY_HOMES`: colon-separated list of legacy Hermes homes to inspect during migration.

## Testing

Run the test suite:

```bash
python3 -m pytest -q
```

Verify the modules compile:

```bash
python3 -m py_compile hmx.py hmxlib/*.py
```

## Project layout

```text
hmx.py                  CLI entrypoint module
hmxlib/                 implementation modules
tests/                  pytest suite
pyproject.toml          packaging and console-script metadata
README.md               public documentation
CONTRIBUTING.md         contributor workflow
```

## Example output

`hmx list` is designed to stay compact and operator-friendly:

```text
ACCOUNT      PROVIDER      STATUS     5H   RESET   DETAIL
main         openai-codex  active     -    -       currently selected
backup-alt   openai-codex  limited    30%  1h30m   usage limit
research     openai-codex  available  -    -       live probe ok

active: main
auto-rotate: on
```

## Limitations

- patch/update commands are coupled to the upstream Hermes repository layout expected by this project
- the auto-rotation logic is currently centered on Codex account behavior
- this repository does not bundle Hermes itself
- public production-hardening is out of scope; this is an operator tool for trusted environments

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

See [CHANGELOG.md](CHANGELOG.md).

## License

MIT
