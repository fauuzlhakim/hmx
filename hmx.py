#!/usr/bin/env python3
import importlib
import importlib.util
import subprocess
import sys
from pathlib import Path

from hmxlib import account_health as _account_health
from hmxlib import account_store as _account_store
from hmxlib import cli as _cli
from hmxlib import commands as _commands
from hmxlib import hermes_patch as _hermes_patch
from hmxlib import runtime as _runtime
from hmxlib import utils as _utils

_runtime = importlib.reload(_runtime)
_utils = importlib.reload(_utils)
_account_store = importlib.reload(_account_store)
_account_health = importlib.reload(_account_health)
_hermes_patch = importlib.reload(_hermes_patch)
_commands = importlib.reload(_commands)
_cli = importlib.reload(_cli)

_SYNC_MODULES = [_runtime, _utils, _account_store, _account_health, _hermes_patch, _commands, _cli]
_SYNC_NAMES = [
    'ROOT_HOME', 'MUX_DIR', 'AUTH_DIR', 'REGISTRY_PATH', 'LIVE_AUTH_PATH', 'LIVE_AUTH_LOCK',
    '__version__', 'HOME_DIR', 'DEFAULT_PROVIDER', 'BASE_URL', 'LEGACY_REGISTRY', 'LEGACY_HOMES', 'HERMES_REPO_PATH',
    'HERMES_RUN_AGENT_PATH', 'HERMES_AUTH_MODULE_PATH', 'HERMES_CODEX_ACCOUNT_REGISTRY_PATH', 'HMX_BIN_PATH', 'HMX_DEFAULT_SOURCE_PATH',
    'HMX_SOURCE_PATH', 'HERMES_VENV_PYTHON', 'PROBE_TIMEOUT_SECONDS', 'PROBE_FRESHNESS_SECONDS',
    'MODE_PRESETS', 'yaml',
    'err', 'ensure_dirs', 'utcnow', 'now_iso', 'slugify', 'deep_get', 'deep_set', 'load_registry',
    'save_registry', 'load_yaml', 'dump_yaml', 'ensure_base_config', 'read_auth_file',
    'ensure_account_store_shape', 'auth_payload_summary', 'hermes_python', 'probe_result_age_seconds',
    'fresh_probe_result', 'run_account_probe', 'apply_probe_result', 'probe_accounts', 'account_target',
    'active_target', 'sync_live_auth_symlink', 'ensure_auth_lock', 'ordered_accounts', 'metadata_text',
    'note_text', 'effective_account_summary', 'parse_iso8601_utc', 'FIVE_HOURS_SECONDS',
    'format_duration_compact', 'format_percent_compact', 'five_hour_window_remaining',
    'describe_account_health', 'build_list_rows', 'render_table', 'list_summary_text', 'next_account',
    'hermes_cmd', 'normalize_hermes_args', 'run_hermes', 'import_auth_file', 'migrate_from_existing',
    'doctor_status', 'patch_run_agent', 'patch_auth_store_symlink_preservation', 'unpatch_run_agent',
    '_account_activation_guard', 'cmd_init', 'cmd_list', 'cmd_probe', 'cmd_rename', 'cmd_annotate', 'cmd_use', 'cmd_hop', 'cmd_import',
    'cmd_capture', 'cmd_add', 'cmd_login', 'cmd_remove', 'cmd_disable', 'cmd_enable', 'cmd_current',
    'cmd_mode', 'cmd_auto', 'cmd_doctor', 'describe_run_agent_patch', 'cmd_explain',
    'verify_run_agent_compile', 'verify_auth_module_compile', 'verify_codex_account_registry_compile', 'repair_live_auth_from_registry',
    'ensure_codex_account_registry_helper', 'ensure_hmx_entrypoint_wrapper', 'run_codex_rotation_smoke_test', 'cmd_patch_hermes', 'cmd_smoke',
    'cmd_update_hermes', 'cmd_unpatch_hermes', 'build_parser', 'main',
]


