import argparse
import os
from pathlib import Path
import shutil
import subprocess

from .runtime import DEFAULT_PROVIDER, HERMES_REPO_PATH, HMX_BIN_PATH, HMX_SOURCE_PATH, MODE_PRESETS, ROOT_HOME
from .utils import deep_get, deep_set, err, now_iso, utcnow
from .account_store import (
    AUTH_DIR, LIVE_AUTH_PATH, ensure_auth_lock, ensure_base_config, import_auth_file,
    load_registry, load_yaml, dump_yaml, migrate_from_existing, ordered_accounts,
    repair_live_auth_from_registry, save_registry, sync_live_auth_symlink,
)
from .account_health import (
    build_list_rows, doctor_status, fresh_probe_result, list_summary_text, metadata_text,
    next_account, note_text, probe_accounts, render_table,
)
from .hermes_patch import (
    describe_run_agent_patch, ensure_hmx_entrypoint_wrapper, patch_auth_store_symlink_preservation,
    patch_run_agent, run_codex_rotation_smoke_test, unpatch_run_agent,
    verify_auth_module_compile, verify_run_agent_compile,
)

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

        registry_path, live_auth = resolve_codex_account_paths()
        registry = load_codex_account_registry(registry_path)
        if not isinstance(registry, dict):
            return False

        current = registry.get("active")
        accounts = registry.get("accounts") if isinstance(registry.get("accounts"), dict) else {}
        if current not in accounts:
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

        while True:
            next_alias = select_next_codex_account(accounts, current)
            if not next_alias:
                break

            next_info = accounts.get(next_alias) or {}
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
    if getattr(args, 'probe', False):
        probe_accounts(registry, model=getattr(args, 'model', 'gpt-5.4'))
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
    for alias in ordered_accounts(registry, include_disabled=args.all, now=utcnow()):
        info = registry['accounts'][alias]
        meta = metadata_text(info)
        note = note_text(info)
        probe = fresh_probe_result(info, now=utcnow())
        extras = []
        if meta:
            extras.append(meta)
        if note:
            extras.append(f'note={note}')
        if probe:
            detail = str(probe.get('detail') or probe.get('code') or probe.get('status') or '').strip()
            observed = probe.get('observed_at') or '-'
            extras.append(f'probe={probe.get("status", "unknown")} @ {observed}')
            if detail and detail != 'probe ok':
                extras.append(f'detail={detail[:96]}')
        if extras:
            metadata_lines.append(f'- {alias}: ' + ' | '.join(extras))
    if metadata_lines:
        print('')
        print('notes:')
        for line in metadata_lines:
            print(line)
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    registry = load_registry()
    aliases = [args.alias] if getattr(args, 'alias', None) else None
    results = probe_accounts(registry, aliases=aliases, model=getattr(args, 'model', 'gpt-5.4'))
    if not results:
        print('no accounts probed')
        return 0

    headers = ['CUR', 'ACCOUNT', 'STATUS', 'HTTP', 'CODE', 'LAT', 'LAST_OK', 'OBSERVED']
    active_alias = registry.get('active')
    table_rows = []
    for result in results:
        table_rows.append([
            '*' if result.get('account') == active_alias else '',
            str(result.get('account') or '-'),
            str(result.get('status') or '-'),
            str(result.get('status_code') or '-'),
            str(result.get('code') or '-'),
            (str(result.get('latency_ms')) + 'ms') if result.get('latency_ms') is not None else '-',
            str(result.get('last_ok_at') or '-'),
            str(result.get('observed_at') or '-'),
        ])
    for line in render_table(headers, table_rows):
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

def cmd_patch_hermes(args: argparse.Namespace) -> int:
    ensure_hmx_entrypoint_wrapper()
    patch_run_agent()
    patch_auth_store_symlink_preservation()
    try:
        verify_run_agent_compile()
        verify_auth_module_compile()
    except Exception as exc:
        err(f'patched Hermes overlay but verification failed: {exc}')
    repair_live_auth_from_registry()
    print('patched Hermes run_agent for automatic codex account rotation on usage-limit 429')
    print('patched Hermes auth store writes to preserve hmx-selected auth symlinks')
    print('verified: run_agent.py and hermes_cli/auth.py compile cleanly')
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

