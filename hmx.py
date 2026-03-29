#!/usr/bin/env python3
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
HMX_SOURCE_PATH = Path(__file__).resolve()
HMX_BIN_PATH = Path('/root/.local/bin/hmx')

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


def err(msg: str, exit_code: int = 1) -> None:
    print(f'error: {msg}', file=sys.stderr)
    raise SystemExit(exit_code)


def ensure_dirs() -> None:
    AUTH_DIR.mkdir(parents=True, exist_ok=True)


def utcnow() -> dt.datetime:
    return dt.datetime.utcnow().replace(microsecond=0)


def now_iso() -> str:
    return utcnow().isoformat() + 'Z'


def slugify(text: str) -> str:
    out = []
    for ch in text.lower():
        if ch.isalnum():
            out.append(ch)
        elif ch in {'@', '.', '-', '_', '+'}:
            out.append('-')
    s = ''.join(out).strip('-')
    while '--' in s:
        s = s.replace('--', '-')
    return s or 'account'


def deep_get(data: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def deep_set(data: dict[str, Any], dotted: str, value: Any) -> None:
    cur = data
    parts = dotted.split('.')
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value


def load_registry() -> dict[str, Any]:
    ensure_dirs()
    if REGISTRY_PATH.exists():
        data = json.loads(REGISTRY_PATH.read_text())
        if isinstance(data, dict):
            data.setdefault('schema', 2)
            data.setdefault('active', None)
            data.setdefault('accounts', {})
            data.setdefault('auto_switch_on_limit', True)
            data.setdefault('root_home', str(ROOT_HOME))
            for info in data.get('accounts', {}).values():
                if isinstance(info, dict):
                    info.setdefault('provider', DEFAULT_PROVIDER)
            return data
    return {
        'schema': 2,
        'root_home': str(ROOT_HOME),
        'active': None,
        'accounts': {},
        'auto_switch_on_limit': True,
        'last_mode': None,
        'updated_at': now_iso(),
    }


def save_registry(registry: dict[str, Any]) -> None:
    ensure_dirs()
    registry['updated_at'] = now_iso()
    REGISTRY_PATH.write_text(json.dumps(registry, indent=2, sort_keys=True) + '\n')


def load_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        err('PyYAML is not available; cannot edit config.yaml')
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def dump_yaml(path: Path, data: dict[str, Any]) -> None:
    if yaml is None:
        err('PyYAML is not available; cannot write config.yaml')
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    path.write_text(text)


def ensure_base_config() -> None:
    cfg_path = ROOT_HOME / 'config.yaml'
    cfg = load_yaml(cfg_path)
    changed = False
    for dotted, value in {
        'model.default': 'gpt-5.4',
        'model.provider': 'openai-codex',
        'model.base_url': BASE_URL,
        'agent.reasoning_effort': 'high',
        'smart_model_routing.enabled': True,
        'smart_model_routing.max_simple_chars': 220,
        'smart_model_routing.max_simple_words': 40,
        'smart_model_routing.cheap_model': {'provider': 'openai-codex', 'model': 'gpt-5.4-mini'},
    }.items():
        if deep_get(cfg, dotted) != value:
            deep_set(cfg, dotted, value)
            changed = True
    if changed or not cfg_path.exists():
        dump_yaml(cfg_path, cfg)


def read_auth_file(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def ensure_account_store_shape(data: dict[str, Any]) -> dict[str, Any]:
    if 'tokens' in data and isinstance(data.get('tokens'), dict):
        return {
            'version': 1,
            'providers': {
                'openai-codex': {
                    'tokens': data['tokens'],
                    'last_refresh': data.get('last_refresh'),
                    'auth_mode': data.get('auth_mode', 'chatgpt'),
                }
            },
            'active_provider': 'openai-codex',
            'updated_at': now_iso(),
        }
    return data


def auth_payload_summary(data: dict[str, Any] | None) -> dict[str, str]:
    if not data:
        return {'status': 'missing'}
    active_provider = DEFAULT_PROVIDER
    provider = None
    if 'tokens' in data:
        provider = data
    else:
        providers = data.get('providers', {}) if isinstance(data.get('providers'), dict) else {}
        active_provider = str(data.get('active_provider') or DEFAULT_PROVIDER)
        provider = providers.get(active_provider)
    if not isinstance(provider, dict):
        return {'status': 'invalid'}
    tokens = provider.get('tokens', {}) if isinstance(provider.get('tokens'), dict) else {}
    access_token = str(tokens.get('access_token') or '')
    account_id = str(tokens.get('account_id') or '')
    id_token = str(tokens.get('id_token') or '')
    email = 'unknown'
    plan = 'unknown'
    try:
        token_source = id_token if id_token and '.' in id_token else access_token
        if token_source and '.' in token_source:
            payload = token_source.split('.')[1]
            payload += '=' * (-len(payload) % 4)
            decoded = json.loads(base64.urlsafe_b64decode(payload.encode()).decode())
            email = decoded.get('email', decoded.get('https://api.openai.com/profile', {}).get('email', 'unknown'))
            auth = decoded.get('https://api.openai.com/auth', {})
            if isinstance(auth, dict):
                plan = auth.get('chatgpt_plan_type', 'unknown')
                if not account_id:
                    account_id = str(auth.get('chatgpt_account_id') or auth.get('chatgpt_account_user_id') or '')
    except Exception:
        pass
    return {
        'status': 'ok' if access_token else 'empty',
        'provider': active_provider,
        'email': email,
        'plan': plan,
        'account_suffix': account_id[-8:] if account_id else 'unknown',
        'has_refresh': 'yes' if tokens.get('refresh_token') else 'no',
    }


def account_target(alias: str, registry: dict[str, Any]) -> Path:
    acct = registry.get('accounts', {}).get(alias)
    if not acct:
        err(f"unknown account '{alias}'")
    return AUTH_DIR / acct['file']


def active_target(registry: dict[str, Any]) -> Path | None:
    active = registry.get('active')
    if not active:
        return None
    acct = registry.get('accounts', {}).get(active)
    if not acct:
        return None
    return AUTH_DIR / acct['file']


def sync_live_auth_symlink(registry: dict[str, Any]) -> None:
    target = active_target(registry)
    if target is None:
        return
    ensure_dirs()
    if LIVE_AUTH_PATH.is_symlink() and LIVE_AUTH_PATH.resolve() == target.resolve():
        return
    if LIVE_AUTH_PATH.exists() or LIVE_AUTH_PATH.is_symlink():
        backup = LIVE_AUTH_PATH.with_name(f'auth.json.pre-hmx-{dt.datetime.now().strftime("%Y%m%d-%H%M%S")}.bak')
        if not backup.exists():
            LIVE_AUTH_PATH.rename(backup)
            print(f'backed up {LIVE_AUTH_PATH} -> {backup}')
        elif LIVE_AUTH_PATH.exists() or LIVE_AUTH_PATH.is_symlink():
            LIVE_AUTH_PATH.unlink()
    LIVE_AUTH_PATH.symlink_to(target)


def ensure_auth_lock() -> None:
    LIVE_AUTH_LOCK.touch(exist_ok=True)


def ordered_accounts(registry: dict[str, Any], include_disabled: bool = False) -> list[str]:
    rows = []
    for alias, info in registry.get('accounts', {}).items():
        if not include_disabled and info.get('disabled'):
            continue
        rows.append((int(info.get('priority', 100)), alias))
    rows.sort()
    return [alias for _, alias in rows]


def metadata_text(info: dict[str, Any]) -> str:
    parts = []
    if info.get('label'):
        parts.append(f"label={info['label']}")
    if info.get('role'):
        parts.append(f"role={info['role']}")
    return ' '.join(parts)


def note_text(info: dict[str, Any], limit: int = 96) -> str:
    note = str(info.get('note') or '').strip()
    if not note:
        return ''
    if len(note) <= limit:
        return note
    return note[: limit - 3] + '...'


def effective_account_summary(info: dict[str, Any], auth_path: Path) -> dict[str, str]:
    summary = auth_payload_summary(read_auth_file(auth_path))
    email = summary.get('email') or 'unknown'
    plan = summary.get('plan') or 'unknown'
    provider = summary.get('provider') or str(info.get('provider') or DEFAULT_PROVIDER)
    if email == 'unknown' and info.get('email'):
        email = str(info.get('email'))
    if plan == 'unknown' and info.get('plan'):
        plan = str(info.get('plan'))
    summary['email'] = email
    summary['plan'] = plan
    summary['provider'] = provider
    return summary


def parse_iso8601_utc(value: Any) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.endswith('Z'):
            text = text[:-1] + '+00:00'
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(dt.timezone.utc).replace(tzinfo=None)
    return parsed


FIVE_HOURS_SECONDS = 5 * 3600


def format_duration_compact(seconds: int | float | None) -> str:
    if seconds is None:
        return '-'
    remaining = max(0, int(seconds))
    hours, rem = divmod(remaining, 3600)
    minutes, _ = divmod(rem, 60)
    if hours and minutes:
        return f'{hours}h{minutes:02}m'
    if hours:
        return f'{hours}h'
    return f'{minutes}m'


def format_percent_compact(value: float | None) -> str:
    if value is None:
        return '-'
    pct = max(0.0, min(100.0, float(value)))
    return f'{int(round(pct))}%'


def five_hour_window_remaining(limit: dict[str, Any], now: dt.datetime) -> str:
    state = str(limit.get('state') or '').strip().lower()
    if state != 'limited':
        return '-'

    reset_at = parse_iso8601_utc(limit.get('reset_at'))
    if reset_at is None or reset_at <= now:
        return '-'

    seconds_left = limit.get('resets_in_seconds')
    if not isinstance(seconds_left, (int, float)) or seconds_left <= 0:
        seconds_left = int((reset_at - now).total_seconds())

    if seconds_left <= 0 or seconds_left > FIVE_HOURS_SECONDS:
        return '-'

    return format_percent_compact((seconds_left / FIVE_HOURS_SECONDS) * 100)


def describe_account_health(alias: str, info: dict[str, Any], auth_path: Path, now: dt.datetime | None = None) -> dict[str, str]:
    now = now or utcnow()
    summary = effective_account_summary(info, auth_path)
    limit = info.get('limit') if isinstance(info.get('limit'), dict) else {}
    state = str(limit.get('state') or '').strip().lower()
    reset_at = parse_iso8601_utc(limit.get('reset_at'))
    reset = '-'
    five_h = five_hour_window_remaining(limit, now)

    if state == 'limited' and reset_at is None:
        return {
            'status': 'limited',
            'detail': 'usage limit',
            'reset': reset,
            'five_h': five_h,
            'auth_status': summary.get('status', '-'),
        }

    if reset_at and reset_at > now:
        seconds_left = int((reset_at - now).total_seconds())
        return {
            'status': 'limited',
            'detail': 'usage limit',
            'reset': format_duration_compact(seconds_left),
            'five_h': five_h,
            'auth_status': summary.get('status', '-'),
        }

    if info.get('disabled'):
        return {
            'status': 'disabled',
            'detail': 'manually disabled',
            'reset': reset,
            'five_h': five_h,
            'auth_status': summary.get('status', '-'),
        }
    if not auth_path.exists():
        return {
            'status': 'missing',
            'detail': 'auth file missing',
            'reset': reset,
            'five_h': five_h,
            'auth_status': summary.get('status', '-'),
        }
    auth_status = summary.get('status', '-')
    if auth_status == 'invalid':
        return {'status': 'invalid', 'detail': 'auth unreadable', 'reset': reset, 'five_h': five_h, 'auth_status': auth_status}
    if auth_status == 'missing':
        return {'status': 'missing', 'detail': 'auth missing', 'reset': reset, 'five_h': five_h, 'auth_status': auth_status}
    if auth_status == 'empty':
        return {'status': 'empty', 'detail': 'token missing', 'reset': reset, 'five_h': five_h, 'auth_status': auth_status}
    return {
        'status': 'unknown',
        'detail': 'no limit telemetry',
        'reset': reset,
        'five_h': five_h,
        'auth_status': auth_status,
    }


def build_list_rows(registry: dict[str, Any], include_disabled: bool = False, now: dt.datetime | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    now = now or utcnow()
    active_alias = registry.get('active')
    for alias in ordered_accounts(registry, include_disabled=include_disabled):
        info = registry['accounts'][alias]
        auth_path = AUTH_DIR / info['file']
        summary = effective_account_summary(info, auth_path)
        health = describe_account_health(alias, info, auth_path, now=now)
        rows.append(
            {
                'current': '*' if alias == active_alias else '',
                'account': alias,
                'provider': summary.get('provider', DEFAULT_PROVIDER) or DEFAULT_PROVIDER,
                'plan': summary.get('plan', '-') or '-',
                'status': health['status'],
                'five_h': health['five_h'],
                'reset': health['reset'],
                'priority': str(info.get('priority', '-')),
                'email': summary.get('email', '-') or '-',
            }
        )
    return rows


def render_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))
    header_line = '  '.join(header.ljust(widths[idx]) for idx, header in enumerate(headers))
    divider_line = '  '.join('-' * widths[idx] for idx in range(len(headers)))
    body_lines = [
        '  '.join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))
        for row in rows
    ]
    return [header_line, divider_line, *body_lines]


