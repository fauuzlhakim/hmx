import datetime as dt
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
from typing import Any

from .runtime import (
    HERMES_AUTH_MODULE_PATH, HERMES_REPO_PATH, HERMES_RUN_AGENT_PATH,
    HMX_BIN_PATH, HMX_SOURCE_PATH,
)

def patch_run_agent() -> None:
    path = HERMES_RUN_AGENT_PATH
    text = path.read_text()
    marker = '    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:\n'
    injected_block = """
    def _extract_codex_limit_metadata(self, error_text: str = "") -> dict:
        try:
            from hermes_cli.codex_account_registry import extract_codex_account_error_metadata
        except Exception:
            return {}
        return extract_codex_account_error_metadata(error_text)

    def _record_codex_account_rotation_condition(self, registry_path, registry, current_alias, *, classified):
        accounts = registry.get("accounts") if isinstance(registry.get("accounts"), dict) else {}
        acct = accounts.get(current_alias)
        if not isinstance(acct, dict):
            return
        classified = classified if isinstance(classified, dict) else {}
        scenario = str(classified.get("scenario") or "unknown").strip().lower()
        meta = classified.get("meta") if isinstance(classified.get("meta"), dict) else {}
        observed_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"

        try:
            from hermes_cli.codex_account_registry import (
                record_codex_account_deactivation_state,
                record_codex_account_limit_state,
                save_codex_account_registry,
            )
        except Exception as exc:
            logger.debug("Codex account registry helper import failed: %s", exc)
            return

        if scenario == "limited":
            record_codex_account_limit_state(registry, current_alias, limit_meta=meta)
        elif scenario == "deactivated":
            record_codex_account_deactivation_state(
                registry,
                current_alias,
                error_meta=meta,
                disable_account=bool(classified.get("disable_account", True)),
            )
        elif scenario in {"billing_inactive", "auth_invalid", "local_credentials_missing"}:
            payload_key = {
                "billing_inactive": "billing",
                "auth_invalid": "auth_failure",
                "local_credentials_missing": "auth_failure",
            }[scenario]
            payload = {
                "state": scenario,
                "observed_at": observed_at,
                "source": f"codex_{scenario}",
            }
            code = str(classified.get("code") or meta.get("code") or "").strip()
            if code:
                payload["code"] = code
            acct[payload_key] = payload
            if classified.get("disable_account"):
                acct["disabled"] = True
                acct["disabled_reason"] = code or scenario
                acct["disabled_at"] = observed_at
        save_codex_account_registry(registry_path, registry)

    def _try_rotate_codex_account_on_error(
        self,
        *,
        status_code=None,
        error_text: str = "",
        error_body=None,
        reason_label: str = "request_error",
    ) -> bool:
        if self.api_mode != "codex_responses" or self.provider != "openai-codex":
            return False

        try:
            import fcntl
            from hermes_cli.codex_account_registry import (
                classify_codex_account_condition,
                load_codex_account_registry,
                resolve_codex_account_paths,
                save_codex_account_registry,
                select_next_codex_account,
                swap_live_auth_symlink,
            )
        except Exception as exc:
            logger.debug("Codex account rotation helper import failed: %s", exc)
            return False

        def _parse_rotation_dt(value):
            if not value:
                return None
            if isinstance(value, (int, float)):
                try:
                    return datetime.utcfromtimestamp(int(value))
                except Exception:
                    return None
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return None
                try:
                    if text.endswith('Z'):
                        text = text[:-1] + '+00:00'
                    parsed = datetime.fromisoformat(text)
                    if parsed.tzinfo is not None:
                        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
                    return parsed
                except Exception:
                    return None
            return None

        def _cooldown_seconds(info, now_dt):
            limit = info.get('limit') if isinstance(info, dict) and isinstance(info.get('limit'), dict) else {}
            if str(limit.get('state') or '').strip().lower() != 'limited':
                return 0
            reset_at = _parse_rotation_dt(limit.get('reset_at'))
            if reset_at is None:
                reset_at = _parse_rotation_dt(limit.get('resets_at'))
            if reset_at is None and isinstance(limit.get('resets_in_seconds'), (int, float)):
                observed = _parse_rotation_dt(limit.get('observed_at'))
                if observed is not None:
                    reset_at = observed + __import__('datetime').timedelta(seconds=max(0, int(float(limit.get('resets_in_seconds')))))
            if reset_at is None:
                return 1
            return max(0, int((reset_at - now_dt).total_seconds()))

        def _candidate_is_blocked(alias, info, now_dt):
            if not isinstance(info, dict) or info.get('disabled'):
                return True, 'disabled'
            cooldown = _cooldown_seconds(info, now_dt)
            if cooldown > 0:
                return True, f'cooldown={cooldown}s'
            return False, None

        def _rotation_candidates(accounts, current_alias, now_dt):
            source_current = current_alias
            search_current = current_alias
            attempted = {current_alias}
            ordered = []
            while True:
                next_alias = select_next_codex_account(accounts, search_current)
                if not next_alias or next_alias in attempted:
                    break
                attempted.add(next_alias)
                ordered.append(next_alias)
                search_current = next_alias
            if not ordered:
                for alias, info in sorted(
                    ((alias, info) for alias, info in accounts.items() if alias != source_current),
                    key=lambda item: (int((item[1] or {}).get('priority', 100)), item[0]),
                ):
                    if alias not in ordered:
                        ordered.append(alias)
            return ordered

        registry_path, live_auth = resolve_codex_account_paths()
        lock_path = live_auth.with_name('auth.lock')
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, 'a+', encoding='utf-8') as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            registry = load_codex_account_registry(registry_path)
            if not isinstance(registry, dict):
                return False

            current = registry.get("active")
            accounts = registry.get("accounts") if isinstance(registry.get("accounts"), dict) else {}
            if current not in accounts:
                logger.debug("Codex account rotation skipped: current not in accounts")
                return False

            classified = classify_codex_account_condition(
                status_code=status_code,
                error_text=error_text,
                error_body=error_body,
            )
            if not classified.get("should_rotate"):
                return False

            try:
                self._record_codex_account_rotation_condition(
                    registry_path,
                    registry,
                    current,
                    classified=classified,
                )
            except Exception as exc:
                logger.debug("Codex account condition write failed: %s", exc)

            if not registry.get("auto_switch_on_limit", True):
                return False

            previous_target = None
            if live_auth.is_symlink():
                try:
                    previous_target = live_auth.resolve()
                except Exception:
                    previous_target = None
            elif live_auth.exists():
                previous_target = live_auth
            previous_active = current
            now_dt = datetime.utcnow().replace(microsecond=0)
            blocked_candidates = []

            for next_alias in _rotation_candidates(accounts, current, now_dt):
                next_info = accounts.get(next_alias) or {}
                blocked, blocked_reason = _candidate_is_blocked(next_alias, next_info, now_dt)
                if blocked:
                    blocked_candidates.append(f"{next_alias}:{blocked_reason}")
                    continue

                next_file = next_info.get("file")
                if not next_file:
                    self._record_codex_account_rotation_condition(
                        registry_path,
                        registry,
                        next_alias,
                        classified={
                            "scenario": "local_credentials_missing",
                            "disable_account": True,
                            "meta": {"code": "auth_file_missing"},
                        },
                    )
                    continue

                target = registry_path.parent / "auth" / str(next_file)
                if not target.is_file():
                    self._record_codex_account_rotation_condition(
                        registry_path,
                        registry,
                        next_alias,
                        classified={
                            "scenario": "local_credentials_missing",
                            "disable_account": True,
                            "meta": {"code": "auth_file_missing"},
                        },
                    )
                    continue

                try:
                    swap_live_auth_symlink(live_auth, target)
                    registry["active"] = next_alias
                    acct = accounts.get(next_alias)
                    if isinstance(acct, dict):
                        acct["last_selected_at"] = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                    save_codex_account_registry(registry_path, registry)

                    from hermes_cli.auth import resolve_codex_runtime_credentials
                    creds = resolve_codex_runtime_credentials(force_refresh=False)

                    api_key = creds.get("api_key")
                    base_url = creds.get("base_url")
                    if not isinstance(api_key, str) or not api_key.strip():
                        raise RuntimeError("rotated Codex account missing api_key")
                    if not isinstance(base_url, str) or not base_url.strip():
                        raise RuntimeError("rotated Codex account missing base_url")

                    self.api_key = api_key
                    self.base_url = base_url.strip().rstrip("/")
                    self._client_kwargs["api_key"] = self.api_key
                    self._client_kwargs["base_url"] = self.base_url

                    if not self._replace_primary_openai_client(reason=f"codex_account_rotation_{reason_label}"):
                        raise RuntimeError("failed to rebuild client after account rotation")
                except Exception as exc:
                    logger.debug("Codex account rotation candidate %s failed: %s", next_alias, exc)
                    failed_condition = classify_codex_account_condition(
                        status_code=getattr(exc, "status_code", None),
                        error_text=str(exc),
                        error_body=getattr(exc, "body", None),
                    )
                    if failed_condition.get("scenario") in {
                        "deactivated",
                        "billing_inactive",
                        "auth_invalid",
                        "local_credentials_missing",
                    }:
                        try:
                            self._record_codex_account_rotation_condition(
                                registry_path,
                                registry,
                                next_alias,
                                classified=failed_condition,
                            )
                        except Exception as state_exc:
                            logger.debug("Codex candidate failure state write failed: %s", state_exc)
                        continue
                    try:
                        if previous_target is not None:
                            swap_live_auth_symlink(live_auth, previous_target)
                        registry["active"] = previous_active
                        save_codex_account_registry(registry_path, registry)
                    except Exception as rollback_exc:
                        logger.debug("Codex account rotation rollback failed: %s", rollback_exc)
                    return False

                self._vprint(
                    f"{self.log_prefix}🔄 Codex account rotated ({classified.get('scenario')}). Switched account: {current} → {next_alias}",
                    force=True,
                )
                return True

            try:
                if previous_target is not None:
                    swap_live_auth_symlink(live_auth, previous_target)
                registry["active"] = previous_active
                save_codex_account_registry(registry_path, registry)
            except Exception as rollback_exc:
                logger.debug("Codex account rotation rollback failed: %s", rollback_exc)
            self._vprint(
                f"{self.log_prefix}⛔ Codex rotation exhausted: no viable account after {current}; blocked={', '.join(blocked_candidates) if blocked_candidates else 'none'}",
                force=True,
            )
            return False

    def _try_rotate_codex_account_on_limit(self, *, error_text: str = "") -> bool:
        return self._try_rotate_codex_account_on_error(
            status_code=429,
            error_text=error_text,
            error_body=None,
            reason_label="usage_limit",
        )
""".lstrip("\n")
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

    desired_template = """
if (
    self.api_mode == "codex_responses"
    and self.provider == "openai-codex"
    and self._try_rotate_codex_account_on_error(
        status_code=status_code,
        error_text=str(api_error),
        error_body=getattr(api_error, "body", None),
    )
):
    codex_auth_retry_attempted = False
    retry_count = 0
    continue

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
""".lstrip("\n")

    rate_limit_start = text.find('is_usage_limit = (\n')
    if rate_limit_start == -1:
        rate_limit_start = text.find('is_rate_limited = (\n')
    payload_token = 'is_payload_too_large = (\n'
    payload_start = text.find(payload_token, rate_limit_start if rate_limit_start != -1 else 0)
    if rate_limit_start == -1 or payload_start == -1:
        raise RuntimeError('run_agent rate-limit block not found')

    block_start = text.rfind('\n', 0, rate_limit_start) + 1
    indent = text[block_start:rate_limit_start]
    desired_with_payload = ''.join(
        (indent + line if line else '') + '\n'
        for line in desired_template.splitlines()
    )
    text = text[:block_start] + desired_with_payload + text[payload_start + len(payload_token):]
    path.write_text(text)


