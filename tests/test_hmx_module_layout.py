from pathlib import Path
import importlib
import sys


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_hmx_is_split_into_readable_modules():
    root = REPO_ROOT
    expected = {
        'hmxlib/__init__.py',
        'hmxlib/runtime.py',
        'hmxlib/utils.py',
        'hmxlib/account_store.py',
        'hmxlib/account_health.py',
        'hmxlib/hermes_patch.py',
        'hmxlib/commands.py',
        'hmxlib/cli.py',
    }
    missing = sorted(str(path) for path in expected if not (root / path).exists())
    assert not missing, f'missing module files: {missing}'


def test_library_cli_exports_parser_and_main():
    sys.path.insert(0, str(REPO_ROOT))
    cli = importlib.import_module('hmxlib.cli')
    runtime = importlib.import_module('hmxlib.runtime')
    parser = cli.build_parser()
    args = parser.parse_args(['list'])
    assert args.command == 'list'
    assert callable(args.func)
    assert runtime.__version__ == '0.1.0'