def list_summary_text(registry: dict[str, Any], rows: list[dict[str, str]]) -> list[str]:
    total = len(registry.get('accounts', {}))
    enabled = sum(1 for info in registry.get('accounts', {}).values() if not info.get('disabled'))
    disabled = total - enabled
    limited = sum(1 for row in rows if row['status'] == 'limited')
    unhealthy = sum(1 for row in rows if row['status'] not in {'healthy', 'limited'})
    auto_rotate = 'on' if registry.get('auto_switch_on_limit', True) else 'off'
    return [
        f'account-pool: total={total} enabled={enabled} disabled={disabled} limited={limited} issues={unhealthy}',
        f'active: {registry.get("active") or "-"}  auto-rotate: {auto_rotate}  root: {ROOT_HOME}',
    ]


def next_account(registry: dict[str, Any], current: str | None = None) -> str:
    names = ordered_accounts(registry)
    if not names:
        err('no active accounts')
    cur = current or registry.get('active')
    if cur not in names:
        return names[0]
    idx = names.index(cur)
    return names[(idx + 1) % len(names)]


def hermes_cmd() -> list[str]:
    return [shutil.which('hermes') or 'hermes']


def normalize_hermes_args(args: list[str]) -> list[str]:
    if args and args[0] == '--':
        return args[1:]
    return args