def patch_auth_store_symlink_preservation() -> None:
    path = HERMES_AUTH_MODULE_PATH
    text = path.read_text()
    start = text.find('def _save_auth_store(auth_store: Dict[str, Any]) -> Path:\n')
    end = text.find('\ndef _load_provider_state(', start)
    if start == -1 or end == -1:
        raise RuntimeError('auth store patch marker not found')
    replacement = '''def _save_auth_store(auth_store: Dict[str, Any]) -> Path:
    auth_file = _auth_file_path()
    auth_target = auth_file.resolve() if auth_file.is_symlink() else auth_file
    auth_target.parent.mkdir(parents=True, exist_ok=True)
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(auth_store, indent=2) + "\\n"
    tmp_path = auth_target.with_name(f"{auth_target.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, auth_target)
        try:
            dir_fd = os.open(str(auth_target.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    # Restrict file permissions to owner only
    try:
        auth_target.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return auth_target
'''
    text = text[:start] + replacement + text[end + 1:]
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

def describe_run_agent_patch() -> list[str]:
    path = HERMES_RUN_AGENT_PATH
    lines = []
    if not path.exists():
        return ['run_agent.py: missing']
    text = path.read_text()
    has_helpers = 'def _extract_codex_limit_metadata' in text and 'def _try_rotate_codex_account_on_error' in text
    has_runtime_hook = 'and self._try_rotate_codex_account_on_error(' in text
    if has_helpers and has_runtime_hook:
        status = 'patched'
    elif has_helpers:
        status = 'partial'
    else:
        status = 'upstream-only'
    lines.append(f'run_agent.py: {status}')
    lines.append('  base: Hermes upstream file')
    if status == 'patched':
        lines.append('  overlay: hmx auto-rotate patch for Codex auth/limit/account-health failures')
        lines.append('  behavior: update -> reapply patch -> verify compile')
    elif status == 'partial':
        lines.append('  overlay: helpers present but 429 runtime hook missing')
        lines.append('  action: rerun hmx patch-hermes to restore live auto-rotate behavior')
    else:
        lines.append('  overlay: not present')
    return lines

