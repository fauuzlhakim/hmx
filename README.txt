hmx — Hermes single-home multi-account manager

Design
- One real Hermes home only: /root/.hermes
- Session/history/checkpoints/skills/config stay exactly where Hermes expects them
- The current implementation is centered on pooled Codex auth.json slots, but the registry now tracks provider per account so the format can evolve toward mixed-provider pools later
- Multiple Codex accounts live as separate auth files under:
  /root/.hermes/accounts/auth/
- Active account is just a symlink:
  /root/.hermes/auth.json -> /root/.hermes/accounts/auth/<alias>.json

Why this layout is better
- No more /root/.hermes-b, /root/.hermes-c workflow for day-to-day use
- Removing one expired account is just deleting one auth slot, not cleaning a whole Hermes home
- Hermes updates are less fragile because state/config stay in the default home
- Existing sessions remain in /root/.hermes/state.db, so nothing about session continuity changes

Current commands
- HMX_ROOT_HOME=/root/.hermes hmx list   # now includes provider + 5H cooldown view
- HMX_ROOT_HOME=/root/.hermes hmx explain  # show how upstream Hermes and the local overlay fit together
- HMX_ROOT_HOME=/root/.hermes hmx doctor
- HMX_ROOT_HOME=/root/.hermes hmx current
- HMX_ROOT_HOME=/root/.hermes hmx use main
- HMX_ROOT_HOME=/root/.hermes hmx use b -c
- HMX_ROOT_HOME=/root/.hermes hmx hop -c
- HMX_ROOT_HOME=/root/.hermes hmx add d
- HMX_ROOT_HOME=/root/.hermes hmx login d
- HMX_ROOT_HOME=/root/.hermes hmx import /path/to/auth.json d --activate
- HMX_ROOT_HOME=/root/.hermes hmx capture my-new-account
- HMX_ROOT_HOME=/root/.hermes hmx rename my-new-account research-alt
- HMX_ROOT_HOME=/root/.hermes hmx annotate research-alt --label "Research Alt" --role backup --note "Use for long-context overflow" --priority 4
- HMX_ROOT_HOME=/root/.hermes hmx remove b --purge
- HMX_ROOT_HOME=/root/.hermes hmx disable b
- HMX_ROOT_HOME=/root/.hermes hmx enable b
- HMX_ROOT_HOME=/root/.hermes hmx auto on
- HMX_ROOT_HOME=/root/.hermes hmx auto off
- HMX_ROOT_HOME=/root/.hermes hmx mode balanced
- HMX_ROOT_HOME=/root/.hermes hmx mode focus
- HMX_ROOT_HOME=/root/.hermes hmx mode saver
- HMX_ROOT_HOME=/root/.hermes hmx patch-hermes
- HMX_ROOT_HOME=/root/.hermes hmx smoke
- HMX_ROOT_HOME=/root/.hermes hmx update

Current imported accounts
- main  -> /root/.hermes/accounts/auth/main.json
- b     -> /root/.hermes/accounts/auth/b.json
- c     -> /root/.hermes/accounts/auth/c.json

Naming and annotating accounts
- Rename a slot without touching the auth contents:
  HMX_ROOT_HOME=/root/.hermes hmx rename b backup-team
- Add or update human-friendly metadata in the registry only:
  HMX_ROOT_HOME=/root/.hermes hmx annotate backup-team --label "Team Backup" --role backup --note "Use when main account hits limit" --priority 5
- Omitted annotate flags leave existing values unchanged

Removing an expired account
- Example: HMX_ROOT_HOME=/root/.hermes hmx remove b --purge
- If that account is active, hmx will switch active account first
- If you want to keep the file but stop using it, do:
  HMX_ROOT_HOME=/root/.hermes hmx disable b

Adding accounts without device-auth dependency
- If you already have a valid auth.json from any other flow/tool/browser capture:
  HMX_ROOT_HOME=/root/.hermes hmx import /path/to/auth.json alias --activate
- If you login manually and Hermes currently has the token loaded, save it as a slot:
  HMX_ROOT_HOME=/root/.hermes hmx capture alias

Auto limit handling
- Hermes was patched so a 429 usage_limit_reached from openai-codex will rotate to the next enabled account automatically and retry inside the same conversation
- Registry toggle:
  HMX_ROOT_HOME=/root/.hermes hmx auto on
  HMX_ROOT_HOME=/root/.hermes hmx auto off

Cost / quality modes
- balanced:
  gpt-5.4 + reasoning=high + short/simple prompts routed to gpt-5.4-mini
- focus:
  gpt-5.4 + reasoning=xhigh
- saver:
  gpt-5.4-mini + reasoning=medium

Suggested workflow
1. Normal work: HMX_ROOT_HOME=/root/.hermes hmx mode balanced
2. Hard tasks: HMX_ROOT_HOME=/root/.hermes hmx mode focus
3. Label important accounts once so list/doctor stay readable:
   HMX_ROOT_HOME=/root/.hermes hmx annotate main --label "Primary" --role default --priority 1
4. If a response says usage limit reached, Hermes should auto-hop to the next account
5. If you want to rotate manually: HMX_ROOT_HOME=/root/.hermes hmx hop -c
6. If you obtain a new auth.json externally: HMX_ROOT_HOME=/root/.hermes hmx import /path/to/auth.json newalias

Updates
- Sessions/config stay in the default Hermes home, so upgrades should not destroy the account pool
- `/root/.local/bin/hmx` is now a tiny wrapper that execs `/root/hermes-mux/hmx.py`, so source edits do not require a manual reinstall step
- The auto-limit behavior is a patch in run_agent.py; if a Hermes update overwrites it, run:
  HMX_ROOT_HOME=/root/.hermes hmx patch-hermes
- `hmx patch-hermes` now also verifies `run_agent.py` with `python3 -m py_compile`
- `hmx smoke` runs a local Codex 429 simulation and proves runtime account rotation happens before fallback
- `hmx update` runs `hermes update` non-interactively, then reapplies the patch, verifies it, and runs the smoke test in one command

Contributing
- See CONTRIBUTING.md for the expected workflow, test commands, and review checklist
- The repo is intentionally compact: `hmx.py` is the CLI entrypoint, `tests/test_hmx_list.py` covers the main behaviors, and docs live in README.txt/CONTRIBUTING.md