def run_hermes(extra_args: list[str], replace: bool = True) -> int:
    cmd = hermes_cmd() + normalize_hermes_args(extra_args)
    if replace:
        os.execvp(cmd[0], cmd)
        return 0
    proc = subprocess.run(cmd)
    return proc.returncode


def import_auth_file(registry: dict[str, Any], source: Path, alias: str | None = None, make_active: bool = False) -> str:
    data = read_auth_file(source)
    if not data:
        err(f'could not read auth json from {source}')
    data = ensure_account_store_shape(data)
    summary = auth_payload_summary(data)
    derived = summary.get('email') or source.stem
    alias = alias or slugify(derived)
    filename = f'{alias}.json'
    target = AUTH_DIR / filename
    target.write_text(json.dumps(data, indent=2) + '\n')
    registry.setdefault('accounts', {})[alias] = {
        'file': filename,
        'provider': summary.get('provider', DEFAULT_PROVIDER) or DEFAULT_PROVIDER,
        'email': summary.get('email', 'unknown'),
        'plan': summary.get('plan', 'unknown'),
        'added_at': now_iso(),
        'last_selected_at': registry.get('accounts', {}).get(alias, {}).get('last_selected_at'),
        'priority': registry.get('accounts', {}).get(alias, {}).get('priority', len(registry.get('accounts', {})) + 1),
        'source': str(source),
        'disabled': False,
    }
    if make_active or not registry.get('active'):
        registry['active'] = alias
    return alias


def migrate_from_existing(registry: dict[str, Any]) -> list[str]:
    imported = []
    seen_aliases = set(registry.get('accounts', {}).keys())
    candidates: list[tuple[str | None, Path]] = []
    if LIVE_AUTH_PATH.exists() and not LIVE_AUTH_PATH.is_symlink():
        candidates.append(('main', LIVE_AUTH_PATH))
    for legacy in LEGACY_HOMES:
        p = Path(legacy) / 'auth.json'
        if p.exists():
            candidates.append((Path(legacy).name.replace('.hermes-', ''), p))
    if LEGACY_REGISTRY.exists():
        try:
            legacy = json.loads(LEGACY_REGISTRY.read_text())
            for alias, info in legacy.get('accounts', {}).items():
                home = Path(info.get('home', ''))
                p = home / 'auth.json'
                if p.exists():
                    candidates.append((alias, p))
        except Exception:
            pass
    for suggested_alias, path in candidates:
        try:
            resolved = path.resolve()
        except Exception:
            resolved = path
        if resolved.is_relative_to(AUTH_DIR) if hasattr(resolved, 'is_relative_to') else str(resolved).startswith(str(AUTH_DIR)):
            continue
        alias = suggested_alias or path.parent.name
        alias = slugify(alias)
        if alias in seen_aliases:
            base = alias
            i = 2
            while f'{base}-{i}' in seen_aliases:
                i += 1
            alias = f'{base}-{i}'
        imported_alias = import_auth_file(registry, path, alias=alias, make_active=(registry.get('active') is None))
        seen_aliases.add(imported_alias)
        imported.append(imported_alias)
    return imported


def doctor_status(registry: dict[str, Any]) -> int:
    bad = False
    print(f'root_home: {ROOT_HOME}')
    print(f'auth_dir: {AUTH_DIR}')
    print(f'registry: {REGISTRY_PATH}')
    print(f'live_auth: {LIVE_AUTH_PATH}')
    print(f'live_auth_link: {LIVE_AUTH_PATH.is_symlink()}')
    if LIVE_AUTH_PATH.is_symlink():
        print(f'live_auth_target: {LIVE_AUTH_PATH.resolve()}')
    print('')
    for alias in ordered_accounts(registry, include_disabled=True):
        info = registry['accounts'][alias]
        target = AUTH_DIR / info['file']
        summary = effective_account_summary(info, target)
        active = '*' if alias == registry.get('active') else ' '
        disabled = ' disabled' if info.get('disabled') else ''
        print(f"{active} {alias:12} file={info['file']} provider={summary.get('provider','-'):13} status={summary.get('status','-'):7} plan={summary.get('plan','-'):7} email={summary.get('email','-')}{disabled}")
        meta = metadata_text(info)
        if meta:
            print(f"   {'':12} {meta}")
        note = note_text(info)
        if note:
            print(f"   {'':12} note={note}")
        if not target.exists():
            bad = True
    if not LIVE_AUTH_PATH.is_symlink():
        bad = True
    return 1 if bad else 0


