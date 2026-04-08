import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from .runtime import (
    AUTH_DIR, DEFAULT_PROVIDER, HERMES_REPO_PATH, HERMES_VENV_PYTHON,
    LIVE_AUTH_PATH, PROBE_FRESHNESS_SECONDS, PROBE_TIMEOUT_SECONDS, REGISTRY_PATH, ROOT_HOME,
)
from .utils import (
    FIVE_HOURS_SECONDS, err, format_duration_compact, format_percent_compact,
    now_iso, parse_iso8601_utc, utcnow,
)
from .account_store import auth_payload_summary, ordered_accounts, read_auth_file, save_registry

def hermes_python() -> str:
    if HERMES_VENV_PYTHON.exists():
        return str(HERMES_VENV_PYTHON)
    return sys.executable or 'python3'


def probe_result_age_seconds(probe: dict[str, Any] | None, now: dt.datetime) -> int | None:
    if not isinstance(probe, dict):
        return None
    observed_at = parse_iso8601_utc(probe.get('observed_at'))
    if observed_at is None:
        return None
    return max(0, int((now - observed_at).total_seconds()))


def fresh_probe_result(info: dict[str, Any], now: dt.datetime | None = None) -> dict[str, Any] | None:
    now = now or utcnow()
    probe = info.get('probe') if isinstance(info.get('probe'), dict) else None
    age = probe_result_age_seconds(probe, now)
    if age is None or age > PROBE_FRESHNESS_SECONDS:
        return None
    return probe


def run_account_probe(alias: str, info: dict[str, Any], registry: dict[str, Any], model: str = 'gpt-5.4') -> dict[str, Any]:
    auth_path = AUTH_DIR / info['file']
    observed_at = now_iso()
    if not auth_path.exists():
        return {
            'observed_at': observed_at,
            'status': 'missing',
            'scenario': 'local_credentials_missing',
            'status_code': None,
            'code': 'auth_file_missing',
            'detail': 'auth file missing',
            'latency_ms': None,
        }

    with tempfile.TemporaryDirectory(prefix=f'hmx-probe-{alias}-') as tmpdir:
        tmp_home = Path(tmpdir)
        (tmp_home / 'auth.json').symlink_to(auth_path)
        env = os.environ.copy()
        env['HERMES_HOME'] = str(tmp_home)
        existing_pp = env.get('PYTHONPATH', '')
        env['PYTHONPATH'] = str(HERMES_REPO_PATH) + (os.pathsep + existing_pp if existing_pp else '')
        env['HMX_PROBE_MODEL'] = model
        script = '''
import json, os, time
from openai import OpenAI
from hermes_cli.auth import resolve_codex_runtime_credentials
from hermes_cli.codex_account_registry import classify_codex_account_condition

result = {
    "ok": False,
    "status": "unknown",
    "scenario": "unknown",
    "status_code": None,
    "code": None,
    "detail": "",
    "latency_ms": None,
}
start = time.time()
try:
    creds = resolve_codex_runtime_credentials(force_refresh=False)
    client = OpenAI(api_key=creds.get("api_key"), base_url=creds.get("base_url"))
    with client.responses.stream(
        model=os.getenv("HMX_PROBE_MODEL", "gpt-5.4"),
        instructions="Reply exactly OK",
        input=[{"role": "user", "content": [{"type": "input_text", "text": "OK"}]}],
        store=False,
    ) as stream:
        response = stream.get_final_response()
    result.update({
        "ok": True,
        "status": "available",
        "scenario": "available",
        "detail": "probe ok",
        "response_id": getattr(response, "id", None),
    })
except Exception as exc:
    status_code = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    classified = classify_codex_account_condition(
        status_code=status_code,
        error_text=str(exc),
        error_body=body,
    )
    result.update({
        "status": classified.get("scenario") or "unknown",
        "scenario": classified.get("scenario") or "unknown",
        "status_code": status_code,
        "code": classified.get("code"),
        "detail": str(exc),
        "meta": classified.get("meta") or {},
    })
finally:
    result["latency_ms"] = int((time.time() - start) * 1000)
print(json.dumps(result))
'''
        proc = subprocess.run(
            [hermes_python(), '-c', script],
            env=env,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
        )

    stdout = (proc.stdout or '').strip().splitlines()
    payload = None
    if stdout:
        try:
            payload = json.loads(stdout[-1])
        except Exception:
            payload = None

    if not isinstance(payload, dict):
        payload = {
            'status': 'unknown',
            'scenario': 'unknown',
            'status_code': None,
            'code': None,
            'detail': (proc.stderr or proc.stdout or 'probe failed').strip()[:400],
            'latency_ms': None,
        }

    payload['observed_at'] = observed_at
    payload['account'] = alias
    if payload.get('status') == 'available':
        payload['last_ok_at'] = observed_at
    else:
        previous_probe = info.get('probe') if isinstance(info.get('probe'), dict) else {}
        if previous_probe.get('last_ok_at'):
            payload['last_ok_at'] = previous_probe.get('last_ok_at')
    return payload