def _sync_globals():
    current = globals()
    for module in _SYNC_MODULES:
        for name in _SYNC_NAMES:
            if name not in current:
                continue
            value = current[name]
            if getattr(value, '_hmx_wrapper', False):
                continue
            setattr(module, name, value)


def _wrap(module, name):
    target = getattr(module, name)

    def _wrapped(*args, **kwargs):
        _sync_globals()
        return target(*args, **kwargs)

    _wrapped.__name__ = name
    _wrapped.__doc__ = target.__doc__
    _wrapped._hmx_wrapper = True
    return _wrapped

for _name in [
    'ROOT_HOME', 'MUX_DIR', 'AUTH_DIR', 'REGISTRY_PATH', 'LIVE_AUTH_PATH', 'LIVE_AUTH_LOCK',
    '__version__', 'HOME_DIR', 'DEFAULT_PROVIDER', 'BASE_URL', 'LEGACY_REGISTRY', 'LEGACY_HOMES', 'HERMES_REPO_PATH',
    'HERMES_RUN_AGENT_PATH', 'HERMES_AUTH_MODULE_PATH', 'HERMES_CODEX_ACCOUNT_REGISTRY_PATH', 'HMX_BIN_PATH', 'HMX_DEFAULT_SOURCE_PATH',
    'HMX_SOURCE_PATH', 'HERMES_VENV_PYTHON', 'PROBE_TIMEOUT_SECONDS', 'PROBE_FRESHNESS_SECONDS',
    'MODE_PRESETS', 'yaml',
]:
    globals()[_name] = getattr(_runtime, _name)

subprocess = subprocess
importlib = importlib
Path = Path
sys = sys

for _module, _names in [
    (_runtime, ['resolve_hmx_source_path']),
    (_utils, ['err', 'utcnow', 'now_iso', 'slugify', 'deep_get', 'deep_set', 'parse_iso8601_utc', 'format_duration_compact', 'format_percent_compact']),
    (_account_store, ['ensure_dirs', 'load_registry', 'save_registry', 'load_yaml', 'dump_yaml', 'ensure_base_config', 'read_auth_file', 'ensure_account_store_shape', 'auth_payload_summary', 'account_target', 'active_target', 'sync_live_auth_symlink', 'ensure_auth_lock', 'import_auth_file', 'migrate_from_existing', 'repair_live_auth_from_registry']),
    (_account_health, ['hermes_python', 'probe_result_age_seconds', 'fresh_probe_result', 'run_account_probe', 'apply_probe_result', 'probe_accounts', 'ordered_accounts', 'metadata_text', 'note_text', 'effective_account_summary', 'FIVE_HOURS_SECONDS', 'five_hour_window_remaining', 'describe_account_health', 'build_list_rows', 'render_table', 'list_summary_text', 'next_account', 'doctor_status']),
    (_hermes_patch, ['patch_run_agent', 'patch_auth_store_symlink_preservation', 'unpatch_run_agent', 'describe_run_agent_patch', 'verify_run_agent_compile', 'verify_auth_module_compile', 'verify_codex_account_registry_compile', 'ensure_codex_account_registry_helper', 'ensure_hmx_entrypoint_wrapper', 'run_codex_rotation_smoke_test']),
    (_commands, ['hermes_cmd', 'normalize_hermes_args', 'run_hermes', '_account_activation_guard', 'cmd_init', 'cmd_list', 'cmd_probe', 'cmd_rename', 'cmd_annotate', 'cmd_use', 'cmd_hop', 'cmd_import', 'cmd_capture', 'cmd_add', 'cmd_login', 'cmd_remove', 'cmd_disable', 'cmd_enable', 'cmd_current', 'cmd_mode', 'cmd_auto', 'cmd_doctor', 'cmd_explain', 'cmd_patch_hermes', 'cmd_smoke', 'cmd_update_hermes', 'cmd_unpatch_hermes']),
    (_cli, ['build_parser', 'main']),
]:
    for _name in _names:
        globals()[_name] = _wrap(_module, _name)

if __name__ == '__main__':
    raise SystemExit(main())