def patch_run_agent() -> None:
    path = HERMES_RUN_AGENT_PATH
    text = path.read_text()
    marker = '    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:\n'
    injected_block = '''    def _extract_codex_limit_metadata(self, error_text: str = "") -> dict:\n        meta = {}\n        if not error_text:\n            return meta\n\n        payload = None\n        try:\n            import ast as _ast\n            start = error_text.find("{")\n            end = error_text.rfind("}")\n            if start != -1 and end != -1 and end > start:\n                payload = _ast.literal_eval(error_text[start:end + 1])\n        except Exception:\n            payload = None\n\n        if isinstance(payload, dict) and isinstance(payload.get("error"), dict):\n            payload = payload.get("error")\n\n        if isinstance(payload, dict):\n            resets_in_seconds = payload.get("resets_in_seconds")\n            if isinstance(resets_in_seconds, int) and resets_in_seconds > 0:\n                meta["resets_in_seconds"] = resets_in_seconds\n            resets_at = payload.get("resets_at")\n            if isinstance(resets_at, int) and resets_at > 0:\n                meta["resets_at"] = resets_at\n            plan_type = payload.get("plan_type")\n            if isinstance(plan_type, str) and plan_type.strip():\n                meta["plan_type"] = plan_type.strip()\n\n        if meta:\n            return meta\n\n        import re as _re\n        patterns = {\n            "resets_in_seconds": r"resets_in_seconds[^0-9]*(\\d+)",\n            "resets_at": r"resets_at[^0-9]*(\\d+)",\n            "plan_type": r"plan_type[^a-zA-Z0-9]*['\\"]?([a-zA-Z0-9_-]+)",\n        }\n        for key, pattern in patterns.items():\n            match = _re.search(pattern, error_text)\n            if not match:\n                continue\n            value = match.group(1)\n            if key in {"resets_in_seconds", "resets_at"}:\n                try:\n                    meta[key] = int(value)\n                except Exception:\n                    pass\n            elif value:\n                meta[key] = value\n        return meta\n\n    def _record_codex_account_limit_state(self, registry_path, registry, current_alias, *, limit_meta=None):\n        accounts = registry.get("accounts") if isinstance(registry.get("accounts"), dict) else {}\n        acct = accounts.get(current_alias)\n        if not isinstance(acct, dict):\n            return\n        import datetime as _dt\n        observed_at = _dt.datetime.utcnow().replace(microsecond=0)\n        limit_meta = limit_meta if isinstance(limit_meta, dict) else {}\n        payload = {\n            "state": "limited",\n            "observed_at": observed_at.isoformat() + "Z",\n            "source": "codex_429",\n        }\n        plan_type = limit_meta.get("plan_type")\n        if isinstance(plan_type, str) and plan_type.strip():\n            payload["plan_type"] = plan_type.strip()\n\n        resets_at = limit_meta.get("resets_at")\n        if isinstance(resets_at, int) and resets_at > 0:\n            payload["resets_at"] = resets_at\n            payload["reset_at"] = _dt.datetime.utcfromtimestamp(resets_at).replace(microsecond=0).isoformat() + "Z"\n\n        resets_in_seconds = limit_meta.get("resets_in_seconds")\n        if isinstance(resets_in_seconds, int) and resets_in_seconds > 0:\n            payload["resets_in_seconds"] = resets_in_seconds\n            payload.setdefault("reset_at", (observed_at + _dt.timedelta(seconds=resets_in_seconds)).isoformat() + "Z")\n\n        acct["limit"] = payload\n        registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\\n")\n\n    def _try_rotate_codex_account_on_limit(self, *, error_text: str = "") -> bool:\n        if self.api_mode != "codex_responses" or self.provider != "openai-codex":\n            return False\n\n        registry_path = Path(os.getenv("HERMES_ACCOUNT_REGISTRY", str(Path.home() / ".hermes" / "accounts" / "registry.json")))\n        live_auth = Path(os.getenv("HERMES_AUTH_FILE_PATH", str(Path.home() / ".hermes" / "auth.json")))\n        if not registry_path.is_file():\n            return False\n\n        try:\n            registry = json.loads(registry_path.read_text())\n        except Exception as exc:\n            logger.debug("Codex account rotation registry read failed: %s", exc)\n            return False\n\n        current = registry.get("active")\n        limit_meta = self._extract_codex_limit_metadata(error_text)\n        try:\n            self._record_codex_account_limit_state(registry_path, registry, current, limit_meta=limit_meta)\n        except Exception as exc:\n            logger.debug("Codex account limit state write failed: %s", exc)\n\n        if not registry.get("auto_switch_on_limit", True):\n            return False\n\n        accounts = registry.get("accounts") if isinstance(registry.get("accounts"), dict) else {}\n        if current not in accounts:\n            return False\n\n        def _limit_active(info):\n            import datetime as _dt\n            limit = info.get("limit") if isinstance(info.get("limit"), dict) else {}\n            reset_at = limit.get("reset_at")\n            if not isinstance(reset_at, str) or not reset_at:\n                return False\n            try:\n                text = reset_at[:-1] + "+00:00" if reset_at.endswith("Z") else reset_at\n                parsed = _dt.datetime.fromisoformat(text)\n                if parsed.tzinfo is not None:\n                    parsed = parsed.astimezone(_dt.timezone.utc).replace(tzinfo=None)\n                return parsed > _dt.datetime.utcnow().replace(microsecond=0)\n            except Exception:\n                return False\n\n        ordered = []\n        cooling_down = []\n        for alias, info in accounts.items():\n            if not isinstance(info, dict) or info.get("disabled"):\n                continue\n            bucket = cooling_down if _limit_active(info) else ordered\n            bucket.append((int(info.get("priority", 100)), alias, info))\n        ordered.sort()\n        cooling_down.sort()\n        available_names = [alias for _, alias, _ in ordered if alias != current]\n        if not available_names:\n            available_names = [alias for _, alias, _ in cooling_down if alias != current]\n        if not available_names:\n            return False\n\n        next_alias = available_names[0]\n        next_info = accounts.get(next_alias) or {}\n        next_file = next_info.get("file")\n        if not next_file:\n            return False\n\n        target = registry_path.parent / "auth" / str(next_file)\n        if not target.is_file():\n            return False\n\n        previous_target = None\n        if live_auth.is_symlink():\n            try:\n                previous_target = live_auth.resolve()\n            except Exception:\n                previous_target = None\n        previous_active = current\n\n        try:\n            if live_auth.is_symlink() or live_auth.exists():\n                live_auth.unlink()\n            live_auth.symlink_to(target)\n            registry["active"] = next_alias\n            acct = accounts.get(next_alias)\n            if isinstance(acct, dict):\n                acct["last_selected_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"\n            registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\\n")\n\n            from hermes_cli.auth import resolve_codex_runtime_credentials\n            creds = resolve_codex_runtime_credentials(force_refresh=False)\n\n            api_key = creds.get("api_key")\n            base_url = creds.get("base_url")\n            if not isinstance(api_key, str) or not api_key.strip():\n                raise RuntimeError("rotated Codex account missing api_key")\n            if not isinstance(base_url, str) or not base_url.strip():\n                raise RuntimeError("rotated Codex account missing base_url")\n\n            self.api_key = api_key.strip()\n            self.base_url = base_url.strip().rstrip("/")\n            self._client_kwargs["api_key"] = self.api_key\n            self._client_kwargs["base_url"] = self.base_url\n\n            if not self._replace_primary_openai_client(reason="codex_account_rotation"):\n                raise RuntimeError("failed to rebuild client after account rotation")\n        except Exception as exc:\n            logger.debug("Codex account rotation failed: %s", exc)\n            try:\n                if live_auth.is_symlink() or live_auth.exists():\n                    live_auth.unlink()\n                if previous_target is not None:\n                    live_auth.symlink_to(previous_target)\n                registry["active"] = previous_active\n                registry_path.write_text(json.dumps(registry, indent=2, sort_keys=True) + "\\n")\n            except Exception as rollback_exc:\n                logger.debug("Codex account rotation rollback failed: %s", rollback_exc)\n            return False\n\n        self._vprint(f"{self.log_prefix}🔄 Codex limit reached. Switched account: {current} → {next_alias}", force=True)\n        return True\n\n'''
    start = text.find('    def _extract_codex_limit_metadata(self, error_text: str = "") -> dict:\n')
    if start == -1:
        start = text.find('    def _try_rotate_codex_account_on_limit(self) -> bool:\n')
    if start != -1:
        end = text.find(marker, start)
        if end != -1:
            text = text[:start] + text[end:]
    if marker not in text:
        raise RuntimeError('run_agent patch marker not found')
    text = text.replace(marker, injected_block + marker)

    old = '''                    if is_rate_limited and not self._fallback_activated:\n                        if self._try_activate_fallback():\n                            retry_count = 0\n                            continue\n'''
    previous = '''                    is_usage_limit = (\n                        status_code == 429\n                        and (\n                            "usage_limit_reached" in error_msg\n                            or "usage limit" in error_msg\n                            or "quota" in error_msg\n                            or "plan_type" in error_msg\n                            or "resets_in_seconds" in error_msg\n                        )\n                    )\n                    if is_usage_limit and self._try_rotate_codex_account_on_limit():\n                        retry_count = 0\n                        continue\n\n                    if is_rate_limited and not self._fallback_activated:\n                        if self._try_activate_fallback():\n                            retry_count = 0\n                            continue\n'''
    desired = '''                    is_usage_limit = (\n                        status_code == 429\n                        and (\n                            "usage_limit_reached" in error_msg\n                            or "usage limit" in error_msg\n                            or "quota" in error_msg\n                            or "plan_type" in error_msg\n                            or "resets_in_seconds" in error_msg\n                        )\n                    )\n                    if is_usage_limit and self._try_rotate_codex_account_on_limit(error_text=str(api_error)):\n                        retry_count = 0\n                        continue\n\n                    is_rate_limited = (\n                        status_code == 429\n                        or "rate limit" in error_msg\n                        or "too many requests" in error_msg\n                        or "rate_limit" in error_msg\n                        or "usage limit" in error_msg\n                        or "quota" in error_msg\n                    )\n                    if is_rate_limited and not self._fallback_activated:\n                        if self._try_activate_fallback():\n                            retry_count = 0\n                            continue\n'''
    payload_token = 'is_payload_too_large = (\n'
    desired_with_payload = desired + '                    is_payload_too_large = (\n'
    replaced_rate_limit_block = False
    comment_block = '''                    # Eager fallback for rate-limit errors (429 or quota exhaustion).\n                    # When a fallback model is configured, switch immediately instead\n                    # of burning through retries with exponential backoff -- the\n                    # primary provider won't recover within the retry window.\n'''
    comment_start = text.find(comment_block)
    if comment_start != -1:
        block_start = comment_start + len(comment_block)
        payload_start = text.find(payload_token, block_start)
        if payload_start != -1:
            text = text[:block_start] + desired_with_payload + text[payload_start + len(payload_token):]
            replaced_rate_limit_block = True
    elif previous in text:
        text = text.replace(previous, desired)
        replaced_rate_limit_block = True
    elif old in text:
        text = text.replace(old, desired)
        replaced_rate_limit_block = True
    else:
        rate_limit_start = text.find('is_rate_limited = (\n')
        if rate_limit_start == -1:
            rate_limit_start = text.find('is_usage_limit = (\n')
        payload_start = text.find(payload_token, rate_limit_start)
        if rate_limit_start != -1 and payload_start != -1:
            rate_limit_line_start = text.rfind('\n', 0, rate_limit_start) + 1
            text = text[:rate_limit_line_start] + desired_with_payload + text[payload_start + len(payload_token):]
            replaced_rate_limit_block = True
    if not replaced_rate_limit_block:
        raise RuntimeError('run_agent rate-limit block not found')
    path.write_text(text)