def apply_probe_result(info: dict[str, Any], probe: dict[str, Any]) -> None:
    info['probe'] = probe
    status = str(probe.get('status') or probe.get('scenario') or '').strip().lower()
    observed_at = probe.get('observed_at')
    if status == 'available':
        info['last_verified_at'] = observed_at
        return
    if status == 'limited':
        meta = probe.get('meta') if isinstance(probe.get('meta'), dict) else {}
        limit = {
            'state': 'limited',
            'observed_at': observed_at,
            'source': 'hmx_probe',
        }
        if meta.get('plan_type'):
            limit['plan_type'] = meta['plan_type']
        if meta.get('resets_at'):
            limit['resets_at'] = meta['resets_at']
        if meta.get('resets_in_seconds'):
            limit['resets_in_seconds'] = meta['resets_in_seconds']
            observed = parse_iso8601_utc(observed_at)
            if observed is not None:
                limit['reset_at'] = (observed + dt.timedelta(seconds=int(meta['resets_in_seconds']))).isoformat() + 'Z'
        info['limit'] = limit
        return
    if status in {'deactivated', 'billing_inactive', 'auth_invalid', 'local_credentials_missing'}:
        payload_key = {
            'deactivated': 'deactivation',
            'billing_inactive': 'billing',
            'auth_invalid': 'auth_failure',
            'local_credentials_missing': 'auth_failure',
        }[status]
        payload = {
            'state': status,
            'observed_at': observed_at,
            'source': 'hmx_probe',
        }
        code = str(probe.get('code') or '').strip()
        if code:
            payload['code'] = code
        info[payload_key] = payload
        info['disabled'] = True
        info['disabled_reason'] = code or status
        info['disabled_at'] = observed_at
        info['last_verified_at'] = observed_at


def probe_accounts(registry: dict[str, Any], aliases: list[str] | None = None, model: str = 'gpt-5.4') -> list[dict[str, Any]]:
    results = []
    targets = aliases or ordered_accounts(registry, include_disabled=False, now=utcnow())
    for alias in targets:
        info = registry.get('accounts', {}).get(alias)
        if not isinstance(info, dict) or info.get('disabled'):
            continue
        probe = run_account_probe(alias, info, registry, model=model)
        apply_probe_result(info, probe)
        results.append(probe)
    if results:
        save_registry(registry)
    return results

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


