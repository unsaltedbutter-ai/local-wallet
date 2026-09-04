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


# ------------------------------------------------- ADR-0007 bridge exception


def test_lint_exception_names_exactly_the_bridge_file():
    """The single lint exception is agent/remote_runtime.py (ADR-0007)."""
    assert LINT.AGENT_LLM_TRANSPORT_FILES == ("agent/remote_runtime.py",)


def test_lint_allows_network_import_in_remote_runtime_bridge(tmp_path):
    """Network imports are permitted in exactly the ADR-0007 bridge file."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    bridge = agent_dir / "remote_runtime.py"
    bridge.write_text(
        "import httpx\nfrom httpx import URL\nfrom urllib.parse import urlsplit\n",
        encoding="utf-8",
    )
    assert LINT.check_tree(tmp_path) == []


def test_lint_still_flags_network_imports_in_other_agent_files(tmp_path):
    """Every other agent/ file stays banned — the exception is not a prefix."""
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "remote_runtime.py").write_text("# no imports here\n", encoding="utf-8")
    (agent_dir / "loop.py").write_text("import httpx\n", encoding="utf-8")
    (agent_dir / "runtime.py").write_text("import socket\n", encoding="utf-8")
    (agent_dir / "remote_runtime_v2.py").write_text("import requests\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    flagged = {v.path.name for v in violations}
    assert flagged == {"loop.py", "runtime.py", "remote_runtime_v2.py"}


def test_lint_passes_on_real_tree_with_bridge_import():
    """The real remote_runtime.py (which imports httpx) lints clean."""
    assert LINT.check_tree(LINT.SRC_ROOT) == []


# ------------------------------------------------- ADR-0016 node/ exception


def test_lint_exception_names_exactly_the_node_dir():
    """The node-doctor exception is exactly the node/ package (ADR-0016)."""
    assert LINT.NODE_NETWORK_DIRS == ("node",)


def test_lint_allows_network_import_inside_node_dir(tmp_path):
    """Network imports are permitted anywhere under node/ (loopback-only)."""
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    (node_dir / "detect.py").write_text("import httpx\n", encoding="utf-8")
    (node_dir / "__init__.py").write_text(
        "from localwallet.node.detect import foo\n", encoding="utf-8"
    )
    assert LINT.check_tree(tmp_path) == []


def test_lint_does_not_exempt_a_sibling_file_named_node(tmp_path):
    """The node/ exception is a directory, not a name prefix."""
    (tmp_path / "node.py").write_text("import httpx\n", encoding="utf-8")
    (tmp_path / "node_extra.py").write_text("import socket\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    flagged = {v.path.name for v in violations}
    assert flagged == {"node.py", "node_extra.py"}


def test_lint_still_flags_network_imports_in_other_packages(tmp_path):
    """Unrelated packages stay banned — only chain/, agent/remote_runtime.py,
    and node/ are exempt."""
    (tmp_path / "wallet.py").write_text("import httpx\n", encoding="utf-8")
    (tmp_path / "store.py").write_text("import socket\n", encoding="utf-8")
    violations = LINT.check_tree(tmp_path)
    flagged = {v.path.name for v in violations}
    assert flagged == {"wallet.py", "store.py"}