def unpatch_run_agent() -> None:
    path = Path('/root/.hermes/hermes-agent/run_agent.py')
    text = path.read_text()
    marker = '    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:\n'
    start = text.find('    def _extract_codex_limit_metadata(self, error_text: str = "") -> dict:\n')
    if start == -1:
        start = text.find('    def _try_rotate_codex_account_on_limit(self) -> bool:\n')
    if start != -1:
        end = text.find(marker, start)
        if end != -1:
            text = text[:start] + text[end:]
    patched = '''                    is_usage_limit = (\n                        status_code == 429\n                        and (\n                            "usage_limit_reached" in error_msg\n                            or "usage limit" in error_msg\n                            or "quota" in error_msg\n                            or "plan_type" in error_msg\n                            or "resets_in_seconds" in error_msg\n                        )\n                    )\n                    if is_usage_limit and self._try_rotate_codex_account_on_limit(error_text=str(api_error)):\n                        retry_count = 0\n                        continue\n\n                    if is_rate_limited and not self._fallback_activated:\n                        if self._try_activate_fallback():\n                            retry_count = 0\n                            continue\n'''
    old = '''                    if is_rate_limited and not self._fallback_activated:\n                        if self._try_activate_fallback():\n                            retry_count = 0\n                            continue\n'''
    if patched in text:
        text = text.replace(patched, old)
    path.write_text(text)


def cmd_init(args: argparse.Namespace) -> int:
    registry = load_registry()
    imported = migrate_from_existing(registry)
    if args.import_path:
        import_auth_file(registry, Path(args.import_path), alias=args.alias, make_active=True)
    if not registry.get('accounts'):
        err('no auth files found to import. use: hmx import /path/to/auth.json alias')
    if args.active and args.active in registry.get('accounts', {}):
        registry['active'] = args.active
    if not registry.get('active'):
        registry['active'] = ordered_accounts(registry, include_disabled=True)[0]
    ensure_base_config()
    ensure_auth_lock()
    save_registry(registry)
    sync_live_auth_symlink(registry)
    patch_run_agent()
    print(f'initialized account mux in {MUX_DIR}')
    if imported:
        print('imported: ' + ', '.join(imported))
    print(f'active: {registry["active"]}')
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    registry = load_registry()
    rows = build_list_rows(registry, include_disabled=args.all, now=utcnow())
    for line in list_summary_text(registry, rows):
        print(line)
    print('')
    headers = ['CUR', 'ACCOUNT', 'PROVIDER', 'PLAN', 'STATUS', '5H', 'RESET', 'PRIO', 'EMAIL']
    table_rows = [
        [row['current'], row['account'], row['provider'], row['plan'], row['status'], row['five_h'], row['reset'], row['priority'], row['email']]
        for row in rows
    ]
    for line in render_table(headers, table_rows):
        print(line)
    metadata_lines = []
    for alias in ordered_accounts(registry, include_disabled=args.all):
        info = registry['accounts'][alias]
        meta = metadata_text(info)
        note = note_text(info)
        extras = []
        if meta:
            extras.append(meta)
        if note:
            extras.append(f'note={note}')
        if extras:
            metadata_lines.append(f'- {alias}: ' + ' | '.join(extras))
    if metadata_lines:
        print('')
        print('notes:')
        for line in metadata_lines:
            print(line)
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    registry = load_registry()
    old_alias = args.old_alias
    new_alias = slugify(args.new_alias)
    if old_alias not in registry.get('accounts', {}):
        err(f"unknown account '{old_alias}'")
    if old_alias == new_alias:
        err('old and new aliases are the same')
    if new_alias in registry.get('accounts', {}):
        err(f"account '{new_alias}' already exists")

    acct = dict(registry['accounts'][old_alias])
    old_target = AUTH_DIR / acct['file']
    renamed_file = False
    if acct.get('file') == f'{old_alias}.json':
        candidate = AUTH_DIR / f'{new_alias}.json'
        if candidate.exists() and candidate != old_target:
            err(f"cannot rename auth file to {candidate.name}; file already exists")
        if old_target.exists() and candidate != old_target:
            old_target.rename(candidate)
            acct['file'] = candidate.name
            renamed_file = True

    registry['accounts'][new_alias] = acct
    registry['accounts'].pop(old_alias, None)
    if registry.get('active') == old_alias:
        registry['active'] = new_alias
    save_registry(registry)
    sync_live_auth_symlink(registry)
    print(f"renamed account {old_alias} -> {new_alias}")
    if args.new_alias != new_alias:
        print(f"normalized alias: {new_alias}")
    if renamed_file:
        print(f"renamed auth file to {acct['file']}")
    elif acct.get('file') != f'{new_alias}.json':
        print(f"kept auth file as {acct['file']}")
    return 0


