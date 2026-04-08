import base64
import datetime as dt
import json
from pathlib import Path
from typing import Any

from .runtime import (
    AUTH_DIR, BASE_URL, DEFAULT_PROVIDER, LEGACY_HOMES, LEGACY_REGISTRY,
    LIVE_AUTH_LOCK, LIVE_AUTH_PATH, MUX_DIR, REGISTRY_PATH, ROOT_HOME, yaml,
)
from .utils import deep_get, deep_set, err, now_iso, slugify

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


def ordered_accounts(registry: dict[str, Any], include_disabled: bool = False, now: dt.datetime | None = None) -> list[str]:
    from .account_health import describe_account_health

    rows = []
    now = now or utcnow()
    active_alias = registry.get('active')
    for alias, info in registry.get('accounts', {}).items():
        if not include_disabled and info.get('disabled'):
            continue
        auth_path = AUTH_DIR / info['file']
        health = describe_account_health(alias, info, auth_path, now=now, current_alias=active_alias)
        status = health['status']
        if alias == active_alias and status == 'active':
            bucket = 0
        elif status in {'active', 'unknown'}:
            bucket = 1
        elif status == 'limited':
            bucket = 2
        elif status == 'disabled':
            bucket = 3
        else:
            bucket = 4
        rows.append((bucket, int(info.get('priority', 100)), alias))
    rows.sort()
    return [alias for _, _, alias in rows]

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

def repair_live_auth_from_registry() -> bool:
    registry = load_registry()
    if not registry.get('active') or not registry.get('accounts'):
        return False
    target = active_target(registry)
    if target is None:
        return False
    sync_live_auth_symlink(registry)
    return True

