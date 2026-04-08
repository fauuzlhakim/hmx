import argparse
import base64
import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
from typing import Any

try:
    import yaml  # type: ignore
except Exception:
    yaml = None

ROOT_HOME = Path(os.environ.get('HMX_ROOT_HOME', '/root/.hermes'))
MUX_DIR = ROOT_HOME / 'accounts'
AUTH_DIR = MUX_DIR / 'auth'
REGISTRY_PATH = MUX_DIR / 'registry.json'
LIVE_AUTH_PATH = ROOT_HOME / 'auth.json'
LIVE_AUTH_LOCK = ROOT_HOME / 'auth.lock'
DEFAULT_PROVIDER = 'openai-codex'
BASE_URL = 'https://chatgpt.com/backend-api/codex'
LEGACY_REGISTRY = Path('/root/.config/hermes-mux/registry.json')
LEGACY_HOMES = ['/root/.hermes-b', '/root/.hermes-c']
HERMES_REPO_PATH = Path('/root/.hermes/hermes-agent')
HERMES_RUN_AGENT_PATH = HERMES_REPO_PATH / 'run_agent.py'
HERMES_AUTH_MODULE_PATH = HERMES_REPO_PATH / 'hermes_cli' / 'auth.py'
HMX_BIN_PATH = Path('/root/.local/bin/hmx')
HMX_DEFAULT_SOURCE_PATH = Path('/root/hermes-mux/hmx.py')


def resolve_hmx_source_path(
    current_path: Path | None = None,
    bin_path: Path | None = None,
    default_source_path: Path | None = None,
) -> Path:
    override = os.environ.get('HMX_SOURCE_PATH')
    if override:
        return Path(override).expanduser().resolve()

    current = (current_path or Path(__file__)).expanduser().resolve()
    entrypoint = (bin_path or HMX_BIN_PATH).expanduser().resolve()
    source_fallback = (default_source_path or HMX_DEFAULT_SOURCE_PATH).expanduser().resolve()

    if current == entrypoint and source_fallback.exists():
        return source_fallback
    return current


HMX_SOURCE_PATH = resolve_hmx_source_path()
HERMES_VENV_PYTHON = HERMES_REPO_PATH / 'venv' / 'bin' / 'python'
PROBE_TIMEOUT_SECONDS = 45
PROBE_FRESHNESS_SECONDS = 15 * 60


MODE_PRESETS = {
    'focus': {
        'model.default': 'gpt-5.4',
        'agent.reasoning_effort': 'xhigh',
        'smart_model_routing.enabled': False,
        'smart_model_routing.max_simple_chars': 160,
        'smart_model_routing.max_simple_words': 28,
        'smart_model_routing.cheap_model': {},
    },
    'balanced': {
        'model.default': 'gpt-5.4',
        'agent.reasoning_effort': 'high',
        'smart_model_routing.enabled': True,
        'smart_model_routing.max_simple_chars': 220,
        'smart_model_routing.max_simple_words': 40,
        'smart_model_routing.cheap_model': {
            'provider': 'openai-codex',
            'model': 'gpt-5.4-mini',
        },
    },
    'saver': {
        'model.default': 'gpt-5.4-mini',
        'agent.reasoning_effort': 'medium',
        'smart_model_routing.enabled': False,
        'smart_model_routing.max_simple_chars': 200,
        'smart_model_routing.max_simple_words': 32,
        'smart_model_routing.cheap_model': {},
    },
}