def verify_run_agent_compile() -> None:
    subprocess.run(['python3', '-m', 'py_compile', str(HERMES_RUN_AGENT_PATH)], check=True)


def verify_auth_module_compile() -> None:
    subprocess.run(['python3', '-m', 'py_compile', str(HERMES_AUTH_MODULE_PATH)], check=True)


def repair_live_auth_from_registry() -> bool:
    registry = load_registry()
    if not registry.get('active') or not registry.get('accounts'):
        return False
    target = active_target(registry)
    if target is None:
        return False
    sync_live_auth_symlink(registry)
    return True


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
        'hermes_cli.codex_account_registry': sys.modules.get('hermes_cli.codex_account_registry'),
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

            fake_auth.resolve_codex_runtime_credentials=_resolve_codex_runtime_credentials

            fake_registry = types.ModuleType('hermes_cli.codex_account_registry')

            def _resolve_codex_account_paths():
                return registry_path, live_auth

            def _load_codex_account_registry(path):
                return json.loads(path.read_text())

            def _save_codex_account_registry(path, registry):
                path.write_text(json.dumps(registry, indent=2, sort_keys=True) + '\n')

            def _parse_ts(value):
                if not isinstance(value, str) or not value:
                    return None
                try:
                    if value.endswith('Z'):
                        value = value[:-1] + '+00:00'
                    parsed = dt.datetime.fromisoformat(value)
                    if parsed.tzinfo is not None:
                        parsed = parsed.replace(tzinfo=None)
                    return parsed
                except Exception:
                    return None

            def _select_next_codex_account(accounts, current):
                rows = []
                cooling = []
                now = dt.datetime.utcnow().replace(microsecond=0)
                for alias, info in accounts.items():
                    if not isinstance(info, dict) or info.get('disabled'):
                        continue
                    limit = info.get('limit') if isinstance(info.get('limit'), dict) else {}
                    reset_at = _parse_ts(limit.get('reset_at'))
                    priority = int(info.get('priority', 100))
                    if reset_at and reset_at > now:
                        cooling.append((priority, alias))
                    else:
                        rows.append((priority, alias))
                rows.sort()
                cooling.sort()
                available = [alias for _, alias in rows if alias != current]
                if not available:
                    available = [alias for _, alias in cooling if alias != current]
                return available[0] if available else None

            def _swap_live_auth_symlink(live_path, target_path):
                tmp_link = live_path.with_name(f'.{live_path.name}.tmp.smoke')
                try:
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink()
                    tmp_link.symlink_to(target_path)
                    os.replace(tmp_link, live_path)
                finally:
                    try:
                        if tmp_link.exists() or tmp_link.is_symlink():
                            tmp_link.unlink()
                    except OSError:
                        pass

            def _extract_codex_account_error_metadata(error_text='', error_body=None):
                meta = {}
                if 'resets_in_seconds' in error_text:
                    meta['resets_in_seconds'] = 428190
                if 'resets_at' in error_text:
                    meta['resets_at'] = 1775181989
                if 'plan_type' in error_text:
                    meta['plan_type'] = 'team'
                return meta

            def _classify_codex_account_condition(status_code=None, error_text='', error_body=None):
                if status_code == 429 or 'usage limit' in error_text.lower():
                    return {
                        'scenario': 'limited',
                        'should_rotate': True,
                        'disable_account': False,
                        'code': None,
                        'meta': _extract_codex_account_error_metadata(error_text, error_body=error_body),
                    }
                return {
                    'scenario': 'unknown',
                    'should_rotate': False,
                    'disable_account': False,
                    'code': None,
                    'meta': {},
                }

            def _record_codex_account_limit_state(registry, current_alias, *, limit_meta=None):
                acct = registry['accounts'][current_alias]
                acct['limit'] = {
                    'state': 'limited',
                    'observed_at': dt.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
                    'source': 'codex_429',
                    **(limit_meta or {}),
                    'reset_at': '2099-01-01T00:00:00Z',
                }
                return True

            def _record_codex_account_deactivation_state(registry, current_alias, *, error_meta=None, disable_account=True):
                acct = registry['accounts'][current_alias]
                acct['deactivation'] = {
                    'state': 'deactivated',
                    'observed_at': dt.datetime.utcnow().replace(microsecond=0).isoformat() + 'Z',
                    'source': 'codex_402',
                }
                if disable_account:
                    acct['disabled'] = True
                return True

            fake_registry.resolve_codex_account_paths = _resolve_codex_account_paths
            fake_registry.load_codex_account_registry = _load_codex_account_registry
            fake_registry.save_codex_account_registry = _save_codex_account_registry
            fake_registry.select_next_codex_account = _select_next_codex_account
            fake_registry.swap_live_auth_symlink = _swap_live_auth_symlink
            fake_registry.classify_codex_account_condition = _classify_codex_account_condition
            fake_registry.extract_codex_account_error_metadata = _extract_codex_account_error_metadata
            fake_registry.record_codex_account_limit_state = _record_codex_account_limit_state
            fake_registry.record_codex_account_deactivation_state = _record_codex_account_deactivation_state

            fake_pkg = types.ModuleType('hermes_cli')
            fake_pkg.auth = fake_auth
            fake_pkg.codex_account_registry = fake_registry
            sys.modules['hermes_cli'] = fake_pkg
            sys.modules['hermes_cli.auth'] = fake_auth
            sys.modules['hermes_cli.codex_account_registry'] = fake_registry

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