def describe_account_health(alias: str, info: dict[str, Any], auth_path: Path, now: dt.datetime | None = None, current_alias: str | None = None) -> dict[str, str]:
    now = now or utcnow()
    summary = effective_account_summary(info, auth_path)
    limit = info.get('limit') if isinstance(info.get('limit'), dict) else {}
    state = str(limit.get('state') or '').strip().lower()
    observed_at = parse_iso8601_utc(limit.get('observed_at'))
    last_selected_at = parse_iso8601_utc(info.get('last_selected_at'))
    reset_at = parse_iso8601_utc(limit.get('reset_at'))
    reset = '-'
    five_h = five_hour_window_remaining(limit, now)
    auth_status = summary.get('status', '-')

    stale_limit = bool(
        state == 'limited'
        and observed_at is not None
        and last_selected_at is not None
        and last_selected_at > observed_at
    )
    if stale_limit:
        state = ''
        reset_at = None
        five_h = '-'

    if info.get('disabled'):
        return {
            'status': 'disabled',
            'detail': 'manually disabled',
            'reset': reset,
            'five_h': five_h,
            'auth_status': auth_status,
        }
    if not auth_path.exists():
        return {
            'status': 'missing',
            'detail': 'auth file missing',
            'reset': reset,
            'five_h': five_h,
            'auth_status': auth_status,
        }
    if auth_status == 'invalid':
        return {'status': 'invalid', 'detail': 'auth unreadable', 'reset': reset, 'five_h': five_h, 'auth_status': auth_status}
    if auth_status == 'missing':
        return {'status': 'missing', 'detail': 'auth missing', 'reset': reset, 'five_h': five_h, 'auth_status': auth_status}
    if auth_status == 'empty':
        return {'status': 'empty', 'detail': 'token missing', 'reset': reset, 'five_h': five_h, 'auth_status': auth_status}

    probe = fresh_probe_result(info, now=now)
    if probe:
        status = str(probe.get('status') or probe.get('scenario') or 'unknown').strip().lower()
        detail = str(probe.get('detail') or probe.get('code') or 'probe result').strip()
        if status == 'available':
            return {
                'status': 'active' if alias == current_alias else 'available',
                'detail': 'live probe ok',
                'reset': '-',
                'five_h': '-',
                'auth_status': auth_status,
            }
        if status == 'limited':
            probe_reset = '-'
            probe_five_h = '-'
            meta = probe.get('meta') if isinstance(probe.get('meta'), dict) else {}
            observed = parse_iso8601_utc(probe.get('observed_at'))
            seconds_left = None
            if isinstance(meta.get('resets_in_seconds'), (int, float)) and meta.get('resets_in_seconds') > 0:
                if observed is not None:
                    elapsed = max(0, int((now - observed).total_seconds()))
                    seconds_left = max(0, int(float(meta.get('resets_in_seconds')) - elapsed))
                else:
                    seconds_left = int(float(meta.get('resets_in_seconds')))
            elif isinstance(meta.get('resets_at'), (int, float)) and meta.get('resets_at') > 0:
                reset_dt = dt.datetime.utcfromtimestamp(int(meta.get('resets_at')))
                seconds_left = max(0, int((reset_dt - now).total_seconds()))
            if seconds_left and seconds_left > 0:
                probe_reset = format_duration_compact(seconds_left)
                if seconds_left <= FIVE_HOURS_SECONDS:
                    probe_five_h = format_percent_compact((seconds_left / FIVE_HOURS_SECONDS) * 100)
            return {
                'status': 'limited',
                'detail': detail,
                'reset': probe_reset,
                'five_h': probe_five_h,
                'auth_status': auth_status,
            }
        if status in {'deactivated', 'billing_inactive', 'auth_invalid', 'local_credentials_missing'}:
            pretty = {
                'deactivated': 'deactivated',
                'billing_inactive': 'billing',
                'auth_invalid': 'auth',
                'local_credentials_missing': 'missing',
            }[status]
            return {
                'status': pretty,
                'detail': detail,
                'reset': '-',
                'five_h': '-',
                'auth_status': auth_status,
            }

    if state == 'limited' and reset_at is None:
        return {
            'status': 'limited',
            'detail': 'usage limit',
            'reset': reset,
            'five_h': five_h,
            'auth_status': auth_status,
        }

    if state == 'limited' and reset_at and reset_at > now:
        seconds_left = int((reset_at - now).total_seconds())
        return {
            'status': 'limited',
            'detail': 'usage limit',
            'reset': format_duration_compact(seconds_left),
            'five_h': five_h,
            'auth_status': auth_status,
        }

    if alias == current_alias and auth_status == 'ok':
        return {
            'status': 'active',
            'detail': 'currently selected',
            'reset': reset,
            'five_h': five_h,
            'auth_status': auth_status,
        }

    return {
        'status': 'unknown',
        'detail': 'stale limit telemetry' if stale_limit else 'no limit telemetry',
        'reset': reset,
        'five_h': five_h,
        'auth_status': auth_status,
    }


def build_list_rows(registry: dict[str, Any], include_disabled: bool = False, now: dt.datetime | None = None) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    now = now or utcnow()
    active_alias = registry.get('active')
    ordered_aliases = ordered_accounts(registry, include_disabled=include_disabled, now=now)
    for rank, alias in enumerate(ordered_aliases, start=1):
        info = registry['accounts'][alias]
        auth_path = AUTH_DIR / info['file']
        summary = effective_account_summary(info, auth_path)
        health = describe_account_health(alias, info, auth_path, now=now, current_alias=active_alias)
        rows.append(
            {
                'current': '*' if alias == active_alias else '',
                'account': alias,
                'provider': summary.get('provider', DEFAULT_PROVIDER) or DEFAULT_PROVIDER,
                'plan': summary.get('plan', '-') or '-',
                'status': health['status'],
                'five_h': health['five_h'],
                'reset': health['reset'],
                'priority': str(rank),
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
    unhealthy = sum(1 for row in rows if row['status'] in {'disabled', 'missing', 'invalid', 'empty'})
    auto_rotate = 'on' if registry.get('auto_switch_on_limit', True) else 'off'
    return [
        f'account-pool: total={total} enabled={enabled} disabled={disabled} limited={limited} issues={unhealthy}',
        f'active: {registry.get("active") or "-"}  auto-rotate: {auto_rotate}  root: {ROOT_HOME}',
    ]


def next_account(registry: dict[str, Any], current: str | None = None) -> str:
    names = ordered_accounts(registry, now=utcnow())
    if not names:
        err('no active accounts')
    cur = current or registry.get('active')
    if cur not in names:
        return names[0]
    idx = names.index(cur)
    return names[(idx + 1) % len(names)]

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

