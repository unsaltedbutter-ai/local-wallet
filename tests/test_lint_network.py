"""Tests for the network-import lint tool (tools/lint_network.py)."""

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LINT_PATH = REPO_ROOT / "tools" / "lint_network.py"


def _load_lint():
    spec = importlib.util.spec_from_file_location("lint_network", LINT_PATH)
    module = importlib.util.module_from_spec(spec)
    # py3.14: dataclass resolution (Violation) requires the module to be in
    # sys.modules before exec_module, so register it first.
    import sys

    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


LINT = _load_lint()
SRC_ROOT = LINT.SRC_ROOT


def test_lint_passes_on_current_scaffold():
    """The shipped scaffold contains no network imports outside chain/."""
    assert LINT.check_tree(SRC_ROOT) == []


def test_lint_catches_network_import_outside_chain(tmp_path):
    evil = tmp_path / "evil.py"
    evil.write_text(
        "import httpx\n"
        "from urllib.request import urlopen\n"
        "import socket\n",
        encoding="utf-8",
    )
    violations = LINT.check_tree(tmp_path)
    assert len(violations) == 3
    for v in violations:
        assert v.path == evil
    modules = {v.module for v in violations}
    assert "httpx" in modules
    assert "urllib" in modules
    assert "socket" in modules


def test_lint_allows_network_import_inside_chain(tmp_path):
    chain_dir = tmp_path / "chain"
    chain_dir.mkdir()
    ok = chain_dir / "esplora.py"
    ok.write_text("import httpx\n", encoding="utf-8")
    assert LINT.check_tree(tmp_path) == []
