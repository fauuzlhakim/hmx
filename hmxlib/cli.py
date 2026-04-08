import argparse

from .runtime import DEFAULT_PROVIDER, MODE_PRESETS
from .commands import (
    cmd_add, cmd_annotate, cmd_auto, cmd_capture, cmd_current, cmd_disable, cmd_doctor,
    cmd_enable, cmd_explain, cmd_hop, cmd_import, cmd_init, cmd_list, cmd_login, cmd_mode,
    cmd_patch_hermes, cmd_probe, cmd_remove, cmd_rename, cmd_smoke, cmd_unpatch_hermes,
    cmd_update_hermes, cmd_use,
)

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
    sp.add_argument('--probe', action='store_true', help='refresh account health by probing each account before listing')
    sp.add_argument('--model', default='gpt-5.4', help='model to use for probe requests')
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser('probe', help='probe one or more accounts and store fresh health results')
    sp.add_argument('alias', nargs='?')
    sp.add_argument('--model', default='gpt-5.4', help='model to use for probe requests')
    sp.set_defaults(func=cmd_probe)

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


