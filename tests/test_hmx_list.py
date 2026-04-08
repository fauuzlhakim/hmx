import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def load_hmx_module():
    sys.path.insert(0, str(REPO_ROOT))
    spec = importlib.util.spec_from_file_location("hmx_module", REPO_ROOT / "hmx.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def write_auth(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_provider": "openai-codex",
                "providers": {
                    "openai-codex": {
                        "tokens": {
                            "access_token": "test-access-token",
                            "refresh_token": "test-refresh-token",
                        }
                    }
                },
            }
        )
    )


def test_describe_account_health_marks_future_limit_as_limited(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "limited.json"
    write_auth(auth_path)

    now = dt.datetime(2026, 3, 28, 9, 0, 0)
    info = {
        "email": "limited@example.com",
        "plan": "plus",
        "limit": {
            "state": "limited",
            "observed_at": "2026-03-28T08:55:00Z",
            "reset_at": "2026-03-28T10:30:00Z",
            "resets_in_seconds": 5400,
        },
    }

    health = hmx.describe_account_health("limited", info, auth_path, now=now)

    assert health["status"] == "limited"
    assert health["reset"] == "1h30m"
    assert health["five_h"] == "30%"
    assert health["detail"] == "usage limit"


def test_describe_account_health_treats_expired_limit_as_healthy(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "healthy.json"
    write_auth(auth_path)

    now = dt.datetime(2026, 3, 28, 12, 0, 0)
    info = {
        "email": "healthy@example.com",
        "plan": "team",
        "limit": {
            "state": "limited",
            "observed_at": "2026-03-28T08:00:00Z",
            "reset_at": "2026-03-28T09:00:00Z",
            "resets_in_seconds": 3600,
        },
    }

    health = hmx.describe_account_health("healthy", info, auth_path, now=now)

    assert health["status"] == "unknown"
    assert health["reset"] == "-"


def test_describe_account_health_treats_stale_limit_as_active_for_current_account(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "active.json"
    write_auth(auth_path)

    now = dt.datetime(2026, 4, 1, 14, 0, 0)
    info = {
        "email": "active@example.com",
        "plan": "plus",
        "last_selected_at": "2026-04-01T13:49:52Z",
        "limit": {
            "state": "limited",
            "observed_at": "2026-03-29T15:02:07Z",
            "reset_at": "2026-04-03T02:21:23Z",
            "resets_in_seconds": 386356,
        },
    }

    health = hmx.describe_account_health("active", info, auth_path, now=now, current_alias="active")

    assert health["status"] == "active"
    assert health["reset"] == "-"
    assert health["five_h"] == "-"


def test_describe_account_health_without_limit_telemetry_is_unknown(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "unknown.json"
    write_auth(auth_path)

    info = {
        "email": "unknown@example.com",
        "plan": "plus",
    }

    health = hmx.describe_account_health("unknown", info, auth_path, now=dt.datetime(2026, 3, 28, 12, 0, 0))

    assert health["status"] == "unknown"
    assert health["detail"] == "no limit telemetry"


def test_describe_account_health_treats_manual_limited_state_as_limited(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "manual-limited.json"
    write_auth(auth_path)

    info = {
        "email": "manual@example.com",
        "plan": "plus",
        "limit": {
            "state": "limited",
            "observed_at": "2026-03-28T08:00:00Z",
            "source": "manual",
        },
    }

    health = hmx.describe_account_health("manual", info, auth_path, now=dt.datetime(2026, 3, 28, 12, 0, 0))

    assert health["status"] == "limited"
    assert health["reset"] == "-"
    assert health["detail"] == "usage limit"


def test_cmd_list_renders_clean_table_output(tmp_path):
    hmx = load_hmx_module()
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    write_auth(auth_dir / "active.json")
    write_auth(auth_dir / "limited.json")

    hmx.AUTH_DIR = auth_dir
    hmx.ROOT_HOME = tmp_path

    registry = {
        "active": "active",
        "auto_switch_on_limit": True,
        "accounts": {
            "active": {
                "file": "active.json",
                "email": "active@example.com",
                "plan": "plus",
                "priority": 1,
            },
            "limited": {
                "file": "limited.json",
                "email": "limited@example.com",
                "plan": "team",
                "priority": 2,
                "limit": {
                    "state": "limited",
                    "observed_at": "2026-03-28T08:55:00Z",
                    "reset_at": "2026-03-28T10:30:00Z",
                    "resets_in_seconds": 5400,
                },
            },
        },
    }

    hmx.load_registry = lambda: registry
    hmx.utcnow = lambda: dt.datetime(2026, 3, 28, 9, 0, 0)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = hmx.cmd_list(argparse.Namespace(all=False))

    output = buf.getvalue()
    assert exit_code == 0
    assert "ACCOUNT" in output
    assert "PROVIDER" in output
    assert "STATUS" in output
    assert "5H" in output
    assert "RESET" in output
    assert "email=" not in output
    assert "plan=" not in output
    assert "file=" not in output
    assert "active@example.com" in output
    assert "limited@example.com" in output
    assert "openai-codex" in output
    assert "active" in output
    assert "limited" in output
    assert "30%" in output
    assert "auto-rotate: on" in output


def test_describe_account_health_prefers_fresh_probe_result(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "probe.json"
    write_auth(auth_path)

    info = {
        "email": "probe@example.com",
        "plan": "plus",
        "probe": {
            "observed_at": "2026-04-01T14:00:00Z",
            "status": "available",
            "detail": "probe ok",
        },
        "limit": {
            "state": "limited",
            "observed_at": "2026-04-01T13:00:00Z",
            "reset_at": "2026-04-01T17:00:00Z",
            "resets_in_seconds": 10800,
        },
    }

    health = hmx.describe_account_health("probe", info, auth_path, now=dt.datetime(2026, 4, 1, 14, 5, 0), current_alias=None)

    assert health["status"] == "available"
    assert health["detail"] == "live probe ok"
    assert health["reset"] == "-"


def test_describe_account_health_recomputes_limited_probe_countdown_from_observed_at(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "probe-limited.json"
    write_auth(auth_path)

    info = {
        "email": "probe@example.com",
        "plan": "plus",
        "probe": {
            "observed_at": "2026-04-01T14:00:00Z",
            "status": "limited",
            "detail": "usage limit",
            "meta": {"resets_in_seconds": 5400},
        },
    }

    health = hmx.describe_account_health("probe", info, auth_path, now=dt.datetime(2026, 4, 1, 14, 5, 0), current_alias=None)

    assert health["status"] == "limited"
    assert health["reset"] == "1h25m"
    assert health["five_h"] == "28%"



def test_describe_account_health_ignores_stale_limited_probe_for_current_account(tmp_path):
    hmx = load_hmx_module()
    auth_path = tmp_path / "probe-stale-limited.json"
    write_auth(auth_path)

    info = {
        "email": "probe@example.com",
        "plan": "plus",
        "last_selected_at": "2026-04-01T14:20:00Z",
        "probe": {
            "observed_at": "2026-04-01T14:00:00Z",
            "status": "limited",
            "detail": "usage limit",
            "meta": {"resets_in_seconds": 5400},
        },
        "limit": {
            "state": "limited",
            "observed_at": "2026-04-01T14:00:00Z",
            "reset_at": "2026-04-01T15:30:00Z",
            "resets_in_seconds": 5400,
        },
    }

    health = hmx.describe_account_health("probe", info, auth_path, now=dt.datetime(2026, 4, 1, 15, 0, 0), current_alias="probe")

    assert health["status"] == "active"
    assert health["reset"] == "-"
    assert health["five_h"] == "-"



def test_apply_probe_result_disables_auth_invalid_account():
    hmx = load_hmx_module()
    info = {"email": "broken@example.com", "plan": "team", "disabled": False}

    hmx.apply_probe_result(
        info,
        {
            "observed_at": "2026-04-03T05:16:25Z",
            "status": "auth_invalid",
            "code": "invalid_grant",
            "detail": "Codex token refresh failed with status 401.",
        },
    )

    assert info["disabled"] is True
    assert info["disabled_reason"] == "invalid_grant"
    assert info["disabled_at"] == "2026-04-03T05:16:25Z"
    assert info["auth_failure"]["state"] == "auth_invalid"


def test_cmd_list_can_refresh_with_probe(tmp_path):
    hmx = load_hmx_module()
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    write_auth(auth_dir / "active.json")

    hmx.AUTH_DIR = auth_dir
    hmx.ROOT_HOME = tmp_path

    registry = {
        "active": "active",
        "auto_switch_on_limit": True,
        "accounts": {
            "active": {
                "file": "active.json",
                "email": "active@example.com",
                "plan": "plus",
                "priority": 1,
            },
        },
    }

    called = {"probe": 0}

    def _fake_probe_accounts(current_registry, aliases=None, model='gpt-5.4'):
        called["probe"] += 1
        current_registry["accounts"]["active"]["probe"] = {
            "observed_at": "2026-04-01T14:00:00Z",
            "status": "available",
            "detail": "probe ok",
        }
        return [current_registry["accounts"]["active"]["probe"]]

    hmx.load_registry = lambda: registry
    hmx.probe_accounts = _fake_probe_accounts
    hmx.utcnow = lambda: dt.datetime(2026, 4, 1, 14, 0, 30)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = hmx.cmd_list(argparse.Namespace(all=False, probe=True, model='gpt-5.4'))

    output = buf.getvalue()
    assert exit_code == 0
    assert called["probe"] == 1
    assert "active" in output


def test_build_list_rows_deprioritizes_limited_accounts_and_uses_effective_rank(tmp_path):
    hmx = load_hmx_module()
    auth_dir = tmp_path / "auth"
    auth_dir.mkdir(parents=True)
    write_auth(auth_dir / "main.json")
    write_auth(auth_dir / "kariemsno.json")

    hmx.AUTH_DIR = auth_dir

    registry = {
        "active": "kariemsno",
        "accounts": {
            "main": {
                "file": "main.json",
                "email": "main@example.com",
                "plan": "plus",
                "priority": 1,
                "limit": {
                    "state": "limited",
                    "observed_at": "2026-04-01T13:08:21Z",
                    "reset_at": "2026-04-01T17:57:33Z",
                    "resets_in_seconds": 17353,
                },
            },
            "kariemsno": {
                "file": "kariemsno.json",
                "email": "kariemsno@example.com",
                "plan": "plus",
                "priority": 5,
                "last_selected_at": "2026-04-01T13:49:52Z",
                "limit": {
                    "state": "limited",
                    "observed_at": "2026-03-29T15:02:07Z",
                    "reset_at": "2026-04-03T02:21:23Z",
                    "resets_in_seconds": 386356,
                },
            },
        },
    }

    rows = hmx.build_list_rows(registry, now=dt.datetime(2026, 4, 1, 14, 0, 0))

    assert [row["account"] for row in rows] == ["kariemsno", "main"]
    assert rows[0]["status"] == "active"
    assert rows[0]["priority"] == "1"
    assert rows[1]["status"] == "limited"
    assert rows[1]["priority"] == "2"


def test_load_registry_backfills_default_provider_for_existing_accounts(tmp_path):
    hmx = load_hmx_module()
    auth_dir = tmp_path / "accounts" / "auth"
    auth_dir.mkdir(parents=True)
    registry_path = tmp_path / "accounts" / "registry.json"
    registry_path.write_text(
        json.dumps(
            {
                "schema": 2,
                "active": "legacy",
                "accounts": {
                    "legacy": {
                        "file": "legacy.json",
                        "email": "legacy@example.com",
                        "plan": "plus",
                        "priority": 1,
                    }
                },
            }
        )
    )

    hmx.ROOT_HOME = tmp_path
    hmx.MUX_DIR = tmp_path / "accounts"
    hmx.AUTH_DIR = auth_dir
    hmx.REGISTRY_PATH = registry_path

    registry = hmx.load_registry()

    assert registry["accounts"]["legacy"]["provider"] == hmx.DEFAULT_PROVIDER


def test_patch_run_agent_prefers_non_limited_candidate_even_if_current_is_cooling_down(tmp_path):
    hmx = load_hmx_module()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    run_agent_path = repo_dir / "run_agent.py"
    run_agent_path.write_text(
        "from pathlib import Path\n"
        "import json\n"
        "from datetime import datetime, timezone\n\n"
        "class AIAgent:\n"
        "    def _replace_primary_openai_client(self, reason=None):\n"
        "        return True\n\n"
        "    def _try_activate_fallback(self):\n"
        "        return False\n\n"
        "    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:\n"
        "        return False\n\n"
        "    def sample(self, status_code, error_msg, api_error):\n"
        "        is_rate_limited = (\n"
        "            status_code == 429\n"
        "            or \"usage limit\" in error_msg\n"
        "        )\n"
        "        if is_rate_limited and not self._fallback_activated:\n"
        "            if self._try_activate_fallback():\n"
        "                retry_count = 0\n"
        "                continue\n\n"
        "        is_payload_too_large = (\n"
        "            status_code == 413\n"
        "        )\n"
    )

    hmx.HERMES_RUN_AGENT_PATH = run_agent_path
    hmx.patch_run_agent()
    patched = run_agent_path.read_text()

    assert "current not in accounts" in patched
    assert "next_alias = select_next_codex_account(accounts, current)" in patched
    assert "while True:" in patched
    assert "swap_live_auth_symlink(live_auth, target)" in patched


def test_patch_run_agent_replaces_current_upstream_rate_limit_block(tmp_path):
    hmx = load_hmx_module()
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    run_agent_path = repo_dir / "run_agent.py"
    run_agent_path.write_text(
        "from pathlib import Path\n"
        "import json\n"
        "from datetime import datetime, timezone\n\n"
        "class AIAgent:\n"
        "    def _replace_primary_openai_client(self, reason=None):\n"
        "        return True\n\n"
        "    def _try_activate_fallback(self):\n"
        "        return False\n\n"
        "    def _try_refresh_codex_client_credentials(self, *, force: bool = True) -> bool:\n"
        "        return False\n\n"
        "    def sample(self, status_code, error_msg, api_error):\n"
        "        is_rate_limited = (\n"
        "            status_code == 429\n"
        "            or \"rate limit\" in error_msg\n"
        "            or \"too many requests\" in error_msg\n"
        "            or \"rate_limit\" in error_msg\n"
        "            or \"usage limit\" in error_msg\n"
        "            or \"quota\" in error_msg\n"
        "        )\n"
        "        if is_rate_limited and not self._fallback_activated:\n"
        "            self._emit_status(\"⚠️ Rate limited — switching to fallback provider...\")\n"
        "            if self._try_activate_fallback():\n"
        "                retry_count = 0\n"
        "                continue\n\n"
        "        is_payload_too_large = (\n"
        "            status_code == 413\n"
        "        )\n"
    )

    hmx.HERMES_RUN_AGENT_PATH = run_agent_path
    hmx.patch_run_agent()
    patched = run_agent_path.read_text()

    assert '_try_rotate_codex_account_on_error(' in patched
    assert 'self._emit_status("⚠️ Rate limited — switching to fallback provider...")' not in patched
    assert 'is_payload_too_large = (' in patched


def test_ensure_hmx_entrypoint_wrapper_writes_wrapper(tmp_path):
    hmx = load_hmx_module()
    hmx.HMX_SOURCE_PATH = tmp_path / "hmx.py"
    hmx.HMX_SOURCE_PATH.write_text("#!/usr/bin/env python3\n")
    hmx.HMX_BIN_PATH = tmp_path / "bin" / "hmx"

    hmx.ensure_hmx_entrypoint_wrapper()

    wrapper = hmx.HMX_BIN_PATH.read_text()
    assert wrapper == f'#!/usr/bin/env bash\nexec python3 {hmx.HMX_SOURCE_PATH} "$@"\n'
    assert oct(hmx.HMX_BIN_PATH.stat().st_mode & 0o777) == "0o755"


def test_resolve_hmx_source_path_prefers_repo_source_when_running_from_bin(tmp_path):
    hmx = load_hmx_module()
    installed_bin = tmp_path / "bin" / "hmx"
    repo_source = tmp_path / "repo" / "hmx.py"
    installed_bin.parent.mkdir(parents=True, exist_ok=True)
    repo_source.parent.mkdir(parents=True, exist_ok=True)
    installed_bin.write_text("#!/usr/bin/env python3\n")
    repo_source.write_text("#!/usr/bin/env python3\n")

    resolved = hmx.resolve_hmx_source_path(
        current_path=installed_bin,
        bin_path=installed_bin,
        default_source_path=repo_source,
    )

    assert resolved == repo_source.resolve()


def test_run_codex_rotation_smoke_test_passes():
    hmx = load_hmx_module()

    result = hmx.run_codex_rotation_smoke_test()

    assert result["active_after"] == "backup"
    assert result["fallback_called"] is False
    assert result["client_base_url"] == "https://example.invalid/codex"
    assert result["limit_reset_at"]


def test_patch_auth_store_symlink_preservation_keeps_live_auth_symlink(tmp_path):
    hmx = load_hmx_module()
    auth_module_path = tmp_path / "auth.py"
    live_auth = tmp_path / "auth.json"
    pooled_auth = tmp_path / "accounts" / "auth" / "labold1337.json"
    pooled_auth.parent.mkdir(parents=True, exist_ok=True)
    pooled_auth.write_text('{"version": 1, "providers": {}}\n')
    live_auth.symlink_to(pooled_auth)
    auth_module_path.write_text(
        "import json\n"
        "import os\n"
        "import stat\n"
        "import uuid\n"
        "from datetime import datetime, timezone\n"
        "from pathlib import Path\n"
        "from typing import Any, Dict\n\n"
        "AUTH_STORE_VERSION = 1\n"
        f"AUTH_FILE = Path({str(live_auth)!r})\n\n"
        "def _auth_file_path() -> Path:\n"
        "    return AUTH_FILE\n\n"
        "def _save_auth_store(auth_store: Dict[str, Any]) -> Path:\n"
        "    auth_file = _auth_file_path()\n"
        "    auth_file.parent.mkdir(parents=True, exist_ok=True)\n"
        "    auth_store[\"version\"] = AUTH_STORE_VERSION\n"
        "    auth_store[\"updated_at\"] = datetime.now(timezone.utc).isoformat()\n"
        "    payload = json.dumps(auth_store, indent=2) + \"\\n\"\n"
        "    tmp_path = auth_file.with_name(f\"{auth_file.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}\")\n"
        "    try:\n"
        "        with tmp_path.open(\"w\", encoding=\"utf-8\") as handle:\n"
        "            handle.write(payload)\n"
        "            handle.flush()\n"
        "            os.fsync(handle.fileno())\n"
        "        os.replace(tmp_path, auth_file)\n"
        "    finally:\n"
        "        try:\n"
        "            if tmp_path.exists():\n"
        "                tmp_path.unlink()\n"
        "        except OSError:\n"
        "            pass\n"
        "    try:\n"
        "        auth_file.chmod(stat.S_IRUSR | stat.S_IWUSR)\n"
        "    except OSError:\n"
        "        pass\n"
        "    return auth_file\n\n"
        "def _load_provider_state(*args, **kwargs):\n"
        "    return None\n"
    )

    hmx.HERMES_AUTH_MODULE_PATH = auth_module_path
    hmx.patch_auth_store_symlink_preservation()

    spec = importlib.util.spec_from_file_location("patched_auth_module", auth_module_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    returned = module._save_auth_store({"providers": {"openai-codex": {}}})

    assert live_auth.is_symlink()
    assert live_auth.resolve() == pooled_auth.resolve()
    assert returned == pooled_auth.resolve()
    payload = json.loads(pooled_auth.read_text())
    assert payload["providers"]["openai-codex"] == {}


def test_cmd_patch_hermes_patches_run_agent_and_auth_then_repairs_live_auth(monkeypatch):
    hmx = load_hmx_module()
    calls = []

    monkeypatch.setattr(hmx, "ensure_hmx_entrypoint_wrapper", lambda: calls.append(("wrapper", None)))
    monkeypatch.setattr(hmx, "patch_run_agent", lambda: calls.append(("patch_run_agent", None)))
    monkeypatch.setattr(hmx, "patch_auth_store_symlink_preservation", lambda: calls.append(("patch_auth", None)))
    monkeypatch.setattr(hmx, "verify_run_agent_compile", lambda: calls.append(("verify_run_agent", None)))
    monkeypatch.setattr(hmx, "verify_auth_module_compile", lambda: calls.append(("verify_auth", None)))
    monkeypatch.setattr(hmx, "repair_live_auth_from_registry", lambda: calls.append(("repair_live_auth", None)) or True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = hmx.cmd_patch_hermes(argparse.Namespace())

    output = buf.getvalue()
    assert exit_code == 0
    assert calls == [
        ("wrapper", None),
        ("patch_run_agent", None),
        ("patch_auth", None),
        ("verify_run_agent", None),
        ("verify_auth", None),
        ("repair_live_auth", None),
    ]
    assert "patched Hermes run_agent" in output
    assert "patched Hermes auth store writes to preserve hmx-selected auth symlinks" in output
    assert "verified: run_agent.py and hermes_cli/auth.py compile cleanly" in output


def test_cmd_update_hermes_runs_update_then_repatches_and_smokes(monkeypatch):
    hmx = load_hmx_module()
    calls = []

    def fake_wrapper():
        calls.append(("wrapper", None))

    def fake_run(cmd, cwd=None, stdin=None, check=None):
        calls.append(("run", tuple(cmd), cwd, stdin, check))
        class _Result:
            returncode = 0
        return _Result()

    def fake_patch(args):
        calls.append(("patch", args))
        return 0

    def fake_smoke(args):
        calls.append(("smoke", args))
        return 0

    monkeypatch.setattr(hmx, "ensure_hmx_entrypoint_wrapper", fake_wrapper)
    monkeypatch.setattr(hmx.subprocess, "run", fake_run)
    monkeypatch.setattr(hmx, "cmd_patch_hermes", fake_patch)
    monkeypatch.setattr(hmx, "cmd_smoke", fake_smoke)
    monkeypatch.setattr(hmx, "hermes_cmd", lambda: ["hermes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = hmx.cmd_update_hermes(argparse.Namespace(skip_smoke=False))

    output = buf.getvalue()
    assert exit_code == 0
    assert calls[0] == ("wrapper", None)
    assert calls[1][0] == "run"
    assert calls[1][1] == ("hermes", "update")
    assert calls[1][2] == hmx.HERMES_REPO_PATH
    assert calls[1][3] is hmx.subprocess.DEVNULL
    assert calls[1][4] is False
    assert calls[2][0] == "patch"
    assert calls[3][0] == "smoke"
    assert "running hermes update" in output.lower()
    assert "reapplying hermes patch" in output.lower()
    assert "running codex rotation smoke test" in output.lower()


def test_describe_run_agent_patch_reports_partial_overlay(tmp_path):
    hmx = load_hmx_module()
    run_agent_path = tmp_path / "run_agent.py"
    run_agent_path.write_text(
        "class AIAgent:\n"
        "    def _extract_codex_limit_metadata(self, error_text: str = \"\") -> dict:\n"
        "        return {}\n\n"
        "    def _try_rotate_codex_account_on_error(self, *, status_code=None, error_text: str = \"\", error_body=None) -> bool:\n"
        "        return False\n"
    )
    hmx.HERMES_RUN_AGENT_PATH = run_agent_path

    lines = hmx.describe_run_agent_patch()

    assert lines[0] == "run_agent.py: partial"
    assert any("runtime hook missing" in line for line in lines)


def test_cmd_explain_describes_upstream_plus_overlay(monkeypatch):
    hmx = load_hmx_module()
    monkeypatch.setattr(hmx, "load_registry", lambda: {"active": "main"})
    monkeypatch.setattr(hmx, "describe_run_agent_patch", lambda: ["run_agent.py: patched", "  overlay: hmx auto-rotate patch"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = hmx.cmd_explain(argparse.Namespace())

    output = buf.getvalue()
    assert exit_code == 0
    assert "Hermes layering:" in output
    assert "hermes update pulls upstream changes" in output
    assert "hmx patch-hermes reapplies the local Codex rotation overlay" in output
    assert "run_agent.py: patched" in output
