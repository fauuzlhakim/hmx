import argparse
import contextlib
import datetime as dt
import importlib.util
import io
import json
from pathlib import Path


def load_hmx_module():
    spec = importlib.util.spec_from_file_location("hmx_module", "/root/hermes-mux/hmx.py")
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
    assert "unknown" in output
    assert "limited" in output
    assert "30%" in output
    assert "auto-rotate: on" in output


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
    assert "available_names = [alias for _, alias, _ in ordered if alias != current]" in patched
    assert "if not available_names:" in patched
    assert "next_alias = available_names[0]" in patched


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

    assert '_try_rotate_codex_account_on_limit(error_text=str(api_error))' in patched
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


def test_run_codex_rotation_smoke_test_passes():
    hmx = load_hmx_module()

    result = hmx.run_codex_rotation_smoke_test()

    assert result["active_after"] == "backup"
    assert result["fallback_called"] is False
    assert result["client_base_url"] == "https://example.invalid/codex"
    assert result["limit_reset_at"]


def test_cmd_patch_hermes_also_verifies_run_agent_compile(monkeypatch):
    hmx = load_hmx_module()
    calls = []

    def fake_wrapper():
        calls.append(("wrapper", None))

    def fake_patch_run_agent():
        calls.append(("patch", None))

    def fake_run(cmd, check):
        calls.append(("run", tuple(cmd), check))
        class _Result:
            returncode = 0
        return _Result()

    monkeypatch.setattr(hmx, "ensure_hmx_entrypoint_wrapper", fake_wrapper)
    monkeypatch.setattr(hmx, "patch_run_agent", fake_patch_run_agent)
    monkeypatch.setattr(hmx.subprocess, "run", fake_run)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exit_code = hmx.cmd_patch_hermes(argparse.Namespace())

    output = buf.getvalue()
    assert exit_code == 0
    assert calls[0] == ("wrapper", None)
    assert calls[1] == ("patch", None)
    assert calls[2] == (
        "run",
        ("python3", "-m", "py_compile", "/root/.hermes/hermes-agent/run_agent.py"),
        True,
    )
    assert "patched Hermes run_agent" in output
    assert "verified: run_agent.py compiles cleanly" in output


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
        "    def _try_rotate_codex_account_on_limit(self, *, error_text: str = \"\") -> bool:\n"
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