def cmd_annotate(args: argparse.Namespace) -> int:
    registry = load_registry()
    acct = registry.get('accounts', {}).get(args.alias)
    if not acct:
        err(f"unknown account '{args.alias}'")

    changes = []
    for field in ('label', 'note', 'role'):
        value = getattr(args, field)
        if value is not None:
            acct[field] = value
            changes.append(field)
    if args.priority is not None:
        acct['priority'] = args.priority
        changes.append('priority')

    if not changes:
        err('no metadata changes provided; use --label/--note/--role/--priority')

    save_registry(registry)
    print(f"updated {args.alias}: {', '.join(changes)}")
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    registry = load_registry()
    alias = args.alias or registry.get('active')
    if not alias or alias not in registry.get('accounts', {}):
        err('unknown account')
    if registry['accounts'][alias].get('disabled'):
        err(f"account '{alias}' is disabled")
    registry['active'] = alias
    registry['accounts'][alias]['last_selected_at'] = now_iso()
    save_registry(registry)
    sync_live_auth_symlink(registry)
    extra_args = list(args.hermes_args)
    if args.continue_latest:
        extra_args = ['-c'] + extra_args
    if args.resume:
        extra_args = ['--resume', args.resume] + extra_args
    if extra_args:
        return run_hermes(extra_args, replace=True)
    print(alias)
    return 0


def cmd_hop(args: argparse.Namespace) -> int:
    registry = load_registry()
    alias = next_account(registry, current=args.from_account or registry.get('active'))
    registry['active'] = alias
    registry['accounts'][alias]['last_selected_at'] = now_iso()
    save_registry(registry)
    sync_live_auth_symlink(registry)
    extra_args = list(args.hermes_args)
    if args.resume:
        extra_args = ['--resume', args.resume] + extra_args
    elif args.continue_latest or not extra_args:
        extra_args = ['-c'] + extra_args
    return run_hermes(extra_args, replace=True)


def cmd_import(args: argparse.Namespace) -> int:
    registry = load_registry()
    alias = import_auth_file(registry, Path(args.path), alias=args.alias, make_active=args.activate)
    save_registry(registry)
    if args.activate:
        sync_live_auth_symlink(registry)
    print(f'imported {alias} -> {AUTH_DIR / registry["accounts"][alias]["file"]}')
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    registry = load_registry()
    if not LIVE_AUTH_PATH.exists():
        err(f'{LIVE_AUTH_PATH} does not exist')
    alias = import_auth_file(registry, LIVE_AUTH_PATH.resolve() if LIVE_AUTH_PATH.is_symlink() else LIVE_AUTH_PATH, alias=args.alias, make_active=args.activate)
    save_registry(registry)
    if args.activate:
        sync_live_auth_symlink(registry)
    print(f'captured current auth as {alias}')
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    registry = load_registry()
    alias = slugify(args.alias)
    target = AUTH_DIR / f'{alias}.json'
    if alias in registry.get('accounts', {}):
        err(f"account '{alias}' already exists")
    target.write_text(json.dumps({'version': 1, 'providers': {}, 'active_provider': 'openai-codex'}, indent=2) + '\n')
    registry.setdefault('accounts', {})[alias] = {
        'file': target.name,
        'provider': DEFAULT_PROVIDER,
        'email': 'unknown',
        'plan': 'unknown',
        'added_at': now_iso(),
        'priority': len(registry.get('accounts', {})) + 1,
        'disabled': False,
        'source': 'hmx-add',
    }

    save_registry(registry)
    print(f'created empty account slot: {alias}')
    print(f'then run: hmx login {alias}')
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    registry = load_registry()
    alias = args.alias
    if alias not in registry.get('accounts', {}):
        err(f"unknown account '{alias}'")
    registry['active'] = alias
    save_registry(registry)
    sync_live_auth_symlink(registry)
    return run_hermes(['login', args.provider], replace=False)


def cmd_remove(args: argparse.Namespace) -> int:
    registry = load_registry()
    alias = args.alias
    acct = registry.get('accounts', {}).get(alias)
    if not acct:
        err(f"unknown account '{alias}'")
    target = AUTH_DIR / acct['file']
    was_active = registry.get('active') == alias
    if was_active:
        others = [a for a in ordered_accounts(registry) if a != alias]
        registry['active'] = others[0] if others else None
    registry['accounts'].pop(alias, None)
    save_registry(registry)
    if was_active and registry.get('active'):
        sync_live_auth_symlink(registry)
    if args.purge and target.exists():
        target.unlink()
    print(f'removed account {alias}')
    if was_active:
        print(f'new active: {registry.get("active") or "-"}')
    return 0


def cmd_disable(args: argparse.Namespace) -> int:
    registry = load_registry()
    acct = registry.get('accounts', {}).get(args.alias)
    if not acct:
        err(f"unknown account '{args.alias}'")
    acct['disabled'] = True
    save_registry(registry)
    print(f'disabled {args.alias}')
    return 0


def cmd_enable(args: argparse.Namespace) -> int:
    registry = load_registry()
    acct = registry.get('accounts', {}).get(args.alias)
    if not acct:
        err(f"unknown account '{args.alias}'")
    acct['disabled'] = False
    save_registry(registry)
    print(f'enabled {args.alias}')
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    registry = load_registry()
    print(registry.get('active') or '')
    return 0


def cmd_mode(args: argparse.Namespace) -> int:
    ensure_base_config()
    cfg_path = ROOT_HOME / 'config.yaml'
    cfg = load_yaml(cfg_path)
    preset = MODE_PRESETS[args.mode]
    for dotted, value in preset.items():
        deep_set(cfg, dotted, value)
    dump_yaml(cfg_path, cfg)
    registry = load_registry()
    registry['last_mode'] = args.mode
    save_registry(registry)
    print(f"mode set to {args.mode}")
    print(f"  model: {deep_get(cfg, 'model.default')}")
    print(f"  reasoning: {deep_get(cfg, 'agent.reasoning_effort')}")
    print(f"  smart routing: {deep_get(cfg, 'smart_model_routing.enabled')}")
    cheap = deep_get(cfg, 'smart_model_routing.cheap_model', {}) or {}
    if cheap:
        print(f"  cheap model: {cheap.get('provider')}:{cheap.get('model')}")
    return 0


def cmd_auto(args: argparse.Namespace) -> int:
    registry = load_registry()
    registry['auto_switch_on_limit'] = args.state == 'on'
    save_registry(registry)
    print(f"auto_switch_on_limit: {registry['auto_switch_on_limit']}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    registry = load_registry()
    return doctor_status(registry)


def describe_run_agent_patch() -> list[str]:
    path = HERMES_RUN_AGENT_PATH
    lines = []
    if not path.exists():
        return ['run_agent.py: missing']
    text = path.read_text()
    has_helpers = 'def _extract_codex_limit_metadata' in text and 'def _try_rotate_codex_account_on_limit' in text
    has_runtime_hook = '_try_rotate_codex_account_on_limit(error_text=str(api_error))' in text
    if has_helpers and has_runtime_hook:
        status = 'patched'
    elif has_helpers:
        status = 'partial'
    else:
        status = 'upstream-only'
    lines.append(f'run_agent.py: {status}')
    lines.append('  base: Hermes upstream file')
    if status == 'patched':
        lines.append('  overlay: hmx auto-rotate patch for Codex 429 usage-limit detection')
        lines.append('  behavior: update -> reapply patch -> verify compile')
    elif status == 'partial':
        lines.append('  overlay: helpers present but 429 runtime hook missing')
        lines.append('  action: rerun hmx patch-hermes to restore live auto-rotate behavior')
    else:
        lines.append('  overlay: not present')
    return lines


def cmd_explain(args: argparse.Namespace) -> int:
    registry = load_registry()
    print(f'root_home: {ROOT_HOME}')
    print(f'repo: {HERMES_REPO_PATH}')
    print(f'active: {registry.get("active") or "-"}')
    print(f'default provider: {DEFAULT_PROVIDER}')
    print('')
    print('Hermes layering:')
    print('  1) hermes update pulls upstream changes')
    print('  2) hmx patch-hermes reapplies the local Codex rotation overlay')
    print('  3) hmx update runs both steps and verifies run_agent.py')
    print('')
    for line in describe_run_agent_patch():
        print(line)
    return 0


def verify_run_agent_compile() -> None:
    subprocess.run(['python3', '-m', 'py_compile', str(HERMES_RUN_AGENT_PATH)], check=True)


def ensure_hmx_entrypoint_wrapper() -> None:
    HMX_BIN_PATH.parent.mkdir(parents=True, exist_ok=True)
    wrapper = (
        '#!/usr/bin/env bash\n'
        f'exec python3 {HMX_SOURCE_PATH} "$@"\n'
    )
    if HMX_BIN_PATH.exists() and HMX_BIN_PATH.read_text() == wrapper:
        return
    HMX_BIN_PATH.write_text(wrapper)
    HMX_BIN_PATH.chmod(0o755)


def run_codex_rotation_smoke_test() -> dict[str, Any]:
    smoke_error_text = (
        "HTTP 429: The usage limit has been reached "
        "{'type': 'usage_limit_reached', 'message': 'The usage limit has been reached', "
        "'plan_type': 'team', 'resets_at': 1775181989, 'resets_in_seconds': 428190}"
    )
    run_agent_template = '''from pathlib import Path
import json
import os
from datetime import datetime


class _Logger:
    def debug(self, *args, **kwargs):
        pass


logger = _Logger()


class AIAgent:
    def __init__(self):
        self.api_mode = "codex_responses"
        self.provider = "openai-codex"
        self._fallback_activated = False
        self._client_kwargs = {}
        self.api_key = ""
        self.base_url = ""
        self.log_prefix = ""
        self.replaced_client_reasons = []
        self.fallback_called = False

    def _vprint(self, *args, **kwargs):
        pass

    def _try_activate_fallback(self):
        self.fallback_called = True
        return False

    def _replace_primary_openai_client(self, reason=None):
        self.replaced_client_reasons.append(reason)
        return True

    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:
        return False

    def sample(self, status_code, error_msg, api_error):
        retry_count = 1
        attempts = 0
        while attempts < 3:
            attempts += 1
            if self.replaced_client_reasons:
                status_code = 200
                error_msg = ""

            try:
                if True:
                    # Eager fallback for rate-limit errors (429 or quota exhaustion).
                    # When a fallback model is configured, switch immediately instead
                    # of burning through retries with exponential backoff -- the
                    # primary provider won't recover within the retry window.
                    is_rate_limited = (
                        status_code == 429
                        or "rate limit" in error_msg
                        or "too many requests" in error_msg
                        or "rate_limit" in error_msg
                        or "usage limit" in error_msg
                        or "quota" in error_msg
                    )
                    if is_rate_limited and not self._fallback_activated:
                        if self._try_activate_fallback():
                            retry_count = 0
                            continue

                    is_payload_too_large = (
                        status_code == 413
                    )
                    return {
                        "attempts": attempts,
                        "retry_count": retry_count,
                        "fallback_called": self.fallback_called,
                        "rotated": bool(self.replaced_client_reasons),
                        "is_payload_too_large": is_payload_too_large,
                    }
            except Exception:
                raise
        return {
            "attempts": attempts,
            "retry_count": retry_count,
            "fallback_called": self.fallback_called,
            "rotated": bool(self.replaced_client_reasons),
            "is_payload_too_large": False,
        }
'''
    original_run_agent_path = HERMES_RUN_AGENT_PATH
    saved_modules = {
        'hermes_cli': sys.modules.get('hermes_cli'),
        'hermes_cli.auth': sys.modules.get('hermes_cli.auth'),
    }
    saved_env = {key: os.environ.get(key) for key in ('HERMES_ACCOUNT_REGISTRY', 'HERMES_AUTH_FILE_PATH')}
    try:
        with tempfile.TemporaryDirectory(prefix='hmx-smoke-') as tmpdir:
            tmp = Path(tmpdir)
            repo_dir = tmp / 'repo'
            repo_dir.mkdir()
            run_agent_path = repo_dir / 'run_agent.py'
            run_agent_path.write_text(run_agent_template)

            globals()['HERMES_RUN_AGENT_PATH'] = run_agent_path
            patch_run_agent()
            subprocess.run(['python3', '-m', 'py_compile', str(run_agent_path)], check=True)

            auth_dir = tmp / 'accounts' / 'auth'
            auth_dir.mkdir(parents=True)
            live_auth = tmp / 'auth.json'
            registry_path = tmp / 'accounts' / 'registry.json'
            main_auth = auth_dir / 'main.json'
            backup_auth = auth_dir / 'backup.json'
            for path in (main_auth, backup_auth):
                path.write_text('{"provider":"openai-codex"}\n')
            if live_auth.exists() or live_auth.is_symlink():
                live_auth.unlink()
            live_auth.symlink_to(main_auth)
            registry = {
                'schema': 2,
                'active': 'main',
                'auto_switch_on_limit': True,
                'accounts': {
                    'main': {'file': 'main.json', 'priority': 1, 'disabled': False, 'provider': 'openai-codex', 'plan': 'plus', 'email': 'main@example.com'},
                    'backup': {'file': 'backup.json', 'priority': 2, 'disabled': False, 'provider': 'openai-codex', 'plan': 'team', 'email': 'backup@example.com'},
                },
            }
            registry_path.write_text(json.dumps(registry, indent=2) + '\n')
            os.environ['HERMES_ACCOUNT_REGISTRY'] = str(registry_path)
            os.environ['HERMES_AUTH_FILE_PATH'] = str(live_auth)

            fake_auth = types.ModuleType('hermes_cli.auth')

            def _resolve_codex_runtime_credentials(force_refresh: bool = False):
                return {
                    'api_key': 'rotated-key',
                    'base_url': 'https://example.invalid/codex',
                }

            fake_auth.resolve_codex_runtime_credentials = _resolve_codex_runtime_credentials
            fake_pkg = types.ModuleType('hermes_cli')
            fake_pkg.auth = fake_auth
            sys.modules['hermes_cli'] = fake_pkg
            sys.modules['hermes_cli.auth'] = fake_auth

            spec = importlib.util.spec_from_file_location('hmx_smoke_run_agent', run_agent_path)
            module = importlib.util.module_from_spec(spec)
            assert spec and spec.loader is not None
            spec.loader.exec_module(module)

            agent = module.AIAgent()

            class FakeAPIError:
                def __str__(self):
                    return smoke_error_text

            result = agent.sample(429, smoke_error_text.lower(), FakeAPIError())
            updated = json.loads(registry_path.read_text())
            limited = updated['accounts']['main'].get('limit', {})
            resolved_live_auth = live_auth.resolve()
            if updated.get('active') != 'backup':
                raise RuntimeError(f"smoke failed: expected active=backup, got {updated.get('active')!r}")
            if resolved_live_auth != backup_auth:
                raise RuntimeError(f'smoke failed: live auth did not switch to backup ({resolved_live_auth})')
            if agent._client_kwargs.get('api_key') != 'rotated-key':
                raise RuntimeError('smoke failed: rotated api_key not applied to runtime client')
            if result.get('fallback_called'):
                raise RuntimeError('smoke failed: fallback path ran before codex account rotation')
            if limited.get('state') != 'limited':
                raise RuntimeError('smoke failed: limited state was not persisted for the exhausted account')
            return {
                'attempts': result.get('attempts'),
                'active_before': 'main',
                'active_after': updated.get('active'),
                'live_auth_target': str(resolved_live_auth),
                'fallback_called': result.get('fallback_called'),
                'client_base_url': agent._client_kwargs.get('base_url'),
                'limit_reset_at': limited.get('reset_at'),
                'patch_target': str(run_agent_path),
            }
    finally:
        globals()['HERMES_RUN_AGENT_PATH'] = original_run_agent_path
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for name, module in saved_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module


def cmd_patch_hermes(args: argparse.Namespace) -> int:
    ensure_hmx_entrypoint_wrapper()
    patch_run_agent()
    try:
        verify_run_agent_compile()
    except Exception as exc:
        err(f'patched Hermes run_agent but verification failed: {exc}')
    print('patched Hermes run_agent for automatic codex account rotation on usage-limit 429')
    print('verified: run_agent.py compiles cleanly')
    print(f'wrapper: {HMX_BIN_PATH} -> {HMX_SOURCE_PATH}')
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    try:
        result = run_codex_rotation_smoke_test()
    except Exception as exc:
        err(f'codex rotation smoke test failed: {exc}')
    print('smoke: PASS codex 429 usage-limit rotates account before fallback')
    print(f"  active: {result['active_before']} -> {result['active_after']}")
    print(f"  live_auth: {result['live_auth_target']}")
    print(f"  attempts: {result['attempts']}")
    print(f"  fallback_called: {result['fallback_called']}")
    return 0


def cmd_update_hermes(args: argparse.Namespace) -> int:
    ensure_hmx_entrypoint_wrapper()
    print('→ Running hermes update...')
    try:
        proc = subprocess.run(
            hermes_cmd() + ['update'],
            cwd=HERMES_REPO_PATH,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except FileNotFoundError as exc:
        err(f'could not launch hermes update: {exc}')
    if proc.returncode != 0:
        err(f'hermes update failed with exit code {proc.returncode}')
    print('→ Reapplying Hermes patch...')
    patch_exit = cmd_patch_hermes(args)
    if patch_exit != 0:
        return patch_exit
    if getattr(args, 'skip_smoke', False):
        return 0
    print('→ Running codex rotation smoke test...')
    return cmd_smoke(args)


def cmd_unpatch_hermes(args: argparse.Namespace) -> int:
    unpatch_run_agent()
    print('removed Hermes run_agent codex account rotation patch')
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='hmx', description='Hermes single-home multi-account manager')
    sub = p.add_subparsers(dest='command', required=True)

    sp = sub.add_parser('init', help='migrate current/legacy auths into one account pool under ~/.hermes/accounts')
    sp.add_argument('--import-path')
    sp.add_argument('--alias')
    sp.add_argument('--active')
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser('list', help='list registered accounts')
    sp.add_argument('--all', action='store_true')
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser('use', help='switch active account or exec hermes with it')
    sp.add_argument('alias', nargs='?')
    sp.add_argument('-c', '--continue-latest', action='store_true')
    sp.add_argument('--resume')
    sp.add_argument('hermes_args', nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_use)

    sp = sub.add_parser('hop', help='switch to next enabled account')
    sp.add_argument('--from-account')
    sp.add_argument('-c', '--continue-latest', action='store_true')
    sp.add_argument('--resume')
    sp.add_argument('hermes_args', nargs=argparse.REMAINDER)
    sp.set_defaults(func=cmd_hop)

    sp = sub.add_parser('import', help='import an auth.json into the pooled account store')
    sp.add_argument('path')
    sp.add_argument('alias', nargs='?')
    sp.add_argument('--activate', action='store_true')
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser('capture', help='copy the current live auth.json into the pooled account store')
    sp.add_argument('alias')
    sp.add_argument('--activate', action='store_true')
    sp.set_defaults(func=cmd_capture)

    sp = sub.add_parser('add', help='create an empty account slot before login/import')
    sp.add_argument('alias')
    sp.set_defaults(func=cmd_add)

    sp = sub.add_parser('login', help='run hermes login using one pooled account slot')
    sp.add_argument('alias')
    sp.add_argument('--provider', default=DEFAULT_PROVIDER)
    sp.set_defaults(func=cmd_login)

    sp = sub.add_parser('rename', help='rename an account alias and keep it active if selected')
    sp.add_argument('old_alias')
    sp.add_argument('new_alias')
    sp.set_defaults(func=cmd_rename)

    sp = sub.add_parser('annotate', help='store human-friendly metadata for an account')
    sp.add_argument('alias')
    sp.add_argument('--label')
    sp.add_argument('--note')
    sp.add_argument('--role')
    sp.add_argument('--priority', type=int)
    sp.set_defaults(func=cmd_annotate)

    sp = sub.add_parser('remove', help='remove an account entry')
    sp.add_argument('alias')
    sp.add_argument('--purge', action='store_true')
    sp.set_defaults(func=cmd_remove)

    sp = sub.add_parser('disable', help='disable an account without deleting it')
    sp.add_argument('alias')
    sp.set_defaults(func=cmd_disable)

    sp = sub.add_parser('enable', help='re-enable an account')
    sp.add_argument('alias')
    sp.set_defaults(func=cmd_enable)

    sp = sub.add_parser('current', help='show active account')
    sp.set_defaults(func=cmd_current)

    sp = sub.add_parser('mode', help='switch cost/quality preset')
    sp.add_argument('mode', choices=sorted(MODE_PRESETS.keys()))
    sp.set_defaults(func=cmd_mode)

    sp = sub.add_parser('auto', help='toggle automatic account switch on usage-limit 429')
    sp.add_argument('state', choices=['on', 'off'])
    sp.set_defaults(func=cmd_auto)

    sp = sub.add_parser('doctor', help='verify account pool and live auth symlink')
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser('explain', help='explain how upstream Hermes and hmx patching fit together')
    sp.set_defaults(func=cmd_explain)

    sp = sub.add_parser('patch-hermes', help='patch Hermes for auto account switch on limit')
    sp.set_defaults(func=cmd_patch_hermes)

    sp = sub.add_parser('smoke', help='run a local codex 429 rotation smoke test against a patched temporary run_agent')
    sp.set_defaults(func=cmd_smoke)

    sp = sub.add_parser('update', aliases=['update-hermes'], help='update Hermes, then reapply the hmx patch')
    sp.add_argument('--skip-smoke', action='store_true', help='skip the local codex rotation smoke test after patching')
    sp.set_defaults(func=cmd_update_hermes)

    sp = sub.add_parser('unpatch-hermes', help='remove the auto-switch patch from Hermes')
    sp.set_defaults(func=cmd_unpatch_hermes)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == '__main__':
    raise SystemExit(main())
